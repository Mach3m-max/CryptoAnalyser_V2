# bot/core.py
"""
TradingBot v2 — ядро бота.
Основа: main.py v1 (BotByBit).

ИСПРАВЛЕННЫЕ БАГИ v1:
  ✅ Ghost v1       — sync loop не воскрешает закрывающуюся позицию
  ✅ Bug #3         — досрочный выход очищает breakeven_activated и _trail_best
  ✅ detect_close   — проверяет orderStatus=Filled по сохранённым ID
  ✅ dashboard buy  — open_position_from_dashboard() вызывается после BUY
  ✅ trail floor    — SL не опускается ниже entry * (1 + floor_pct)

АРХИТЕКТУРНЫЕ ИЗМЕНЕНИЯ v2:
  • stable_core.BybitTrader вместо bybit_client.BybitTrader (один источник правды)
  • stable_core.OrderExecutor вместо инлайн-методов place_order в TradingBot
  • config.pairs_config.json вместо хардкода PORTFOLIO в config.py
  • logging_v2.log_decision вместо strategy_logger.log_decision
  • security.load_credentials вместо прямого чтения config.json
"""

import os
import sys
import json
import math
import time
import threading
import signal
from datetime import datetime

# ── Путь к корню проекта ──────────────────────────────────────────────────────
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

# ── Конфигурация ──────────────────────────────────────────────────────────────
from config.app_config import (
    ABS_RESERVE, FEE_PCT,
    BREAKEVEN_TRIGGER, TRAILING_PCT, TRAILING_MIN_MOVE,
    SELL_CLOSE_CONF, MAX_HOLD_BARS,
    ANALYZE_INTERVAL_SEC, MONITOR_INTERVAL_SEC,
    DEBOUNCE_SEC, PAIRS_CONFIG_PATH, BOT_SETTINGS_PATH,
    DASHBOARD_DEMO_PORT, DASHBOARD_REAL_PORT,
)

# ── Стабильные модули (verbatim v1) ───────────────────────────────────────────
from stable_core.bybit_client  import BybitTrader
from stable_core.order_executor import OrderExecutor

# ── Модули v1 (скопированы без изменений) ────────────────────────────────────
from portfolio_manager import PortfolioManager
from data_loader       import DataLoader
from signal_cache      import SignalCache

try:
    from ml.ml_strategy_engine import MLStrategyEngine as StrategyEngine
    print("✅ ML стратегия активна")
except ImportError as _e:
    from strategy_engine import StrategyEngine
    print(f"⚠️  ML не найден ({_e}), используется базовая стратегия")

# ── Логирование v2 ────────────────────────────────────────────────────────────
from logging_v2 import log_decision, log_close, log_candle

# ── Дашборд ───────────────────────────────────────────────────────────────────
from dashboard.app import current_data, register_bot, start_dashboard


# ── Загрузка конфигурации пар ─────────────────────────────────────────────────

def load_pairs_config() -> dict:
    """Читает config/pairs_config.json и возвращает активные пары."""
    if not os.path.exists(PAIRS_CONFIG_PATH):
        print(f"⚠️  {PAIRS_CONFIG_PATH} не найден")
        return {}
    with open(PAIRS_CONFIG_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def get_active_pairs(pairs_config: dict, mode: str) -> list:
    """Возвращает список пар, активных для данного режима."""
    key = "real_enabled" if mode == "REAL" else "demo_enabled"
    return [sym for sym, cfg in pairs_config.items() if cfg.get(key, False)]


# ═════════════════════════════════════════════════════════════════════════════
# TradingBot
# ═════════════════════════════════════════════════════════════════════════════

class TradingBot:

    def __init__(self, real_mode: bool = False,
                 api_key: str = None, api_secret: str = None):
        """
        Args:
            real_mode  — True = REAL, False = DEMO
            api_key    — передаётся из security.load_credentials()
            api_secret — передаётся из security.load_credentials()
        """
        self.real_mode = real_mode
        self.running   = True
        _mode_str      = "REAL" if real_mode else "DEMO"

        print("\n" + "=" * 70)
        print(f"🚀 ЗАПУСК БОТА HTT v2 — {_mode_str}")
        print("=" * 70)

        if not api_key or not api_secret:
            raise ValueError("API ключи не переданы. Используй security.load_credentials()")

        # ── API клиент (stable_core, verbatim v1) ─────────────────────────────
        self.trader = BybitTrader(api_key, api_secret, testnet=not real_mode)
        print(f"✅ API клиент: {self.trader.base_url}")

        # ── Конфигурация пар ──────────────────────────────────────────────────
        self.pairs_config  = load_pairs_config()
        self.active_pairs  = get_active_pairs(self.pairs_config, _mode_str)
        print(f"📊 Активных пар [{_mode_str}]: {len(self.active_pairs)} — {self.active_pairs}")

        # ── Компоненты ────────────────────────────────────────────────────────
        self.portfolio    = PortfolioManager()
        self.data_loader  = DataLoader(self.trader)
        self.strategy     = StrategyEngine()
        self.signal_cache = SignalCache()

        # ── Состояние ─────────────────────────────────────────────────────────
        self.signals:          dict = {}
        self.historical_data:  dict = {}
        self.instruments:      dict = {}   # {symbol: {min_qty, qty_step, tick_size}}
        self.open_order_ids:   dict = self._load_order_ids()
        self.breakeven_activated: dict = {}
        self._trail_best:      dict = {}   # {symbol: max_price_seen}
        self._sw_closing:      set  = set()
        self._debounce:        dict = {}   # {symbol: last_signal_ts}

        # ── Параметры стратегии из bot_settings.json ──────────────────────────
        self.BREAKEVEN_TRIGGER  = BREAKEVEN_TRIGGER
        self.TRAILING_PCT       = TRAILING_PCT
        self.TRAILING_MIN_MOVE  = TRAILING_MIN_MOVE
        self.SELL_CLOSE_CONF    = SELL_CLOSE_CONF
        self.shorts_enabled     = False
        self._load_bot_settings()

        # ── OrderExecutor (stable_core, verbatim v1) ──────────────────────────
        self.executor = OrderExecutor(
            trader=self.trader,
            instruments=self.instruments,
            portfolio=self.portfolio,
            open_order_ids=self.open_order_ids,
            real_mode=self.real_mode,
            current_data=current_data,
            breakeven_activated=self.breakeven_activated,
        )

        # ── Дашборд ───────────────────────────────────────────────────────────
        current_data['mode']            = _mode_str
        current_data['trading_allowed'] = False
        current_data['prices']          = {}
        current_data['portfolio']       = self.portfolio.get_portfolio_summary()
        current_data.setdefault('trades', [])

        print("✅ Все компоненты инициализированы")
        self._start_background_tasks()

    # ─────────────────────────────────────────────────────────────────────────
    # Балансы
    # ─────────────────────────────────────────────────────────────────────────

    def update_real_balances(self):
        """Синхронизирует total_capital с реальным балансом Bybit."""
        try:
            wb = self.trader.get_wallet_balance()
            if wb.get("retCode") == 0:
                total = 0.0
                for coin_info in wb["result"]["list"][0]["coin"]:
                    try:
                        uv = float(coin_info.get("usdValue") or 0)
                        total += uv
                    except Exception:
                        pass
                self.portfolio.total_capital = total
                current_data.setdefault('balances', {})['total_usdt'] = round(total, 2)
                current_data['portfolio'] = self.portfolio.get_portfolio_summary()
        except Exception as e:
            print(f"⚠️ update_real_balances: {e}")

    # ─────────────────────────────────────────────────────────────────────────
    # Главный цикл анализа
    # ─────────────────────────────────────────────────────────────────────────

    def analyze_markets(self):
        """Цикл анализа: каждые 60 сек получает сигналы и исполняет их."""
        from data_loader import DataLoader

        # Загружаем данные при старте
        self.historical_data = {}
        for sym in self.active_pairs:
            try:
                df = self.data_loader.load_historical_data(sym)
                if df is not None and len(df) > 0:
                    self.historical_data[sym] = df
                    instr_raw = self.trader.get_instruments_info(sym, "spot")
                    try:
                        lot = instr_raw["result"]["list"][0]["lotSizeFilter"]
                        pf  = instr_raw["result"]["list"][0].get("priceFilter", {})
                        self.instruments[sym] = {
                            'min_qty':      float(lot.get("minOrderQty", 0.001)),
                            'min_notional': float(lot.get("minOrderAmt", 5.0)),
                            'qty_step':     float(lot.get("basePrecision", 0.001)),
                            'tick_size':    float(pf.get("tickSize", 0.01)),
                        }
                    except Exception:
                        pass
            except Exception as e:
                print(f"⚠️ Загрузка {sym}: {e}")

        print(f"📈 Загружено данных: {len(self.historical_data)}/{len(self.active_pairs)} пар")

        while self.running:
            try:
                prices = {}
                for sym in self.active_pairs:
                    try:
                        price = self.trader.get_price(sym, "spot")
                        if price > 0:
                            prices[sym] = price
                    except Exception as e:
                        print(f"⚠️ Ошибка получения цены {sym}: {e}")

                current_data['prices'] = prices

                if not current_data.get('trading_allowed', False):
                    time.sleep(ANALYZE_INTERVAL_SEC)
                    continue

                # Обновляем свечи
                for sym in self.active_pairs:
                    try:
                        df = self.data_loader.update_realtime(sym)
                        if df is not None:
                            self.historical_data[sym] = df
                    except Exception:
                        pass

                # Анализируем сигналы
                for sym in self.active_pairs:
                    df = self.historical_data.get(sym)
                    if df is None or len(df) < 200:
                        continue
                    try:
                        signal = self.strategy.analyze_pair(sym, df)
                        if signal:
                            self.signals[sym] = signal
                            self._process_signal(sym, signal, prices.get(sym, 0))
                    except Exception as e:
                        print(f"⚠️ Анализ {sym}: {e}")

                current_data['signals'] = self.signals

            except Exception as e:
                print(f"❌ analyze_markets: {e}")

            time.sleep(ANALYZE_INTERVAL_SEC)

    def _process_signal(self, symbol: str, signal: dict, current_price: float):
        """Обработка сигнала: дебаунс → фильтры → execute."""
        if signal.get('signal') not in ('BUY', 'SELL'):
            return

        # Дебаунс
        now = time.time()
        if now - self._debounce.get(symbol, 0) < DEBOUNCE_SEC:
            return
        self._debounce[symbol] = now

        conf = signal.get('confidence', 0)
        pair_cfg = self.pairs_config.get(symbol, {})
        min_conf = pair_cfg.get('min_conf', 0.60)

        if conf < min_conf:
            return

        sig_type = signal['signal']

        if sig_type == 'BUY':
            self._execute_buy(symbol, signal, current_price, pair_cfg)
        elif sig_type == 'SELL' and self.shorts_enabled:
            self._execute_short_signal(symbol, signal, current_price)

    def _execute_buy(self, symbol: str, signal: dict,
                     current_price: float, pair_cfg: dict):
        """Исполнение BUY сигнала."""
        can, reason = self.portfolio.can_open(symbol)
        if not can:
            print(f"⏸️ {symbol}: пропущен — {reason}")
            return

        # Проверка баланса
        usdt_bal = self.trader.get_coin_balance("USDT")
        if usdt_bal < ABS_RESERVE + 10:
            print(f"⏸️ {symbol}: недостаточно баланса ({usdt_bal:.2f} USDT)")
            return

        usdt_amount = self.portfolio.get_order_amount()
        tp_pct = pair_cfg.get('tp_pct', 3.5)
        sl_pct = pair_cfg.get('sl_pct', 3.0)

        print(f"🔔 ИСПОЛНЕНИЕ BUY {symbol}  conf={signal['confidence']*100:.1f}%"
              f"  TP={tp_pct}%  SL={sl_pct}%")

        success = self.executor.place_order(symbol, "BUY", usdt_amount, tp_pct, sl_pct)

        if success:
            self.portfolio.open_position(symbol, usdt_amount, current_price)
            self._sw_closing.discard(symbol)
            self.breakeven_activated.pop(symbol, None)
            self._trail_best.pop(symbol, None)

            log_decision(
                symbol=symbol, event_type='POSITION_OPENED',
                signal='BUY', confidence=signal['confidence'],
                price=current_price, tp_pct=tp_pct, sl_pct=sl_pct,
                usdt_amount=usdt_amount,
                portfolio_open_count=self.portfolio.open_count(),
                total_capital=self.portfolio.total_capital,
                mode='REAL' if self.real_mode else 'DEMO',
            )
            print(f"✅ BUY {symbol} исполнен")

    # ─────────────────────────────────────────────────────────────────────────
    # Мониторинг позиций (trail + досрочный выход)
    # ─────────────────────────────────────────────────────────────────────────

    def monitor_positions(self):
        """
        Мониторинг каждые 10 сек.
        Программные SL/TP/Trail — не полагается на биржевые условные ордера.
        ИСПРАВЛЕНО: Ghost v1, Bug #3, trail floor.
        """
        while self.running:
            try:
                open_pos = list(self.portfolio.open_positions.items())
                if not open_pos:
                    time.sleep(MONITOR_INTERVAL_SEC)
                    continue

                for sym, pos in open_pos:
                    # ── Ghost v1 FIX: пропускаем закрывающуюся позицию ────────
                    if sym in self._sw_closing:
                        continue

                    entry_price = pos.get('entry_price', 0)
                    if entry_price <= 0:
                        # Пробуем восстановить entry
                        ep = self.executor._restore_entry_price(sym)
                        if ep > 0:
                            self.portfolio.update_entry_price(sym, ep)
                            entry_price = ep
                        else:
                            continue

                    cur_price = current_data['prices'].get(sym, 0)
                    if cur_price <= 0:
                        continue

                    pnl_pct = (cur_price - entry_price) / entry_price * 100

                    # ── Досрочный выход по SELL-сигналу ─────────────────────
                    sig = self.signals.get(sym, {})
                    if (sig.get('signal') == 'SELL'
                            and sig.get('confidence', 0) >= self.SELL_CLOSE_CONF
                            and pnl_pct > 0.5):
                        coin_bal = self.trader.get_coin_balance(sym.replace("USDT", ""))
                        if coin_bal > 0:
                            self._sw_closing.add(sym)
                            ok = self.executor.place_order(sym, "SELL", 0, 0, 0)
                            if ok:
                                # ── Bug #3 FIX: очищаем state ────────────────
                                self.breakeven_activated.pop(sym, None)
                                self._trail_best.pop(sym, None)
                                self._sw_closing.discard(sym)
                                self.portfolio.close_position(sym)
                                log_close(sym, "EARLY_EXIT", pnl_pct,
                                          entry_price=entry_price, exit_price=cur_price,
                                          mode='REAL' if self.real_mode else 'DEMO')
                                print(f"✅ Досрочный выход {sym} +{pnl_pct:.2f}%")
                            else:
                                self._sw_closing.discard(sym)
                        continue

                    # ── Trail / Breakeven ─────────────────────────────────────
                    pair_cfg   = self.pairs_config.get(sym, {})
                    be_trigger = pair_cfg.get('breakeven_trigger', self.BREAKEVEN_TRIGGER)
                    trail_pct  = pair_cfg.get('trailing_pct', self.TRAILING_PCT)

                    if pnl_pct >= be_trigger:
                        if not self.breakeven_activated.get(sym):
                            self.breakeven_activated[sym] = True
                            print(f"📈 {sym}: +{pnl_pct:.2f}% → безубыток")

                    if self.breakeven_activated.get(sym):
                        # Обновляем лучшую цену
                        best = self._trail_best.get(sym, entry_price)
                        if cur_price > best * (1 + self.TRAILING_MIN_MOVE / 100):
                            self._trail_best[sym] = cur_price
                            best = cur_price

                        # ── Trail floor FIX: SL не ниже entry * (1 + floor) ──
                        floor_pct   = 0.008   # 0.8% над entry
                        trail_floor = entry_price * (1 + floor_pct / 100)
                        trail_sl    = best * (1 - trail_pct / 100)
                        trail_sl    = max(trail_sl, trail_floor)

                        if cur_price <= trail_sl:
                            coin_bal = self.trader.get_coin_balance(sym.replace("USDT", ""))
                            if coin_bal > 0:
                                self._sw_closing.add(sym)
                                ok = self.executor.place_order(sym, "SELL", 0, 0, 0)
                                if ok:
                                    self.breakeven_activated.pop(sym, None)
                                    self._trail_best.pop(sym, None)
                                    self._sw_closing.discard(sym)
                                    self.portfolio.close_position(sym)
                                    log_close(sym, "TRAIL", pnl_pct,
                                              entry_price=entry_price, exit_price=cur_price,
                                              mode='REAL' if self.real_mode else 'DEMO')
                                    print(f"📉 ТРЕЙЛИНГ SL {sym}: {cur_price:.4f} ≤ {trail_sl:.4f}"
                                          f"  (best={best:.4f}, +{pnl_pct:.2f}%)")
                                    print(f"  ✅ {sym} закрыт программно (TRAIL, +{pnl_pct:.2f}%)")
                                else:
                                    self._sw_closing.discard(sym)

                # ── Sync loop: восстанавливаем позиции ───────────────────────
                try:
                    wb = self.trader.get_wallet_balance()
                    if wb.get("retCode") == 0:
                        coins_held = {}
                        for ci in wb["result"]["list"][0]["coin"]:
                            coin = ci.get("coin", "")
                            if coin == "USDT":
                                continue
                            sym = coin + "USDT"
                            if sym not in self.active_pairs:
                                continue
                            try:
                                bal = float(ci.get("walletBalance") or 0)
                                uv  = float(ci.get("usdValue")      or 0)
                            except Exception:
                                continue
                            if bal > 0 and uv > 1.0:
                                coins_held[sym] = uv

                        for sym, uv in coins_held.items():
                            if sym in self._sw_closing:  # Ghost v1 FIX
                                continue
                            if not self.portfolio.is_open(sym):
                                ep = self.executor._restore_entry_price(sym)
                                self.portfolio.open_position(sym, uv, ep)
                                print(f"♻️  Восстановлена позиция {sym} entry={ep:.4f}")

                        # Закрываем ghost-позиции (монет нет на балансе)
                        for sym in list(self.portfolio.open_positions.keys()):
                            if sym in self._sw_closing:
                                continue
                            if sym not in coins_held:
                                ids = self.open_order_ids.get(sym, {})
                                tp_id = ids.get('tp', '')
                                sl_id = ids.get('sl', '')
                                reason = self.executor._detect_close_reason(sym, tp_id, sl_id)
                                print(f"ℹ️  Позиция {sym} закрылась ({reason})")
                                self.executor._cancel_tp_sl(sym)
                                self.breakeven_activated.pop(sym, None)
                                self._trail_best.pop(sym, None)
                                self._sw_closing.discard(sym)
                                self.portfolio.close_position(sym)

                except Exception as e:
                    print(f"⚠️ Sync loop: {e}")

                current_data['portfolio'] = self.portfolio.get_portfolio_summary()

            except Exception as e:
                print(f"❌ monitor_positions: {e}")

            time.sleep(MONITOR_INTERVAL_SEC)

    # ─────────────────────────────────────────────────────────────────────────
    # Шорты (verbatim v1, требует Cross Margin включён на Bybit)
    # ─────────────────────────────────────────────────────────────────────────

    def _execute_short_signal(self, symbol: str, signal: dict, current_price: float):
        """Заглушка для шортов — полная реализация аналогична v1 open_short()."""
        pair_cfg = self.pairs_config.get(symbol, {})
        tp_pct   = pair_cfg.get('tp_pct', 3.5)
        sl_pct   = pair_cfg.get('sl_pct', 3.0)
        print(f"👀 СИГНАЛ SELL {symbol} conf={signal['confidence']*100:.1f}% — НАБЛЮДЕНИЕ")
        # TODO: перенести open_short() из main.py v1

    # ─────────────────────────────────────────────────────────────────────────
    # Параметры пар
    # ─────────────────────────────────────────────────────────────────────────

    def get_pair_tp_sl(self, symbol: str) -> tuple:
        """Возвращает (tp_pct, sl_pct) для пары из pairs_config."""
        cfg = self.pairs_config.get(symbol, {})
        tp  = cfg.get('tp_pct',  current_data.get('settings_tp',  3.5))
        sl  = cfg.get('sl_pct',  current_data.get('settings_sl',  3.0))
        return float(tp), float(sl)

    # ─────────────────────────────────────────────────────────────────────────
    # Персистентность order IDs
    # ─────────────────────────────────────────────────────────────────────────

    def _order_ids_path(self) -> str:
        mode = 'real' if self.real_mode else 'demo'
        return os.path.join(ROOT, f'order_ids_{mode}.json')

    def _load_order_ids(self) -> dict:
        path = self._order_ids_path()
        if not os.path.exists(path):
            return {}
        try:
            with open(path, 'r', encoding='utf-8') as f:
                data = json.load(f)
            valid = {sym: ids for sym, ids in data.items()
                     if ids.get('tp') or ids.get('sl')}
            if valid:
                print(f"📌 Загружены order_ids: {list(valid.keys())}")
            return valid
        except Exception as e:
            print(f"⚠️ _load_order_ids: {e}")
            return {}

    # ─────────────────────────────────────────────────────────────────────────
    # Настройки
    # ─────────────────────────────────────────────────────────────────────────

    def _load_bot_settings(self):
        """Загружает bot_settings.json при старте."""
        if not os.path.exists(BOT_SETTINGS_PATH):
            return
        try:
            with open(BOT_SETTINGS_PATH, 'r', encoding='utf-8') as f:
                s = json.load(f)
            if s.get('max_positions'):
                self.portfolio.max_positions    = int(s['max_positions'])
            if s.get('position_divider'):
                self.portfolio.position_divider = int(s['position_divider'])
            if s.get('breakeven_trigger'):
                self.BREAKEVEN_TRIGGER = float(s['breakeven_trigger'])
            if s.get('trailing_pct'):
                self.TRAILING_PCT = float(s['trailing_pct'])
            if s.get('sell_close_conf'):
                self.SELL_CLOSE_CONF = float(s['sell_close_conf'])
            if s.get('min_confidence') is not None:
                current_data['min_confidence'] = float(s['min_confidence'])
            if s.get('shorts_enabled') is not None:
                self.shorts_enabled = bool(s['shorts_enabled'])
            print(f"⚙️  bot_settings.json: maxPos={self.portfolio.max_positions}"
                  f" div={self.portfolio.position_divider}"
                  f" BE={self.BREAKEVEN_TRIGGER}%"
                  f" TRAIL={self.TRAILING_PCT}%")
        except Exception as e:
            print(f"⚠️ _load_bot_settings: {e}")

    # ─────────────────────────────────────────────────────────────────────────
    # Запуск фоновых задач
    # ─────────────────────────────────────────────────────────────────────────

    def _start_background_tasks(self):
        """Запускает потоки мониторинга."""
        t_monitor = threading.Thread(target=self.monitor_positions, daemon=True)
        t_monitor.start()
        print("✅ Поток monitor_positions запущен")

    def stop(self):
        self.running = False
        print("\n🛑 Бот остановлен")
