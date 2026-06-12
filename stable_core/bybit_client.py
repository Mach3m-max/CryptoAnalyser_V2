# ═══════════════════════════════════════════════════════════════════════════════
# bybit_client.py  —  HTT v2 / stable_core
#
# СТАТУС: VERBATIM COPY из v1 (BotByBit).
# ❌ НЕ ИЗМЕНЯТЬ этот файл без крайней необходимости.
#    Весь код отлажен в реальных рыночных условиях.
#
# URL:
#   DEMO: https://api-demo.bybit.com   ← параметр testnet=True
#   REAL: https://api.bybit.com        ← параметр testnet=False
#
#   ВНИМАНИЕ: "testnet" — legacy-название параметра; фактически это DEMO
#   (https://api-demo.bybit.com), а НЕ testnet (https://api-testnet.bybit.com).
#   Bybit разделил: testnet устарел, вместо него — demo-аккаунт на основном домене.
# ═══════════════════════════════════════════════════════════════════════════════

import requests
import time
import hmac
import hashlib
import json
import math
from urllib.parse import urlencode


def sign(secret: str, timestamp: str, api_key: str, recv_window: str, payload: str) -> str:
    param_str = f"{timestamp}{api_key}{recv_window}{payload}"
    return hmac.new(secret.encode("utf-8"), param_str.encode("utf-8"), hashlib.sha256).hexdigest()


class BybitTrader:
    """
    HTTP клиент Bybit API v5.
    Отлажен в реальных рыночных условиях — не изменять.

    Параметры:
        api_key    — API ключ
        api_secret — API секрет
        testnet    — True → DEMO (api-demo.bybit.com)
                     False → REAL (api.bybit.com)
    """

    def __init__(self, api_key: str, api_secret: str, testnet: bool = True):
        self.api_key     = api_key
        self.api_secret  = api_secret
        self.recv_window = "20000"          # увеличено с 5000 — перекрывает небольшой drift
        # ── КРИТИЧНО: DEMO → api-demo.bybit.com, НЕ api-testnet.bybit.com ──
        self.base_url    = "https://api-demo.bybit.com" if testnet else "https://api.bybit.com"
        self._last_orders_errors: list = []
        self._time_offset_ms: int = 0       # коррекция локального времени
        self._sync_time()                   # синхронизируемся при старте

    def _sync_time(self):
        """Получаем серверное время и считаем offset."""
        try:
            r = requests.get(self.base_url + "/v5/market/time", timeout=5)
            server_ms = int(r.json()["result"]["timeNano"]) // 1_000_000
            self._time_offset_ms = server_ms - int(time.time() * 1000)
        except Exception:
            self._time_offset_ms = 0

    def _request(self, method: str, endpoint: str, params: dict = None) -> dict:
        """
        Подписанный запрос к Bybit API.
        Авто-ретрай при ошибках timestamp (10002/10004).
        """
        if params is None:
            params = {}
        url          = self.base_url + endpoint
        timestamp    = str(int(time.time() * 1000) + self._time_offset_ms)
        query_string = urlencode(params) if method == "GET" else ""
        body         = json.dumps(params) if method == "POST" else ""
        sign_payload = (query_string if method == "GET" else body)
        signature    = sign(self.api_secret, timestamp, self.api_key,
                            self.recv_window, sign_payload)
        headers = {
            "X-BAPI-API-KEY":   self.api_key,
            "X-BAPI-TIMESTAMP": timestamp,
            "X-BAPI-SIGN":      signature,
            "X-BAPI-RECV-WINDOW": self.recv_window,
            "Content-Type":     "application/json",
        }
        try:
            if method == "POST":
                r = requests.post(url, headers=headers, data=body, timeout=10)
            else:
                r = requests.get(url, headers=headers, params=params, timeout=10)
            result = r.json()
            # Если ошибка timestamp — пересинхронизируем и повторяем один раз
            if result.get("retCode") in (10002, 10004):
                self._sync_time()
                timestamp = str(int(time.time() * 1000) + self._time_offset_ms)
                if method == "POST":
                    sign_payload = body
                else:
                    sign_payload = query_string
                _sig = hmac.new(
                    self.api_secret.encode("utf-8"),
                    (timestamp + self.api_key + self.recv_window + sign_payload).encode("utf-8"),
                    hashlib.sha256
                ).hexdigest()
                headers["X-BAPI-TIMESTAMP"] = timestamp
                headers["X-BAPI-SIGN"]      = _sig
                if method == "POST":
                    r = requests.post(url, headers=headers, data=body, timeout=10)
                else:
                    r = requests.get(url, headers=headers, params=params, timeout=10)
                result = r.json()
            return result
        except Exception as e:
            return {"retCode": -1, "retMsg": f"Network error: {e}"}

    # ── Рыночные данные ───────────────────────────────────────────────────────

    def get_price(self, symbol: str, category: str = "spot") -> float:
        """Текущая цена символа."""
        r = requests.get(
            self.base_url + "/v5/market/tickers",
            params={"category": category, "symbol": symbol},
            timeout=5,
        )
        try:
            return float(r.json()["result"]["list"][0]["lastPrice"])
        except Exception:
            return 0.0

    def get_klines(self, symbol: str, interval: str = "1",
                   limit: int = 200, category: str = "spot") -> list:
        """Исторические свечи (OHLCV)."""
        r = self._request("GET", "/v5/market/kline", {
            "category": category,
            "symbol":   symbol,
            "interval": interval,
            "limit":    limit,
        })
        if r.get("retCode") == 0:
            return r["result"].get("list", [])
        return []

    def get_instruments_info(self, symbol: str, category: str = "spot") -> dict:
        """Параметры инструмента (min_qty, tick_size, qty_step)."""
        return self._request("GET", "/v5/market/instruments-info", {
            "category": category,
            "symbol":   symbol,
        })

    # ── Ордера ────────────────────────────────────────────────────────────────

    def place_order(self, category: str, symbol: str, side: str,
                    order_type: str, qty: str = None,
                    price: str = None, time_in_force: str = None,
                    market_unit: str = None, trigger_price: str = None,
                    trigger_by: str = None, order_filter: str = None,
                    **kwargs) -> dict:
        """Универсальный ордер (Market/Limit/StopOrder)."""
        params = {
            "category":  category,
            "symbol":    symbol,
            "side":      side,
            "orderType": order_type,
        }
        if qty:            params["qty"]           = qty
        if price:          params["price"]          = price
        if time_in_force:  params["timeInForce"]    = time_in_force
        if market_unit:    params["marketUnit"]     = market_unit
        if trigger_price:  params["triggerPrice"]   = trigger_price
        if trigger_by:     params["triggerBy"]      = trigger_by
        if order_filter:   params["orderFilter"]    = order_filter
        params.update(kwargs)
        return self._request("POST", "/v5/order/create", params)

    def cancel_order(self, category: str, symbol: str,
                     order_id: str = None, order_filter: str = None) -> dict:
        """Отмена ордера по ID."""
        params = {"category": category, "symbol": symbol}
        if order_id:     params["orderId"]     = order_id
        if order_filter: params["orderFilter"] = order_filter
        return self._request("POST", "/v5/order/cancel", params)

    def cancel_all_orders(self, category: str, symbol: str,
                          order_filter: str = None) -> dict:
        """Отмена всех ордеров по символу."""
        params = {"category": category, "symbol": symbol}
        if order_filter: params["orderFilter"] = order_filter
        return self._request("POST", "/v5/order/cancel-all", params)

    def get_open_orders(self, category: str = "spot", symbol: str = None,
                        order_filter: str = None) -> list:
        """Активные ордера."""
        params = {"category": category, "limit": 50}
        if symbol:       params["symbol"]      = symbol
        if order_filter: params["orderFilter"] = order_filter
        r = self._request("GET", "/v5/order/realtime", params)
        if r.get("retCode") == 0:
            return r["result"].get("list", [])
        return []

    def get_order_history(self, category: str = "spot", symbol: str = None,
                          order_id: str = None, order_filter: str = None,
                          limit: int = 10) -> list:
        """История ордеров."""
        params = {"category": category, "limit": limit}
        if symbol:       params["symbol"]      = symbol
        if order_id:     params["orderId"]     = order_id
        if order_filter: params["orderFilter"] = order_filter
        r = self._request("GET", "/v5/order/history", params)
        if r.get("retCode") == 0:
            return r["result"].get("list", [])
        return []

    def get_filled_qty(self, symbol: str, order_id: str,
                       category: str = "spot") -> float:
        """Исполненное количество по ордеру."""
        r = self._request("GET", "/v5/order/history", {
            "category": category,
            "symbol":   symbol,
            "orderId":  order_id,
            "limit":    1,
        })
        try:
            lst = r["result"].get("list", [])
            if lst:
                return float(lst[0].get("cumExecQty", 0) or 0)
        except Exception:
            pass
        return 0.0

    def get_execution_list(self, symbol: str, category: str = "spot",
                           limit: int = 50) -> list:
        """История исполнений (сделки) с пагинацией."""
        r = self._request("GET", "/v5/execution/list", {
            "category": category,
            "symbol":   symbol,
            "limit":    limit,
        })
        if r.get("retCode") == 0:
            return r["result"].get("list", [])
        return []

    def place_spot_oco(self, symbol: str, qty: str,
                       tp_price: str, sl_price: str) -> dict:
        """
        OCO ордер для Spot UTA.
        Используется в REAL-режиме (в DEMO — Limit+Stop раздельно).
        """
        return self._request("POST", "/v5/order/create", {
            "category":    "spot",
            "symbol":      symbol,
            "side":        "Sell",
            "orderType":   "Limit",
            "qty":         qty,
            "price":       tp_price,
            "timeInForce": "GTC",
            "tpslMode":    "Full",
            "stopLoss":    sl_price,
            "slTriggerBy": "LastPrice",
            "slOrderType": "Market",
        })

    # ── Баланс ────────────────────────────────────────────────────────────────

    def get_wallet_balance(self) -> dict:
        """Полный баланс кошелька UNIFIED."""
        return self._request("GET", "/v5/account/wallet-balance",
                             {"accountType": "UNIFIED"})

    def get_coin_balance(self, coin: str) -> float:
        """
        Баланс конкретной монеты.
        Перебирает поля по приоритету:
        availableToWithdraw → availableToTrade → walletBalance.
        """
        try:
            data = self._request("GET", "/v5/account/wallet-balance",
                                 {"accountType": "UNIFIED"})
            if data.get("retCode") != 0:
                return 0.0
            for c in data["result"]["list"][0]["coin"]:
                if c.get("coin") == coin:
                    for field in ("availableToWithdraw", "availableToTrade", "walletBalance"):
                        val = c.get(field)
                        if val not in (None, "", "0", 0):
                            try:
                                f = float(val)
                                if f > 0:
                                    return f
                            except (ValueError, TypeError):
                                pass
        except Exception as e:
            print(f"⚠️ get_coin_balance {coin}: {e}")
        return 0.0

    # ── Spot Margin ───────────────────────────────────────────────────────────

    def check_borrow_quota(self, symbol: str, side: str,
                           order_type: str = "Market",
                           qty: str = None, quote_qty: str = None) -> dict:
        """Проверка доступной суммы займа для Spot Margin."""
        params = {
            "category":  "spot",
            "symbol":    symbol,
            "side":      side,
            "orderType": order_type,
        }
        if qty:       params["qty"]      = qty
        if quote_qty: params["quoteQty"] = quote_qty
        return self._request("GET", "/v5/order/spot-borrow-check", params)

    # ── Futures (вспомогательные — не используются в Spot-боте) ──────────────

    def set_trading_stop(self, symbol: str, position_idx: int = 0,
                         stop_loss: float = None,
                         take_profit: float = None,
                         category: str = "linear") -> dict:
        """Трейлинг/TP/SL для Futures-позиции (не используется в Spot)."""
        params = {
            "category":    category,
            "symbol":      symbol,
            "positionIdx": position_idx,
        }
        if stop_loss is not None:
            params["stopLoss"]   = str(stop_loss)
        if take_profit is not None:
            params["takeProfit"] = str(take_profit)
        return self._request("POST", "/v5/position/trading-stop", params)

    def get_positions(self, category: str = "linear",
                      symbol: str = None) -> list:
        """Открытые Futures-позиции."""
        params = {"category": category, "limit": 50}
        if symbol:
            params["symbol"] = symbol
        r = self._request("GET", "/v5/position/list", params)
        if r.get("retCode") == 0:
            return [p for p in r["result"].get("list", [])
                    if float(p.get("size", 0)) > 0]
        return []
