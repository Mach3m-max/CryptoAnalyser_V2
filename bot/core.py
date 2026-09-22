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
from datetime import datetime, timedelta
from decimal import Decimal

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
from logging_v2 import log_decision, log_close, log_candle, signal_snapshot, ensure_today_schema

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

        try:
            ensure_today_schema()
        except Exception as e:
            print(f"⚠️ Проверка схемы decisions-лога: {e}")

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
        self.short_positions:  dict = {}   # {symbol: {qty, entry, usdt_amt, tp_pct, sl_pct, open_time}}
        self._entry_confidence: dict = {}  # {symbol: confidence на момент открытия лонга} — для анализа калибровки
        self._short_close_attempts: dict = {}  # {symbol: подряд идущих попыток авто-закрытия}
        self._short_close_blocked:  set  = set()  # символы, для которых авто-закрытие временно остановлено
        self._short_recent_closes:  dict = {}  # {symbol: [datetime закрытий за последние WHIPSAW_WINDOW_MIN]}
        self._short_cooldown_until: dict = {}  # {symbol: datetime, до которого новые шорты по паре заблокированы}
        self.WHIPSAW_WINDOW_MIN   = 30  # окно, за которое считаем повторные закрытия
        self.WHIPSAW_MAX_CLOSES   = 3   # сколько закрытий за окно считаем "пилой"
        self.WHIPSAW_COOLDOWN_MIN = 60  # на сколько блокируем новые открытия после срабатывания
        self._sync_ready:      bool = False  # True после первого цикла discovery в monitor_positions

        # ── Параметры стратегии из bot_settings.json ──────────────────────────
        self.BREAKEVEN_TRIGGER    = BREAKEVEN_TRIGGER
        self.TRAILING_PCT         = TRAILING_PCT
        self.TRAILING_MIN_MOVE    = TRAILING_MIN_MOVE
        self.SELL_CLOSE_CONF      = SELL_CLOSE_CONF
        self.SHORT_MIN_CONFIDENCE = 0.75  # отдельный, обычно более строгий порог для шортов
        self.shorts_enabled       = False
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
        current_data['short_positions'] = self.short_positions  # ссылка — мутации видны сразу
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
                        signal = self.strategy.analyze_pair(sym, df, prices.get(sym, 0))
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

        sig_type = signal['signal']

        if sig_type == 'BUY':
            if conf < min_conf:
                return
            self._execute_buy(symbol, signal, current_price, pair_cfg)
        elif sig_type == 'SELL' and self.shorts_enabled:
            # ── Отдельный, обычно более строгий порог для шортов ─────────────
            if conf < self.SHORT_MIN_CONFIDENCE:
                return
            self._execute_short_signal(symbol, signal, current_price)

    def _execute_buy(self, symbol: str, signal: dict,
                     current_price: float, pair_cfg: dict):
        """Исполнение BUY сигнала."""
        if not self._sync_ready:
            print(f"⏸️ {symbol}: пропущен — ждём первый цикл sync-loop (защита от дублей после рестарта)")
            return

        can, reason = self.portfolio.can_open(symbol)
        if not can:
            print(f"⏸️ {symbol}: пропущен — {reason}")
            return

        # ── Запрет встречной позиции по тому же активу (лонг поверх шорта) ───
        if symbol in self.short_positions:
            print(f"⏸️ {symbol}: пропущен — уже открыт ШОРТ по этому активу")
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
            self._entry_confidence[symbol] = signal['confidence']

            log_decision(
                symbol=symbol, event_type='POSITION_OPENED',
                signal='BUY', confidence=signal['confidence'],
                price=current_price, tp_pct=tp_pct, sl_pct=sl_pct,
                usdt_amount=usdt_amount,
                portfolio_open_count=self.portfolio.open_count(),
                total_capital=self.portfolio.total_capital,
                mode='REAL' if self.real_mode else 'DEMO',
                **signal_snapshot(signal),
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
                                usdt_amount = pos.get('usdt_amount', 0)
                                pnl_usdt    = usdt_amount * pnl_pct / 100
                                log_close(sym, "EARLY_EXIT", pnl_pct, pnl_usdt,
                                          entry_price=entry_price, exit_price=cur_price,
                                          mode='REAL' if self.real_mode else 'DEMO',
                                          confidence=self._entry_confidence.get(sym, 0),
                                          **signal_snapshot(sig))
                                self._entry_confidence.pop(sym, None)
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
                                    usdt_amount = pos.get('usdt_amount', 0)
                                    pnl_usdt    = usdt_amount * pnl_pct / 100
                                    log_close(sym, "TRAIL", pnl_pct, pnl_usdt,
                                              entry_price=entry_price, exit_price=cur_price,
                                              mode='REAL' if self.real_mode else 'DEMO',
                                              confidence=self._entry_confidence.get(sym, 0),
                                              **signal_snapshot(self.signals.get(sym, {})))
                                    self._entry_confidence.pop(sym, None)
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
                                # ── FIX: ищем реальные TP/SL на бирже ────────
                                # Без них: (а) не можем понять причину закрытия
                                #          (б) не можем отличить позицию от пыли/остатка
                                tp_id, sl_id = "", ""
                                try:
                                    r_tp = self.trader._request("GET", "/v5/order/realtime", {
                                        "category": "spot", "symbol": sym, "orderFilter": "Order"})
                                    for o in r_tp.get("result", {}).get("list", []):
                                        if o.get("side") == "Sell":
                                            tp_id = o.get("orderId", "")
                                            break
                                    r_sl = self.trader._request("GET", "/v5/order/realtime", {
                                        "category": "spot", "symbol": sym, "orderFilter": "StopOrder"})
                                    for o in r_sl.get("result", {}).get("list", []):
                                        if o.get("side") == "Sell":
                                            sl_id = o.get("orderId", "")
                                            break
                                    # FIX: с переходом на единый OCO-ордер (см.
                                    # order_executor.place_order) обычные TP/SL
                                    # фильтры выше его не находят — без этой
                                    # проверки позиция, защищённая OCO, выглядела
                                    # бы "без защиты" и вообще не восстанавливалась.
                                    if not tp_id and not sl_id:
                                        r_oco = self.trader._request("GET", "/v5/order/realtime", {
                                            "category": "spot", "symbol": sym, "orderFilter": "OcoOrder"})
                                        for o in r_oco.get("result", {}).get("list", []):
                                            if o.get("side") == "Sell":
                                                tp_id = sl_id = o.get("orderId", "")
                                                break
                                except Exception as e:
                                    print(f"⚠️ Поиск TP/SL для {sym}: {e}")

                                if not tp_id and not sl_id:
                                    # Нет TP/SL → это не сделка бота (пыль, остаток,
                                    # или ручная докупка/погашение займа) — не позиция,
                                    # независимо от суммы в USD
                                    continue

                                ep = self.executor._restore_entry_price(sym)
                                self.portfolio.open_position(sym, uv, ep)
                                self.open_order_ids[sym] = {"tp": tp_id, "sl": sl_id}
                                self.executor._save_order_ids()
                                print(f"♻️  Восстановлена позиция {sym} entry={ep:.4f}"
                                      f"  [TP/SL найдены на бирже]")

                        # ── FIX: обнаруживаем шорты, открытые НЕ ботом (вручную на бирже) ──
                        # borrowAmount > 0 по монете = она реально занята (шорт).
                        # ВАЖНО: используем именно borrowAmount, а не equity/walletBalance —
                        # equity показывает НЕТТО (спотовый остаток минус долг), поэтому если
                        # на споте случайно есть хоть немного той же монеты, equity занижает
                        # реальный долг. borrowAmount — это именно "Сумма займа" с биржи.
                        for ci in wb["result"]["list"][0]["coin"]:
                            coin = ci.get("coin", "")
                            if coin == "USDT":
                                continue
                            sym = coin + "USDT"
                            if sym not in self.active_pairs:
                                continue
                            if sym in self.short_positions or sym in self._sw_closing:
                                continue
                            try:
                                bal = float(ci.get("borrowAmount") or 0)
                            except Exception:
                                continue
                            if bal <= 0.0000001:
                                continue  # долга нет — это не шорт

                            # Пыль (доли доллара) — не регистрируем как позицию,
                            # иначе после close_short() неистребимый мелкий хвост
                            # долга будет бесконечно "воскресать" и закрываться заново
                            cur_price_check = current_data['prices'].get(sym, 0)
                            if cur_price_check > 0 and bal * cur_price_check < 1.0:
                                continue

                            entry = 0.0
                            try:
                                r_ex = self.trader._request("GET", "/v5/execution/list", {
                                    "category": "spot", "symbol": sym, "limit": 50})
                                if r_ex.get("retCode") == 0:
                                    for ex in r_ex["result"].get("list", []):
                                        if ex.get("side") == "Sell":
                                            p = float(ex.get("execPrice", 0) or 0)
                                            if p > 0:
                                                entry = p
                                                break
                            except Exception as e:
                                print(f"⚠️ Поиск цены входа шорта {sym}: {e}")
                            if entry <= 0:
                                entry = current_data['prices'].get(sym, 0)
                            if entry <= 0:
                                continue

                            raw_qty = abs(bal)
                            instr = self.instruments.get(sym)
                            if not instr:
                                # Кэш пуст — спрашиваем биржу напрямую (как в order_executor._calc_order_params)
                                try:
                                    info = self.trader._request("GET", "/v5/market/instruments-info",
                                                                {"category": "spot", "symbol": sym})
                                    lst = info.get("result", {}).get("list", []) if info.get("retCode") == 0 else []
                                    if lst:
                                        lf = lst[0].get("lotSizeFilter", {})
                                        instr = {"qty_step": lf.get("basePrecision", lf.get("qtyStep", "0.001"))}
                                        self.instruments[sym] = instr  # кэшируем на будущее
                                except Exception as e:
                                    print(f"⚠️ Запрос lot size {sym}: {e}")
                            step = float((instr or {}).get('qty_step', 0.001))
                            qd   = abs(Decimal(str(step)).as_tuple().exponent)
                            qty  = round(math.floor(raw_qty / step) * step, qd) if step > 0 else raw_qty

                            # ── FIX: игнорируем пыль (хвост от округления вниз при выкупе) ──
                            # Ниже разумного минимума ордера биржи ($5) — это не позиция, а мусор.
                            cur_price = current_data['prices'].get(sym, entry)
                            if qty * cur_price < 5.0:
                                continue

                            # ── FIX: предохранители от whipsaw/зацикленного закрытия должны
                            # действовать и здесь — иначе discovery-loop их полностью обходит
                            # (реальный инцидент: 5 подряд идущих реальных выкупов ARB, потому
                            # что каждое обнаружение выглядело как "новая" находка).
                            _cooldown = self._short_cooldown_until.get(sym)
                            if _cooldown and datetime.now() < _cooldown:
                                continue
                            if sym in self._short_close_blocked:
                                continue

                            tp_pct, sl_pct = self.get_pair_tp_sl(sym)
                            self.short_positions[sym] = {
                                'qty':       qty,
                                'entry':     entry,
                                'usdt_amt':  qty * entry,
                                'tp_pct':    tp_pct,
                                'sl_pct':    sl_pct,
                                'open_time': datetime.now().isoformat(),
                                'conf':      0,
                            }
                            print(f"♻️  Восстановлен ШОРТ {sym} entry={entry:.4f} qty={qty:.4f}"
                                  f"  (открыт не ботом — обнаружен по отрицательному балансу)")

                        # ── Убираем шорты, закрытые вручную (долга по монете больше нет) ──
                        for sym in list(self.short_positions.keys()):
                            if sym in self._sw_closing:
                                continue
                            coin = sym.replace("USDT", "")
                            bal_now = None
                            for ci in wb["result"]["list"][0]["coin"]:
                                if ci.get("coin") == coin:
                                    try:
                                        bal_now = float(ci.get("borrowAmount") or 0)
                                    except Exception:
                                        bal_now = None
                                    break
                            if bal_now is not None and bal_now <= 0.0000001:
                                print(f"♻️  Шорт {sym} закрыт вручную на бирже — убираю из учёта")
                                closed_short = self.short_positions.get(sym, {})
                                entry_s = closed_short.get('entry', 0)
                                exit_s  = current_data['prices'].get(sym, entry_s)
                                pnl_pct_s = ((entry_s - exit_s) / entry_s * 100
                                             if entry_s > 0 else 0)  # шорт: прибыль при падении
                                usdt_amt_s = closed_short.get('usdt_amt', 0)
                                pnl_usd_s  = usdt_amt_s * pnl_pct_s / 100
                                log_close(sym, "SHORT_MANUAL_EXT", pnl_pct_s, pnl_usd_s,
                                          entry_price=entry_s, exit_price=exit_s,
                                          mode='REAL' if self.real_mode else 'DEMO',
                                          confidence=closed_short.get('conf', 0),
                                          **signal_snapshot(self.signals.get(sym, {})))
                                self.short_positions.pop(sym, None)
                                self._short_close_attempts.pop(sym, None)
                                self._short_close_blocked.discard(sym)
                        for sym in list(self.portfolio.open_positions.keys()):
                            if sym in self._sw_closing:
                                continue
                            if sym not in coins_held:
                                ids = self.open_order_ids.get(sym, {})
                                tp_id = ids.get('tp', '')
                                sl_id = ids.get('sl', '')
                                reason = self.executor._detect_close_reason(sym, tp_id, sl_id)
                                print(f"ℹ️  Позиция {sym} закрылась ({reason})")

                                # ── FIX: логируем закрытие — раньше TP/SL вообще не попадали в decisions_*.csv ──
                                closed_pos  = self.portfolio.open_positions.get(sym, {})
                                entry_price = closed_pos.get('entry_price', 0)
                                usdt_amount = closed_pos.get('usdt_amount', 0)
                                exit_price  = current_data['prices'].get(sym, entry_price)
                                pnl_pct     = ((exit_price - entry_price) / entry_price * 100
                                               if entry_price > 0 else 0)
                                pnl_usdt    = usdt_amount * pnl_pct / 100
                                log_close(sym, reason, pnl_pct, pnl_usdt,
                                          entry_price=entry_price, exit_price=exit_price,
                                          mode='REAL' if self.real_mode else 'DEMO',
                                          confidence=self._entry_confidence.get(sym, 0),
                                          **signal_snapshot(self.signals.get(sym, {})))
                                self._entry_confidence.pop(sym, None)

                                self.executor._cancel_tp_sl(sym)
                                self.breakeven_activated.pop(sym, None)
                                self._trail_best.pop(sym, None)
                                self._sw_closing.discard(sym)
                                self.portfolio.close_position(sym)

                except Exception as e:
                    print(f"⚠️ Sync loop: {e}")

                if not self._sync_ready:
                    self._sync_ready = True
                    print("✅ Sync-loop готов — известны все реальные позиции на бирже, торговля разрешена")

                current_data['portfolio'] = self.portfolio.get_portfolio_summary()

            except Exception as e:
                print(f"❌ monitor_positions: {e}")

            time.sleep(MONITOR_INTERVAL_SEC)

    # ─────────────────────────────────────────────────────────────────────────
    # Шорты (Spot Margin: сигнал SELL без баланса → займ → продажа → TP/SL → выкуп/погашение)
    # ─────────────────────────────────────────────────────────────────────────

    def _execute_short_signal(self, symbol: str, signal: dict, current_price: float):
        """Точка входа при сигнале SELL и включённых шортах."""
        self.open_short(symbol, signal)

    def open_short(self, symbol: str, signal: dict,
                   usdt_amount: float = None, tp_pct: float = None, sl_pct: float = None) -> bool:
        """
        Открывает Spot Margin шорт:
          1. place_order(Sell, isLeverage=1) — Bybit автоматически занимает монету
          2. Записываем в self.short_positions (отдельно от self.portfolio.open_positions)

        usdt_amount/tp_pct/sl_pct — опциональные ручные значения (ручная продажа
        через дашборд). Если не переданы — считаются автоматически, как для бота.
        """
        if not self._sync_ready:
            print(f"  ⏭️  {symbol}: пропущен — ждём первый цикл sync-loop (защита от дублей после рестарта)")
            return False
        if not self.shorts_enabled:
            print(f"  ⏭️  {symbol}: шорты выключены")
            return False

        cooldown = self._short_cooldown_until.get(symbol)
        if cooldown and datetime.now() < cooldown:
            remaining = (cooldown - datetime.now()).total_seconds() / 60
            print(f"  ⏭️  {symbol}: пропущен — whipsaw-защита ещё ~{remaining:.0f} мин "
                  f"(было {self.WHIPSAW_MAX_CLOSES}+ закрытий подряд)")
            return False

        if symbol in self.short_positions:
            print(f"  ⏭️  {symbol}: шорт уже открыт")
            return False

        # ── Запрет встречной позиции по тому же активу (шорт поверх лонга) ───
        if self.portfolio.is_open(symbol):
            print(f"  ⏭️  {symbol}: пропущен — уже открыт ЛОНГ по этому активу")
            return False

        # ── Независимый лимит шортов — по position_divider, отдельно от лонгов ──
        if len(self.short_positions) >= self.portfolio.position_divider:
            print(f"  ⏭️  {symbol} short: пропущен — лимит шортов "
                  f"({self.portfolio.position_divider}) уже достигнут")
            return False

        price = current_data['prices'].get(symbol, 0)
        if price <= 0:
            print(f"  ⚠️  {symbol} short: нет цены")
            return False

        if tp_pct is None or sl_pct is None:
            auto_tp, auto_sl = self.get_pair_tp_sl(symbol)
            tp_pct = tp_pct if tp_pct is not None else auto_tp
            sl_pct = sl_pct if sl_pct is not None else auto_sl

        usdt_bal = self.trader.get_coin_balance('USDT')

        if usdt_amount is not None:
            # ── Ручная продажа через дашборд — берём сумму как есть ──────────
            usdt_amt = min(usdt_amount, usdt_bal * 0.95)
        else:
            # ── Автоматика — та же логика, что и лонг ────────────────────────
            usdt_amt = min(
                self.portfolio.total_capital / self.portfolio.position_divider,
                usdt_bal * 0.95
            )
        if usdt_amt < 5.5:
            print(f"  ⚠️  {symbol} short: недостаточно USDT ({usdt_amt:.2f})")
            return False

        instr   = self.instruments.get(symbol, {})
        step    = float(instr.get('qty_step', 0.001))
        qty_dec = abs(Decimal(str(step)).as_tuple().exponent)
        qty     = round(math.floor(usdt_amt / price / step) * step, qty_dec) if price > 0 else 0

        if qty <= 0:
            print(f"  ⚠️  {symbol} short: qty=0  "
                  f"(usdt_amt={usdt_amt:.2f}, usdt_bal={usdt_bal:.2f}, "
                  f"price={price:.6f}, step={step})")
            return False

        print(f"\n📉 ОТКРЫВАЕМ ШОРТ {symbol}  qty={qty}  @{price:.4f}"
              f"  TP={tp_pct}%  SL={sl_pct}%")

        # ── Диагностика: сколько биржа реально готова занять по этой монете ──
        try:
            bq = self.trader.check_borrow_quota(symbol, 'Sell', 'Market', qty=str(qty))
            print(f"  🔍 check_borrow_quota {symbol}: {bq}")
        except Exception as e:
            print(f"  ⚠️ check_borrow_quota ошибка: {e}")

        result = self.trader.place_order(
            category='spot', symbol=symbol,
            side='Sell', order_type='Market',
            qty=str(qty),
            is_leverage=1,          # ← ключевой флаг для Spot Margin шорта
        )

        if result.get('retCode') == 0:
            self.short_positions[symbol] = {
                'qty':       qty,
                'entry':     price,
                'usdt_amt':  usdt_amt,
                'tp_pct':    tp_pct,
                'sl_pct':    sl_pct,
                'open_time': datetime.now().isoformat(),
                'conf':      signal.get('confidence', 0),
            }
            self._short_close_attempts.pop(symbol, None)
            self._short_close_blocked.discard(symbol)
            print(f"  ✅ Шорт открыт: {symbol}  qty={qty}  @{price:.4f}")
            log_decision(symbol=symbol, event_type='SHORT_OPENED',
                signal='SELL', confidence=signal.get('confidence', 0),
                price=price, reason=f'Spot Margin Short qty={qty}',
                portfolio_open_count=len(self.portfolio.open_positions),
                total_capital=self.portfolio.total_capital,
                mode='REAL' if self.real_mode else 'DEMO',
                **signal_snapshot(signal))
            return True
        else:
            err = result.get('retMsg', '?')
            print(f"  ❌ Ошибка открытия шорта {symbol}: {err}")
            return False

    def close_short(self, symbol: str, reason: str):
        """
        Закрывает Spot Margin шорт:
          1. place_order(Buy, isLeverage=1) — покупаем обратно
          2. Bybit автоматически погашает займ из выкупленных монет
        """
        pos = self.short_positions.get(symbol)
        if not pos:
            return

        price   = current_data['prices'].get(symbol, 0)
        qty     = pos['qty']
        entry   = pos['entry']
        pnl_pct = (entry - price) / entry * 100 if entry > 0 else 0  # шорт: прибыль при падении цены
        pnl_usd = pos['usdt_amt'] * pnl_pct / 100

        # ── FIX: берём АКТУАЛЬНЫЙ долг с биржи — за время удержания шорта могли
        # набежать проценты по займу, и записанный при открытии qty уже неточен.
        # Округляем ВВЕРХ, чтобы гарантированно закрыть весь долг, а не оставить хвост.
        # ВАЖНО: используем equity, а не walletBalance — на REAL (Unified/кросс-маржа)
        # walletBalance не отражает реальный долг (может показывать ~0 при долге в сотни
        # монет), а equity показывает точную величину. Ошибка здесь означает, что бот
        # выкупает почти ничего и ложно считает шорт закрытым, оставляя реальный долг.
        coin = symbol.replace("USDT", "")
        try:
            wb = self.trader.get_wallet_balance()
            if wb.get("retCode") == 0:
                for ci in wb["result"]["list"][0]["coin"]:
                    if ci.get("coin") == coin:
                        bal = float(ci.get("borrowAmount") or 0)
                        if bal > 0.0000001:
                            instr = self.instruments.get(symbol, {})
                            step  = float(instr.get('qty_step', 0.001))
                            qd    = abs(Decimal(str(step)).as_tuple().exponent)
                            # Запас на комиссию покупки (списывается из купленной монеты,
                            # обычно ~0.1% — берём с двойным запасом, 0.3%, дешевле чем
                            # разбираться с очередным хвостом-должком)
                            target = bal * 1.003
                            qty    = (round(math.ceil(target / step) * step, qd)
                                     if step > 0 else target)
                        break
        except Exception as e:
            print(f"⚠️ Не удалось получить актуальный долг {symbol}: {e}")

        print(f"\n📈 ЗАКРЫВАЕМ ШОРТ {symbol}  {reason}"
              f"  entry={entry:.4f} → now={price:.4f}"
              f"  PnL={pnl_usd:+.4f} USDT ({pnl_pct:+.2f}%)  qty_к_выкупу={qty}")

        result = self.trader.place_order(
            category='spot', symbol=symbol,
            side='Buy', order_type='Market',
            qty=str(qty),
            market_unit='baseCoin',   # FIX: без этого qty читается как USDT, а не как кол-во монеты!
            is_leverage=1,
        )

        if result.get('retCode') == 0:
            print(f"  ✅ Шорт закрыт: {symbol} ({reason}) {pnl_pct:+.2f}%")

            # ── FIX: явно гасим заём после выкупа ────────────────────────────
            # На некоторых режимах маржи (напр. Portfolio Margin) простая покупка
            # монеты НЕ засчитывается в счёт долга автоматически — нужен отдельный
            # вызов repay. Безвреден, даже если долг уже погашен покупкой (DEMO).
            #
            # ВАЖНО: один вызов repay иногда оставляет крошечный хвост долга
            # (округление на стороне биржи) — если его не добить, discovery-loop
            # на следующем цикле снова находит этот остаток, регистрирует его как
            # "новую" позицию, и monitor_shorts() закрывает её заново — теряя
            # комиссию/спред на пустом месте, много раз подряд. Поэтому гасим
            # в цикле, пока долг не станет нулевым (или не кончатся попытки).
            #
            # FIX: используем /v5/account/no-convert-repay БЕЗ параметра amount.
            # Раньше передавали точную сумму (текущий borrowAmount) в /v5/account/repay —
            # но комиссия за покупку списывается в ТОЙ ЖЕ монете, что и долг, поэтому
            # реально доступный баланс чуть МЕНЬШЕ расчётного долга. Биржа отвечала
            # "The repayment trial calculation failed" и погашение проваливалось
            # систематически (подтверждено на реальных инцидентах ARB и WIF).
            # Без amount биржа сама гасит на всю доступную сумму спот-баланса монеты —
            # это ровно то, что нам нужно, без риска промахнуться мимо точной цифры.
            try:
                # Пауза перед первой попыткой: биржа не всегда мгновенно учитывает
                # только что купленную монету в "доступном" споt-балансе (settlement
                # lag) — no-convert-repay использует именно available-баланс, поэтому
                # попытка погасить СРАЗУ после покупки может упереться в "Repayment
                # unsuccessful", хотя монета уже видна в общем балансе кошелька.
                time.sleep(1.5)
                repaid = False
                for attempt in range(3):
                    fresh_bal = 0.0
                    wb2 = self.trader.get_wallet_balance()
                    if wb2.get("retCode") == 0:
                        for ci2 in wb2["result"]["list"][0]["coin"]:
                            if ci2.get("coin") == coin:
                                fresh_bal = float(ci2.get("borrowAmount") or 0)
                                break

                    # Микрохвосты долга (остаток ниже минимума заимствования для монеты)
                    # могут вызвать "The loan quantity cannot be less than the minimum order quantity"
                    # при попытке закрыть. Считаем такие хвосты "погашенными" и не трогаем.
                    MIN_REPAY_THRESHOLD = {
                        'TIA': 0.001,
                        'APT': 0.001,
                        'ARB': 0.001,
                        'WIF': 0.01,
                        'USDT': 0.01,
                    }
                    threshold = MIN_REPAY_THRESHOLD.get(coin, 0.001)
                    
                    if fresh_bal <= threshold:
                        repaid = True
                        if attempt == 0:
                            print(f"  💳 Займ {coin} уже погашен покупкой (долга не осталось)")
                        else:
                            print(f"  💳 Займ {coin} полностью погашен (попытка {attempt + 1})")
                        break

                    r_repay = self.trader._request("POST", "/v5/account/no-convert-repay",
                                                   {"coin": coin, "repaymentType": "FLEXIBLE"})
                    if r_repay.get('retCode') == 0:
                        status = r_repay.get('result', {}).get('resultStatus', '?')
                        print(f"  💳 Погашение {coin} отправлено (статус {status}), попытка {attempt + 1}")
                    else:
                        print(f"  ⚠️ Явное погашение {coin} (попытка {attempt + 1}) не удалось: "
                              f"{r_repay.get('retMsg')}")
                        if attempt < 2:
                            time.sleep(2 * (attempt + 1))  # растущая пауза — ошибка может быть временной

                if not repaid:
                    print(f"  ⚠️ После нескольких попыток по {coin} всё ещё остаётся остаточный долг — "
                          f"проверьте /balances вручную")
            except Exception as e:
                print(f"  ⚠️ Ошибка явного погашения {coin}: {e}")

            log_close(symbol, f"SHORT_{reason}", pnl_pct, pnl_usd,
                      entry_price=entry, exit_price=price,
                      mode='REAL' if self.real_mode else 'DEMO',
                      confidence=pos.get('conf', 0),
                      **signal_snapshot(self.signals.get(symbol, {})))
            del self.short_positions[symbol]

            # ── Whipsaw-предохранитель: N закрытий по паре за короткое окно ──
            # Реальный инцидент: ARBUSDT открывался и стопился 6 раз за 5 минут —
            # каждое открытие было "честным" (позиция реально закрывалась через SL),
            # поэтому старый предохранитель (для неполного закрытия) не срабатывал.
            now = datetime.now()
            hist = self._short_recent_closes.setdefault(symbol, [])
            hist.append(now)
            cutoff = now - timedelta(minutes=self.WHIPSAW_WINDOW_MIN)
            hist[:] = [t for t in hist if t > cutoff]
            if len(hist) >= self.WHIPSAW_MAX_CLOSES:
                until = now + timedelta(minutes=self.WHIPSAW_COOLDOWN_MIN)
                self._short_cooldown_until[symbol] = until
                self._short_close_blocked.add(symbol)
                print(f"\n🛑 {symbol}: {len(hist)} закрытий шорта за "
                      f"{self.WHIPSAW_WINDOW_MIN} мин (whipsaw) — новые открытия "
                      f"заблокированы до {until.strftime('%H:%M:%S')}\n")
        else:
            err = result.get('retMsg', '?')
            print(f"  ❌ Ошибка закрытия шорта {symbol}: {err}")

    def _try_close_short(self, symbol: str, reason: str):
        """
        Обёртка над close_short() с предохранителем от бесконечного цикла.
        Реальный инцидент: один и тот же шорт "закрывался" (или падал с
        Insufficient balance) десятки раз подряд каждые 15 сек часами —
        то ли из-за неполного погашения и повторного обнаружения остатка
        discovery-loop-ом, то ли биржа просто отказывала раз за разом.
        В обоих случаях продолжать долбить биржу автоматически не нужно —
        после 3 подряд попыток по одному символу останавливаемся и громко
        просим разобраться руками (см. /positions, /balances).
        """
        if symbol in self._short_close_blocked:
            return  # уже остановлено — ждём ручного вмешательства

        self._short_close_attempts[symbol] = self._short_close_attempts.get(symbol, 0) + 1
        attempts = self._short_close_attempts[symbol]

        if attempts > 3:
            self._short_close_blocked.add(symbol)
            print(f"\n🛑 {symbol}: {attempts} подряд авто-попыток закрытия шорта — "
                  f"ОСТАНАВЛИВАЮ автозакрытие для этой пары.")
            print(f"   Проверьте /balances (займ мог остаться непогашенным) и "
                  f"закройте/погасите вручную. Автоматика по {symbol} возобновится "
                  f"после ручного закрытия или перезапуска бота.\n")
            return

        self.close_short(symbol, reason)

        # Если позиция реально пропала из учёта — считаем успех, сбрасываем счётчик
        if symbol not in self.short_positions:
            self._short_close_attempts.pop(symbol, None)

    def check_discrepancies(self) -> dict:
        """
        Read-only сверка баланса, открытых ордеров и того, что бот считает
        своими позициями. Ничего не меняет на бирже — только отчёт для /balances.

        Два вида несоответствий:
          A) "Актив без обвязки" — на балансе есть монета (long или short через
             отрицательный баланс) дороже DUST_USD, но бот не считает её своей
             позицией. Может быть без TP/SL вовсе — риск без защиты.
          B) "Ордер-сирота" — открытый ордер на паре, которую бот не считает
             своей позицией (TP/SL от давно закрытой/пересозданной позиции).
             Реальный инцидент: такой сирота держал в резерве часть баланса,
             из-за чего бот решил, что свободных денег нет.
        """
        result = {'orphan_assets': [], 'orphan_orders': [], 'error': None}
        DUST_USD = 5.0
        try:
            wb = self.trader.get_wallet_balance()
            if wb.get('retCode') != 0:
                result['error'] = f"get_wallet_balance: {wb.get('retMsg')}"
                return result

            orders_resp = self.trader._request("GET", "/v5/order/realtime",
                                               {"category": "spot"})
            if orders_resp.get('retCode') != 0:
                result['error'] = f"order/realtime: {orders_resp.get('retMsg')}"
                return result

            open_orders = orders_resp.get('result', {}).get('list', [])
            orders_by_symbol: dict = {}
            for o in open_orders:
                orders_by_symbol.setdefault(o.get('symbol', ''), []).append(o)

            tracked = set(self.portfolio.open_positions.keys()) | set(self.short_positions.keys())

            # ── A) Активы без обвязки ────────────────────────────────────────
            for ci in wb['result']['list'][0]['coin']:
                coin = ci.get('coin', '')
                if coin == 'USDT':
                    continue
                sym = coin + 'USDT'
                if sym not in self.active_pairs or sym in tracked:
                    continue
                try:
                    bal = float(ci.get('walletBalance') or 0)
                except Exception:
                    continue
                if abs(bal) < 0.0000001:
                    continue
                price = current_data['prices'].get(sym, 0)
                usd_val = abs(bal) * price
                if usd_val < DUST_USD:
                    continue

                direction = 'LONG' if bal > 0 else 'SHORT'
                # Реальная точка входа из истории исполнений — LONG ищет
                # последний Buy, SHORT ищет последний Sell (открытие шорта).
                # Без этого auto_fix_discrepancies() был вынужден брать
                # текущую рыночную цену как условный вход, что давало кривой TP/SL.
                entry_price = 0.0
                try:
                    r_ex = self.trader._request("GET", "/v5/execution/list", {
                        "category": "spot", "symbol": sym, "limit": 50})
                    if r_ex.get("retCode") == 0:
                        wanted_side = "Buy" if direction == "LONG" else "Sell"
                        for ex in r_ex["result"].get("list", []):
                            if ex.get("side") == wanted_side:
                                p = float(ex.get("execPrice", 0) or 0)
                                if p > 0:
                                    entry_price = p
                                    break
                except Exception as e:
                    print(f"⚠️ Поиск точки входа {sym} для отчёта: {e}")

                result['orphan_assets'].append({
                    'symbol':        sym,
                    'balance':       round(bal, 6),
                    'usd_value':     round(usd_val, 2),
                    'direction':     direction,
                    'has_open_order': sym in orders_by_symbol,
                    'entry_price':   round(entry_price, 8) if entry_price > 0 else None,
                })

            # ── B) Ордера-сироты ──────────────────────────────────────────────
            for sym, orders in orders_by_symbol.items():
                if sym in tracked:
                    continue
                for o in orders:
                    result['orphan_orders'].append({
                        'symbol':       sym,
                        'side':         o.get('side', ''),
                        'order_type':   o.get('orderType', ''),
                        'qty':          o.get('qty', ''),
                        'price':        o.get('price', ''),
                        'order_id':     o.get('orderId', ''),
                    })
        except Exception as e:
            result['error'] = str(e)
        return result

    def auto_fix_discrepancies(self) -> dict:
        """
        Авто-починка несоответствий, найденных check_discrepancies():
          A) Голые LONG-активы без защиты → навешиваем TP/SL текущими default'ами
             пары и регистрируем как отслеживаемую позицию.
          B) Ордера-сироты → отменяем.

        SHORT-активы НЕ трогаем автоматически — там на кону реальный займ,
        а не просто актив на споте; ошибка в авто-логике там дороже. Такие
        остаются в отчёте для ручного разбора.

        Возвращает {'fixed_assets': [...], 'cancelled_orders': [...],
                     'skipped': [...], 'errors': [...]}
        """
        report = {'fixed_assets': [], 'cancelled_orders': [], 'skipped': [], 'errors': []}
        disc = self.check_discrepancies()
        if disc.get('error'):
            report['errors'].append(f"check_discrepancies: {disc['error']}")
            return report

        # ── A) Голые LONG-активы ────────────────────────────────────────────
        for a in disc.get('orphan_assets', []):
            sym = a['symbol']
            if a['direction'] != 'LONG':
                report['skipped'].append(f"{sym}: SHORT-актив — авто-починка отключена, нужен ручной разбор")
                continue
            if a['has_open_order']:
                report['skipped'].append(f"{sym}: уже есть какой-то открытый ордер — не трогаем во избежание дублирования защиты")
                continue
            try:
                tp_pct, sl_pct = self.get_pair_tp_sl(sym)
                real_entry = a.get('entry_price')  # None, если история исполнений не нашла Buy
                res = self.executor.attach_protection(sym, tp_pct, sl_pct, entry_override=real_entry)
                if res.get('success'):
                    self.portfolio.open_position(sym, res['usdt_amount'], res['entry'])
                    report['fixed_assets'].append({
                        'symbol': sym, 'qty': res['qty'], 'entry': res['entry'],
                        'tp_price': res['tp_price'], 'sl_price': res['sl_price'],
                    })
                    print(f"🔧 Авто-починка: {sym} защищён (TP={res['tp_price']} SL={res['sl_price']}) и зарегистрирован")
                else:
                    report['errors'].append(f"{sym}: {res.get('error')}")
            except Exception as e:
                report['errors'].append(f"{sym}: {e}")

        # ── B) Ордера-сироты ─────────────────────────────────────────────────
        for o in disc.get('orphan_orders', []):
            sym = o['symbol']
            order_id = o.get('order_id', '')
            if not order_id:
                continue
            try:
                r = self.trader._request("POST", "/v5/order/cancel", {
                    "category": "spot", "symbol": sym, "orderId": order_id,
                })
                if r.get('retCode') == 0:
                    report['cancelled_orders'].append({'symbol': sym, 'order_id': order_id})
                    print(f"🔧 Авто-починка: отменён ордер-сирота {sym} ({order_id[:8]}...)")
                else:
                    report['errors'].append(f"{sym} order {order_id[:8]}...: {r.get('retMsg')}")
            except Exception as e:
                report['errors'].append(f"{sym} order {order_id[:8]}...: {e}")

        return report

    def monitor_shorts(self):
        """
        Фоновый поток: мониторинг открытых шортов каждые 15 сек.
        Программный TP/SL/MAX_HOLD — не зависит от условных ордеров биржи.
        """
        MAX_HOLD_HOURS = 24   # максимум держим шорт 24 часа (проценты по займу растут)

        while self.running:
            try:
                if not self.shorts_enabled or not self.short_positions:
                    time.sleep(15)
                    continue

                for symbol, pos in list(self.short_positions.items()):
                    price = current_data['prices'].get(symbol, 0)
                    if price <= 0:
                        continue

                    entry   = pos['entry']
                    tp_pct  = pos['tp_pct']
                    sl_pct  = pos['sl_pct']
                    pnl_pct = (entry - price) / entry * 100

                    hold_h = (datetime.now() -
                              datetime.fromisoformat(pos['open_time'])
                              ).total_seconds() / 3600
                    if hold_h >= MAX_HOLD_HOURS:
                        print(f"⏰ {symbol} шорт: максимальное время {MAX_HOLD_HOURS}ч")
                        self._try_close_short(symbol, f'MAX_HOLD_{MAX_HOLD_HOURS}H')
                        continue

                    # TP: цена упала на tp_pct% — прибыль по шорту
                    if pnl_pct >= tp_pct:
                        self._try_close_short(symbol, 'TP')
                        continue

                    # SL: цена выросла на sl_pct% против шорта
                    if pnl_pct <= -sl_pct:
                        self._try_close_short(symbol, 'SL')
                        continue

                    # Разворотный BUY-сигнал высокой уверенности
                    sig = self.signals.get(symbol, {})
                    if sig.get('signal') == 'BUY' and sig.get('confidence', 0) >= 0.70:
                        self._try_close_short(symbol, 'BUY_SIGNAL')
                        continue

            except Exception as e:
                print(f"⚠️ monitor_shorts: {e}")
            time.sleep(15)

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
            if s.get('short_min_confidence') is not None:
                self.SHORT_MIN_CONFIDENCE = float(s['short_min_confidence'])
                current_data['short_min_confidence'] = self.SHORT_MIN_CONFIDENCE
            if s.get('min_confidence') is not None:
                current_data['min_confidence'] = float(s['min_confidence'])
            if s.get('shorts_enabled') is not None:
                self.shorts_enabled = bool(s['shorts_enabled'])
            print(f"⚙️  bot_settings.json: maxPos={self.portfolio.max_positions}"
                  f" div={self.portfolio.position_divider}"
                  f" BE={self.BREAKEVEN_TRIGGER}%"
                  f" TRAIL={self.TRAILING_PCT}%"
                  f" SHORT_CONF={self.SHORT_MIN_CONFIDENCE}")
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

        t_shorts = threading.Thread(target=self.monitor_shorts, daemon=True)
        t_shorts.start()
        print("✅ Поток monitor_shorts запущен")

    def stop(self):
        self.running = False
        print("\n🛑 Бот остановлен")
