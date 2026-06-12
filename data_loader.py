from typing import Optional
# data_loader.py
"""
Загрузка необходимых данных с Bybit для быстрого старта.
v1 verbatim — ЕДИНСТВЕННОЕ изменение: self.data_dir → data/historical/
"""
import pandas as pd
import numpy as np
from datetime import datetime, timedelta
import os
import json
from bybit_client import BybitTrader
from config import PORTFOLIO, TECH_PARAMS, LOG_FLAGS


class DataLoader:
    """Загрузчик исторических данных с Bybit"""

    def __init__(self, trader: BybitTrader):
        self.trader = trader
        # v2 PATH FIX: data/historical/ вместо historical_data/
        self.data_dir = os.path.join(
            os.path.dirname(os.path.abspath(__file__)),
            "data", "historical"
        )
        os.makedirs(self.data_dir, exist_ok=True)

    # ── Информация об инструментах ────────────────────────────────────────────

    def load_instruments_info(self, symbols: list) -> dict:
        """Загрузка информации об инструментах (lot size, tick size, min qty)"""
        info   = {}
        errors = []
        for symbol in symbols:
            data = self.trader.get_instruments_info(symbol, "spot")
            try:
                instrument = data["result"]["list"][0]
                lot = instrument["lotSizeFilter"]
                pf  = instrument.get("priceFilter", {})
                info[symbol] = {
                    'min_qty':      float(lot.get("minOrderQty",   0)),
                    'min_notional': float(lot.get("minOrderAmt",   5)),
                    'qty_step':     float(lot.get("basePrecision", 0.001)),
                    'tick_size':    float(pf.get("tickSize",       0.01)),
                    'max_leverage': float(instrument.get(
                        "leverageFilter", {}).get("maxLeverage", 1)),
                }
                if LOG_FLAGS.get('instruments_load', False):
                    print(f"  📐 {symbol}: min_qty={info[symbol]['min_qty']}, "
                          f"step={info[symbol]['qty_step']}")
            except Exception as e:
                errors.append(symbol)
                print(f"  ❌ Ошибка загрузки {symbol}: {e}")
                info[symbol] = self._get_default_info()

        ok = len(symbols) - len(errors)
        print(f"  📐 Инструменты: {ok}/{len(symbols)} загружено"
              + (f" | ❌ ошибки: {', '.join(errors)}" if errors else ""))

        with open(os.path.join(self.data_dir, "instruments_info.json"), 'w') as f:
            json.dump(info, f, indent=2)
        return info

    # ── Исторические свечи ────────────────────────────────────────────────────

    def load_historical_data(self, symbol: str, days: int = 30) -> pd.DataFrame:
        """
        Загрузка исторических данных.
        - При первом запуске грузит максимум доступного (до MAX_DAYS дней)
        - Кэш действует 5 минут, потом инкрементальное обновление
        - days — минимум для работы стратегии, реально грузим больше
        """
        cache_file    = os.path.join(self.data_dir, f"{symbol}_30d.csv")
        CACHE_TTL_SEC = 300   # 5 минут
        MAX_DAYS      = 90    # потолок хранения

        if os.path.exists(cache_file):
            try:
                mtime        = os.path.getmtime(cache_file)
                age          = datetime.now().timestamp() - mtime
                df_cached    = pd.read_csv(cache_file, parse_dates=['timestamp'])
                df_cached    = df_cached.sort_values('timestamp').reset_index(drop=True)
                cached_days  = (df_cached['timestamp'].max() - df_cached['timestamp'].min()).days

                # Если данных мало — докачиваем назад
                if cached_days < MAX_DAYS - 5:
                    oldest_ts_ms = int(df_cached['timestamp'].min().timestamp() * 1000)
                    old_klines   = self._fetch_klines_range(
                        symbol, oldest_ts_ms, MAX_DAYS - cached_days)
                    if old_klines:
                        df_old    = self._klines_to_df(old_klines)
                        df_cached = pd.concat([df_old, df_cached], ignore_index=True)
                        df_cached = df_cached.drop_duplicates('timestamp').sort_values('timestamp').reset_index(drop=True)
                        df_cached = self._trim(df_cached, MAX_DAYS)
                        self._safe_save(df_cached, cache_file)
                        cached_days = (df_cached['timestamp'].max() - df_cached['timestamp'].min()).days
                        if LOG_FLAGS.get('candle_updates', False):
                            print(f"  ✅ {symbol}: докачано → {len(df_cached)} свечей ({cached_days}д)")

                if age < CACHE_TTL_SEC:
                    if LOG_FLAGS.get('candle_updates', False):
                        print(f"  📦 {symbol}: {len(df_cached)} свечей ({cached_days}д) — кэш актуален")
                    return df_cached

                # Кэш устарел — инкрементальное обновление
                last_ts_ms = int(df_cached['timestamp'].max().timestamp() * 1000)
                if LOG_FLAGS.get('candle_updates', False):
                    print(f"  🔄 {symbol}: обновление (кэш {int(age//60)}м назад, {cached_days}д данных)...")

                new_klines = self._fetch_klines_since(symbol, last_ts_ms)
                if new_klines:
                    df_new = self._klines_to_df(new_klines)
                    df     = pd.concat([df_cached, df_new], ignore_index=True)
                    df     = df.drop_duplicates('timestamp').sort_values('timestamp').reset_index(drop=True)
                    df     = self._trim(df, MAX_DAYS)
                    self._safe_save(df, cache_file)
                    new_days = (df['timestamp'].max() - df['timestamp'].min()).days
                    if LOG_FLAGS.get('candle_updates', False):
                        print(f"  ✅ {symbol}: +{len(df_new)} свечей → {len(df)} ({new_days}д)")
                else:
                    if LOG_FLAGS.get('candle_updates', False):
                        print(f"  📦 {symbol}: {len(df_cached)} свечей ({cached_days}д)")
                    df = df_cached
                return df

            except Exception as e:
                print(f"  ⚠️ Ошибка кэша {symbol}: {e} — грузим заново")
                try:
                    os.remove(cache_file)
                except Exception:
                    pass

        # ── Нет кэша — полная загрузка ────────────────────────────────────────
        if LOG_FLAGS.get('candle_full_load', True):
            print(f"  📡 {symbol}: полная загрузка (макс. {MAX_DAYS}д)...")

        all_klines = []
        now_ms     = int(datetime.now().timestamp() * 1000)
        end_ms     = now_ms
        cutoff_ms  = now_ms - MAX_DAYS * 24 * 60 * 60 * 1000
        max_batches= MAX_DAYS * 24 * 60 // 1000 + 10

        for _ in range(max_batches):
            klines = self._fetch_batch(symbol, end_ms)
            if not klines:
                break
            all_klines.extend(klines)
            oldest_ms = int(klines[-1][0])
            if oldest_ms <= cutoff_ms:
                break
            end_ms = oldest_ms - 1

        if not all_klines:
            print(f"  ⚠️ {symbol}: нет данных")
            return None

        df = self._klines_to_df(all_klines)
        df = self._trim(df, MAX_DAYS)
        self._safe_save(df, cache_file)
        loaded_days = (df['timestamp'].max() - df['timestamp'].min()).days
        if LOG_FLAGS.get('candle_full_load', True):
            print(f"  ✅ {symbol}: {len(df):,} свечей ({loaded_days}д) сохранено")
        return df

    def update_realtime(self, symbol: str) -> Optional[pd.DataFrame]:
        """Инкрементальное обновление — только новые свечи с последней записи."""
        cache_file = os.path.join(self.data_dir, f"{symbol}_30d.csv")
        if not os.path.exists(cache_file):
            return self.load_historical_data(symbol)
        try:
            df         = pd.read_csv(cache_file, parse_dates=['timestamp'])
            last_ts_ms = int(df['timestamp'].max().timestamp() * 1000)
            new_klines = self._fetch_klines_since(symbol, last_ts_ms)
            if new_klines:
                df_new = self._klines_to_df(new_klines)
                df     = pd.concat([df, df_new], ignore_index=True)
                df     = df.drop_duplicates('timestamp').sort_values('timestamp').reset_index(drop=True)
                df     = self._trim(df, 90)
                self._safe_save(df, cache_file)
            return df
        except Exception as e:
            print(f"  ⚠️ update_realtime {symbol}: {e}")
            return None

    def load_all_data(self, symbols: list) -> tuple:
        """Загружает данные и инструменты для всех пар."""
        historical_data = {}
        for sym in symbols:
            df = self.load_historical_data(sym)
            if df is not None and len(df) > 200:
                historical_data[sym] = df
        instruments = self.load_instruments_info(symbols)
        print(f"📈 Загружено данных: {len(historical_data)}/{len(symbols)} пар")
        return historical_data, instruments

    # ── Внутренние методы (verbatim v1) ──────────────────────────────────────

    def _fetch_batch(self, symbol: str, end_ms: int, limit: int = 1000) -> list:
        try:
            r = self.trader._request("GET", "/v5/market/kline", {
                "category": "spot",
                "symbol":   symbol,
                "interval": TECH_PARAMS.get('timeframe', '1'),
                "limit":    limit,
                "end":      str(end_ms),
            })
            if r.get("retCode") == 0:
                return r["result"].get("list", [])
        except Exception as e:
            print(f"  ⚠️ _fetch_batch {symbol}: {e}")
        return []

    def _fetch_klines_since(self, symbol: str, since_ms: int, limit: int = 200) -> list:
        try:
            now_ms = int(datetime.now().timestamp() * 1000)
            r = self.trader._request("GET", "/v5/market/kline", {
                "category": "spot",
                "symbol":   symbol,
                "interval": TECH_PARAMS.get('timeframe', '1'),
                "limit":    limit,
                "start":    str(since_ms + 60000),
                "end":      str(now_ms),
            })
            if r.get("retCode") == 0:
                return r["result"].get("list", [])
        except Exception as e:
            print(f"  ⚠️ _fetch_klines_since {symbol}: {e}")
        return []

    def _fetch_klines_range(self, symbol: str, end_ms: int, days: int) -> list:
        all_klines = []
        cutoff_ms  = end_ms - days * 24 * 60 * 60 * 1000
        cur_end    = end_ms - 60000
        for _ in range(days * 24 * 60 // 1000 + 5):
            klines = self._fetch_batch(symbol, cur_end)
            if not klines:
                break
            all_klines.extend(klines)
            oldest = int(klines[-1][0])
            if oldest <= cutoff_ms:
                break
            cur_end = oldest - 1
        return all_klines

    def _klines_to_df(self, klines: list) -> pd.DataFrame:
        df = pd.DataFrame(
            klines,
            columns=["timestamp", "open", "high", "low", "close", "volume", "turnover"]
        )
        df["timestamp"] = pd.to_datetime(df["timestamp"].astype(float), unit="ms")
        for col in ("open", "high", "low", "close", "volume"):
            df[col] = pd.to_numeric(df[col], errors="coerce")
        return df.sort_values("timestamp").reset_index(drop=True)

    def _trim(self, df: pd.DataFrame, max_days: int) -> pd.DataFrame:
        """Обрезает DataFrame до max_days от последней свечи."""
        if df.empty:
            return df
        cutoff = df['timestamp'].max() - pd.Timedelta(days=max_days)
        return df[df['timestamp'] > cutoff].reset_index(drop=True)

    def _safe_save(self, df: pd.DataFrame, path: str):
        """Сохраняет DataFrame в CSV только если он не пустой."""
        if df is None or df.empty:
            print(f"  ⚠️ Пустой DataFrame — файл {os.path.basename(path)} не перезаписан")
            return
        df.to_csv(path, index=False)

    def _get_default_info(self) -> dict:
        return {
            'min_qty':      0.001,
            'min_notional': 5.0,
            'qty_step':     0.001,
            'tick_size':    0.01,
            'max_leverage': 1,
        }
