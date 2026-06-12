# ═══════════════════════════════════════════════════════════════════════════════
# order_executor.py  —  HTT v2 / stable_core
#
# СТАТУС: VERBATIM COPY методов из main.py v1 (BotByBit),
#         выделен в отдельный класс OrderExecutor.
# ❌ ЛОГИКУ НЕ ИЗМЕНЯТЬ. Код отлажен в реальных рыночных условиях.
#
# Содержит:
#   OrderExecutor — класс-обёртка для всех методов работы с ордерами:
#     • place_order()           — BUY (Market + TP Limit + SL StopOrder)
#                                 SELL (cancel TP/SL + Market Sell)
#     • _calc_order_params()    — расчёт qty, step, tick для ордера
#     • _cancel_tp_sl()         — отмена TP + SL + cancel-all страховка
#     • _restore_entry_price()  — восстановление entry из execution/list
#     • _detect_close_reason()  — TP/SL/UNKNOWN по истории ордеров
#     • _move_sl_to_breakeven() — перенос SL на уровень безубытка
#     • _order_ids_path()       — путь к order_ids_{mode}.json
#     • _save_order_ids()       — атомарная запись на диск
#     • _load_order_ids()       — загрузка с диска при старте
#     • get_coin_balance()      — прокси к trader.get_coin_balance()
#
# Использование в v2:
#   from stable_core.order_executor import OrderExecutor
#   self.executor = OrderExecutor(
#       trader=self.trader,
#       instruments=self.instruments,
#       portfolio=self.portfolio,
#       open_order_ids=self.open_order_ids,
#       real_mode=self.real_mode,
#       current_data=current_data,
#       breakeven_activated=self.breakeven_activated,
#   )
#   success = self.executor.place_order(symbol, "BUY", 350.0, tp_pct=3.5, sl_pct=3.0)
# ═══════════════════════════════════════════════════════════════════════════════

import os
import math
import time
import json as _json
from datetime import datetime


class OrderExecutor:
    """
    Все методы работы с ордерами, извлечённые из TradingBot.
    Логика не изменена — только реорганизация в отдельный класс.

    Параметры:
        trader              — экземпляр BybitTrader
        instruments         — dict {symbol: {min_qty, qty_step, tick_size, ...}}
        portfolio           — экземпляр PortfolioManager
        open_order_ids      — dict {symbol: {tp: id, sl: id}}
        real_mode           — bool, True=REAL, False=DEMO
        current_data        — shared dict состояния бота (prices, trades, ...)
        breakeven_activated — dict {symbol: bool}
    """

    #: Смещение SL от entry при переносе на безубыток (покрывает комиссии ~0.28%)
    BREAKEVEN_OFFSET = 0.003

    def __init__(self, trader, instruments: dict, portfolio,
                 open_order_ids: dict, real_mode: bool,
                 current_data: dict, breakeven_activated: dict):
        self.trader               = trader
        self.instruments          = instruments
        self.portfolio            = portfolio
        self.open_order_ids       = open_order_ids
        self.real_mode            = real_mode
        self.current_data         = current_data
        self.breakeven_activated  = breakeven_activated

    # ─────────────────────────────────────────────────────────────────────────
    # Основной метод выставления ордеров
    # ─────────────────────────────────────────────────────────────────────────

    def place_order(self, symbol: str, side: str, usdt_amount: float,
                    tp_percent: float, sl_percent: float) -> bool:
        """
        BUY  — Market Buy → TP Limit → SL StopOrder
        SELL — cancel TP/SL → Market Sell

        Verbatim из main.py v1. НЕ ИЗМЕНЯТЬ.
        """
        try:
            # FIX: нормализуем side — дашборд шлёт 'BUY'/'SELL', API ждёт 'Buy'/'Sell'
            side = side.capitalize()

            params = self._calc_order_params(symbol, side, usdt_amount)
            if params is None:
                return False

            qty          = params['qty']
            qty_dec      = params['qty_dec']
            tick_dec     = params['tick_dec']
            market_unit  = params['market_unit']
            current_price= params['current_price']
            min_notional = params['min_notional']
            step         = params['step']
            min_qty      = params['min_qty']

            if side == "Buy":
                # ── Проверяем баланс USDT ─────────────────────────────────
                usdt_balance = self.trader.get_coin_balance("USDT")
                if usdt_balance == 0:
                    _wb = self.trader.get_wallet_balance()
                    if _wb.get("retCode") == 0:
                        for _c in _wb["result"]["list"][0]["coin"]:
                            if _c.get("coin") == "USDT":
                                print(f"   💰 USDT raw fields: {_c}")
                                for _fld in ("availableToWithdraw", "walletBalance",
                                             "availableToBorrow", "equity"):
                                    _v = _c.get(_fld)
                                    if _v not in (None, "", "0", 0):
                                        try:
                                            _f = float(_v)
                                            if _f > 0:
                                                usdt_balance = _f
                                                print(f"   💰 Баланс из поля '{_fld}': {_f:.2f}")
                                                break
                                        except (ValueError, TypeError):
                                            pass
                                break
                print(f"   💰 Итоговый баланс USDT: {usdt_balance:.2f}")
                if usdt_balance < usdt_amount:
                    print(f"❌ Недостаточно USDT: есть {usdt_balance:.2f}, нужно {usdt_amount:.2f}")
                    return False

                tp_price = round(current_price * (1 + tp_percent / 100), tick_dec)
                sl_price = round(current_price * (1 - sl_percent / 100), tick_dec)
                print(f"   TP={tp_price}  SL={sl_price}")

                usdt_amount_str = str(round(usdt_amount, 2))
                print(f"🔄 Покупка {symbol} на {usdt_amount_str} USDT...")
                buy_result = self.trader.place_order(
                    category="spot",
                    symbol=symbol,
                    side="Buy",
                    order_type="Market",
                    qty=usdt_amount_str,
                    market_unit="quoteCoin",
                )

                if buy_result.get("retCode") != 0:
                    print(f"❌ Ошибка покупки: {buy_result.get('retMsg')}")
                    return False

                order_id = buy_result["result"]["orderId"]
                print(f"✅ Куплено. ID: {order_id}")

                coin       = symbol.replace("USDT", "")
                filled_qty = 0.0
                for attempt in range(10):
                    time.sleep(1)
                    filled_qty = self.trader.get_filled_qty(symbol, order_id, "spot")
                    if filled_qty > 0:
                        print(f"   Исполнено (история): {filled_qty} {coin}")
                        break
                    bal = self.trader.get_coin_balance(coin)
                    if bal > 0:
                        filled_qty = bal
                        print(f"   Исполнено (баланс): {filled_qty} {coin}")
                        break
                    print(f"   Попытка {attempt+1}: ждём...")

                if filled_qty <= 0:
                    print(f"❌ Не удалось получить кол-во монет. Ордер {order_id} — ручная проверка.")
                    return False

                qty_rounded = round(math.floor(filled_qty / step) * step, qty_dec)
                if qty_rounded < min_qty:
                    print(f"❌ Куплено {qty_rounded} {coin} — меньше минимума {min_qty}")
                    return False

                print(f"   Количество: {qty_rounded} {coin}")

                # ── Ждём разблокировки монет (до 10 сек) ──────────────────
                coin_bal = 0.0
                for _attempt in range(10):
                    coin_bal = self.trader.get_coin_balance(coin)
                    if coin_bal >= qty_rounded * 0.98:
                        print(f"   ✅ Монеты доступны (попытка {_attempt+1}): {coin_bal} {coin}")
                        break
                    print(f"   ⏳ Попытка {_attempt+1}/10: баланс {coin} = {coin_bal:.6f}, ждём...")
                    time.sleep(1)
                else:
                    print(f"   ⚠️ Монеты не разлочились за 10 сек")

                actual_qty = (round(math.floor(coin_bal / step) * step, qty_dec)
                              if coin_bal > 0 else qty_rounded)
                if actual_qty < min_qty:
                    actual_qty = qty_rounded
                print(f"   Итоговое qty для TP/SL: {actual_qty} {coin}")

                # ── TP: лимитный ордер ПЕРВЫМ ─────────────────────────────
                # ВАЖНО: TP до SL — лимитный резервирует монеты.
                # Если сначала SL (StopOrder), он тоже резервирует баланс
                # и TP получит Insufficient balance.
                tp_result = self.trader.place_order(
                    category="spot",
                    symbol=symbol,
                    side="Sell",
                    order_type="Limit",
                    qty=str(actual_qty),
                    price=str(tp_price),
                    time_in_force="GTC",
                )
                tp_ok = tp_result.get("retCode") == 0
                print(f"   TP {tp_price}: {'✅ ID=' + tp_result['result'].get('orderId','') if tp_ok else '❌ ' + tp_result.get('retMsg','')}")
                if not tp_ok:
                    print(f"   ⚠️ TP не выставлен — SL будет отменён при закрытии позиции")

                # ── SL: StopOrder ВТОРЫМ ──────────────────────────────────
                # StopOrder на Bybit Spot НЕ резервирует баланс до срабатывания
                sl_result = self.trader._request("POST", "/v5/order/create", {
                    "category":     "spot",
                    "symbol":       symbol,
                    "side":         "Sell",
                    "orderType":    "Market",
                    "qty":          str(actual_qty),
                    "triggerPrice": str(sl_price),
                    "triggerBy":    "LastPrice",
                    "timeInForce":  "IOC",
                    "orderFilter":  "StopOrder",
                })
                sl_ok = sl_result.get("retCode") == 0
                print(f"   SL {sl_price}: {'✅ ID=' + sl_result.get('result',{}).get('orderId','') if sl_ok else '❌ ' + sl_result.get('retMsg','')}")

                tp_id = tp_result["result"].get("orderId", "") if tp_ok else ""
                sl_id = sl_result.get("result", {}).get("orderId", "") if sl_ok else ""
                self.open_order_ids[symbol] = {"tp": tp_id, "sl": sl_id}
                self._save_order_ids()
                print(f"   📌 Сохранены ID: TP={tp_id[:8] if tp_id else '—'}... SL={sl_id[:8] if sl_id else '—'}...")

            else:
                # SELL — отмена TP/SL → рыночная продажа
                self._cancel_tp_sl(symbol)

                coin    = symbol.replace("USDT", "")
                balance = self.trader.get_coin_balance(coin)

                if balance <= 0:
                    print(f"❌ Нет {coin} на балансе для продажи")
                    return False

                sell_qty = round(math.floor(balance / step) * step, qty_dec)
                if sell_qty < min_qty:
                    print(f"❌ Баланс {sell_qty} {coin} меньше минимума {min_qty}")
                    return False

                print(f"🔄 Продажа {sell_qty} {coin} по рынку...")
                sell_result = self.trader.place_order(
                    category="spot",
                    symbol=symbol,
                    side="Sell",
                    order_type="Market",
                    qty=str(sell_qty),
                    market_unit="baseCoin",
                )

                if sell_result.get("retCode") != 0:
                    print(f"❌ Ошибка продажи: {sell_result.get('retMsg')}")
                    return False

                print(f"✅ Продано {sell_qty} {coin}. ID: {sell_result['result']['orderId']}")
                self.portfolio.close_position(symbol)

            # Обновляем историю сделок в дашборде
            _filled = filled_qty if side == 'Buy' else (sell_qty if 'sell_qty' in dir() else 0)
            self.current_data.setdefault('trades', []).append({
                'time':   datetime.now().strftime('%H:%M:%S'),
                'symbol': symbol,
                'side':   side,
                'price':  current_price,
                'qty':    round(_filled, 6),
                'value':  round(current_price * _filled, 2),
                'profit': 0,
            })

            print("💰 Баланс обновится при следующем запросе")
            return True

        except Exception as e:
            print(f"❌ Исключение в place_order: {e}")
            import traceback
            traceback.print_exc()
            return False

    # ─────────────────────────────────────────────────────────────────────────
    # Вспомогательные методы (verbatim из main.py v1)
    # ─────────────────────────────────────────────────────────────────────────

    def _calc_order_params(self, symbol: str, side: str, usdt_amount: float):
        """Вычисляем qty, step, tick и прочее для выставления ордера."""
        try:
            instr = self.instruments.get(symbol) or {}
            if not instr:
                print(f"   📡 instruments кэш пуст для {symbol}, запрашиваем с биржи...")
                raw = self.trader.get_instruments_info(symbol, "spot")
                try:
                    lot  = raw["result"]["list"][0]["lotSizeFilter"]
                    pf   = raw["result"]["list"][0].get("priceFilter", {})
                    instr = {
                        'min_qty':      float(lot.get("minOrderQty", 0.001)),
                        'min_notional': float(lot.get("minOrderAmt", 5.0)),
                        'qty_step':     float(lot.get("basePrecision", 0.001)),
                        'tick_size':    float(pf.get("tickSize", 0.01)),
                    }
                    print(f"   ✅ Параметры получены: {instr}")
                except Exception as e:
                    print(f"   ⚠️ Ошибка парсинга instruments_info: {e}, используем дефолты")

            min_qty      = float(instr.get('min_qty',      0.001))
            min_notional = float(instr.get('min_notional', 5.0))
            step         = float(instr.get('qty_step',     0.001))
            tick_size    = float(instr.get('tick_size',    0.01))

            tick_dec = 0 if tick_size >= 1 else max(0, -int(math.floor(math.log10(tick_size))))
            qty_dec  = 0 if step >= 1      else max(0, -int(math.floor(math.log10(step))))

            current_price = self.current_data.get('prices', {}).get(symbol, 0)
            if current_price == 0:
                current_price = self.trader.get_price(symbol, "spot")

            if usdt_amount < min_notional:
                print(f"⚠️ Сумма {usdt_amount} USDT меньше минимальной {min_notional} USDT")
                return None

            if side == "Buy":
                qty         = usdt_amount / current_price
                market_unit = "quoteCoin"
            else:
                coin    = symbol.replace("USDT", "")
                balance = self.get_coin_balance(coin)
                if balance <= 0:
                    print(f"⚠️ Нет баланса {coin} для продажи")
                    return None
                qty         = balance
                market_unit = "baseCoin"

            qty = round(math.floor(qty / step) * step, qty_dec)

            if qty < min_qty:
                print(f"⚠️ Количество {qty} меньше минимального {min_qty}")
                return None

            return {
                'qty':           qty,
                'qty_dec':       qty_dec,
                'tick_dec':      tick_dec,
                'market_unit':   market_unit,
                'current_price': current_price,
                'min_notional':  min_notional,
                'step':          step,
                'min_qty':       min_qty,
            }

        except Exception as e:
            print(f"❌ Ошибка расчёта параметров: {e}")
            return None

    def get_coin_balance(self, coin: str) -> float:
        """Прокси к trader.get_coin_balance()."""
        return self.trader.get_coin_balance(coin)

    def _cancel_tp_sl(self, symbol: str):
        """
        Отменяем все висящие ордера по символу (TP + SL).
        Verbatim из main.py v1. НЕ ИЗМЕНЯТЬ.
        """
        ids       = self.open_order_ids.pop(symbol, {})
        cancelled = []

        for label, oid in ids.items():
            if not oid:
                continue
            order_filter = "StopOrder" if label == "sl" else "Order"
            r = self.trader._request("POST", "/v5/order/cancel", {
                "category":    "spot",
                "symbol":      symbol,
                "orderId":     oid,
                "orderFilter": order_filter,
            })
            if r.get("retCode") in (0, 110001):
                cancelled.append(f"{label.upper()} {oid[:8]}")
            else:
                print(f"   ⚠️ Не удалось отменить {label.upper()}: {r.get('retMsg')}")

        # Страховка — cancel-all гарантирует отсутствие SL-зомби
        try:
            for order_filter in ("Order", "StopOrder"):
                r = self.trader._request("POST", "/v5/order/cancel-all", {
                    "category":    "spot",
                    "symbol":      symbol,
                    "orderFilter": order_filter,
                })
                if r.get("retCode") == 0:
                    cnt = len(r.get("result", {}).get("list", []))
                    if cnt > 0:
                        print(f"   🗑️  cancel-all {order_filter}: отменено {cnt} ордеров")
        except Exception as e:
            print(f"   ⚠️ cancel-all error: {e}")

        self._save_order_ids()
        if cancelled:
            print(f"🗑️  Отменены ордера {symbol}: {', '.join(cancelled)}")
        else:
            print(f"🗑️  Ордера {symbol} уже закрыты или не найдены")

    def _restore_entry_price(self, symbol: str) -> float:
        """Восстанавливает цену входа из истории исполнений."""
        try:
            r = self.trader._request("GET", "/v5/execution/list", {
                "category": "spot",
                "symbol":   symbol,
                "limit":    50,
            })
            if r.get("retCode") != 0:
                return 0.0
            for ex in r["result"].get("list", []):
                if ex.get("symbol") == symbol and ex.get("side") == "Buy":
                    price = float(ex.get("execPrice", 0) or 0)
                    if price > 0:
                        return price
        except Exception as e:
            print(f"⚠️ _restore_entry_price {symbol}: {e}")
        return 0.0

    def _detect_close_reason(self, symbol: str, tp_id: str, sl_id: str) -> str:
        """Определяем что сработало — TP или SL."""
        try:
            for label, oid, flt in [("TP", tp_id, "Order"),
                                     ("SL", sl_id, "StopOrder")]:
                if not oid:
                    continue
                r = self.trader._request("GET", "/v5/order/history", {
                    "category":    "spot",
                    "symbol":      symbol,
                    "orderId":     oid,
                    "orderFilter": flt,
                    "limit":       1,
                })
                if r.get("retCode") == 0:
                    lst = r.get("result", {}).get("list", [])
                    if lst and lst[0].get("orderStatus") == "Filled":
                        return f"{label} сработал"
        except Exception:
            pass
        return "причина неизвестна"

    def _move_sl_to_breakeven(self, symbol: str, entry_price: float, qty: float):
        """
        Двигает SL на уровень безубытка (entry + BREAKEVEN_OFFSET).
        Вызывается когда цена ушла вверх на BREAKEVEN_TRIGGER%.
        Verbatim из main.py v1. НЕ ИЗМЕНЯТЬ.
        """
        self.breakeven_activated[symbol] = True

        be_price = round(entry_price * (1 + self.BREAKEVEN_OFFSET), 6)

        instr     = self.instruments.get(symbol, {})
        tick_size = float(instr.get('tick_size', 0.0001))
        if tick_size > 0:
            be_price = round(round(be_price / tick_size) * tick_size, 8)

        self.trader._request("POST", "/v5/order/cancel-all", {
            "category":    "spot",
            "symbol":      symbol,
            "orderFilter": "StopOrder",
        })

        step    = float(instr.get('qty_step', 0.001))
        qty_dec = len(str(step).rstrip('0').split('.')[-1]) if '.' in str(step) else 0
        sl_qty  = round(math.floor(qty / step) * step, qty_dec) if step > 0 else qty

        r = self.trader._request("POST", "/v5/order/create", {
            "category":     "spot",
            "symbol":       symbol,
            "side":         "Sell",
            "orderType":    "Market",
            "qty":          str(sl_qty),
            "triggerPrice": str(be_price),
            "triggerBy":    "LastPrice",
            "timeInForce":  "IOC",
            "orderFilter":  "StopOrder",
        })

        if r.get("retCode") == 0:
            new_sl_id = r["result"].get("orderId", "")
            if symbol not in self.open_order_ids:
                self.open_order_ids[symbol] = {}
            self.open_order_ids[symbol]["sl"] = new_sl_id
            self._save_order_ids()
            print(f"   🛡️ Безубыток {symbol}: SL → {be_price}  "
                  f"(entry={entry_price:.4f} + {self.BREAKEVEN_OFFSET*100:.1f}% комиссии)")
        else:
            print(f"   ⚠️ Не удалось выставить SL на безубытке: {r.get('retMsg')}"
                  f"  (флаг активирован — повторных попыток не будет)")

    # ─────────────────────────────────────────────────────────────────────────
    # Персистентность order IDs (verbatim из main.py v1)
    # ─────────────────────────────────────────────────────────────────────────

    def _order_ids_path(self) -> str:
        """Путь к файлу order_ids — отдельный для DEMO и REAL."""
        mode  = 'real' if self.real_mode else 'demo'
        fname = f'order_ids_{mode}.json'
        return os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', fname)

    def _save_order_ids(self):
        """Атомарная запись order IDs на диск."""
        path = self._order_ids_path()
        try:
            tmp = path + '.tmp'
            with open(tmp, 'w', encoding='utf-8') as f:
                _json.dump(self.open_order_ids, f, indent=2)
            os.replace(tmp, path)
        except Exception as e:
            print(f"⚠️  _save_order_ids: {e}")

    def _load_order_ids(self) -> dict:
        """Загрузка order IDs с диска при старте."""
        path = self._order_ids_path()
        if not os.path.exists(path):
            return {}
        try:
            with open(path, 'r', encoding='utf-8') as f:
                data = _json.load(f)
            valid   = {sym: ids for sym, ids in data.items()
                       if ids.get('tp') or ids.get('sl')}
            skipped = len(data) - len(valid)
            if valid:
                mode = 'REAL' if self.real_mode else 'DEMO'
                print(f"📌 Загружены order_ids [{mode}]: {list(valid.keys())}"
                      + (f"  ({skipped} пустых пропущено)" if skipped else ""))
            return valid
        except Exception as e:
            print(f"⚠️  _load_order_ids: {e}")
            return {}
