"""
backtester.py — Бэктест и оптимизация параметров ML стратегии
=============================================================
Запуск:
    python backtester.py                     # все пары, оптимизация
    python backtester.py --pair BTCUSDT      # одна пара
    python backtester.py --no-optimize       # только бэктест с текущими параметрами
    python backtester.py --top 5             # показать топ-5 наборов параметров

Результат:
    backtest_results/report_{timestamp}.html  — интерактивный HTML-отчёт
    backtest_results/best_params.json         — лучшие параметры для бота
"""

from __future__ import annotations
import sys
import os
import json
import pickle
import argparse
import itertools
import math
from copy import deepcopy
from pathlib import Path
from datetime import datetime
from dataclasses import dataclass, field, asdict

import numpy as np
import pandas as pd

# ── Пути ──────────────────────────────────────────────────────────────────────
BASE_DIR     = Path(__file__).parent
DATA_DIR     = BASE_DIR / "historical_data" / "Old"
if not DATA_DIR.exists() or not any(DATA_DIR.glob("*.csv")):
    DATA_DIR = BASE_DIR / "historical_data"
MODELS_DIR   = BASE_DIR / "ml" / "models"
OUT_DIR      = BASE_DIR / "backtest_results"
OUT_DIR.mkdir(exist_ok=True)

sys.path.insert(0, str(BASE_DIR))

# ── Константы ─────────────────────────────────────────────────────────────────
BUY_FEE      = 0.0018   # Bybit spot taker
SELL_FEE     = 0.0010
WARMUP_BARS  = 250       # свечей для прогрева индикаторов

PAIRS = [
    "BTCUSDT","ETHUSDT","SOLUSDT","BNBUSDT",
    "AVAXUSDT","APTUSDT","WUSDT","OPUSDT",
    "TIAUSDT","ATOMUSDT","WIFUSDT","ARBUSDT","XAUTUSDT",
]

# ── Сетка параметров для оптимизации ─────────────────────────────────────────
# Обновлена по результатам бэктеста 2026-03-15:
#   tp=1.5% убран — провоцирует >1000 сделок/шум у WIFUSDT
#   sl=1.0% убран — недостаточно для волатильных альтов
#   conf=0.50/0.55 убраны — best у 10/13 пар 65-70%
# Итого: 288 комбинаций вместо 1920 → в ~6.7× быстрее
PARAM_GRID = {
    "tp_pct":           [0.020, 0.025, 0.030],           # Take Profit %
    "sl_pct":           [0.015, 0.020],                  # Stop Loss %
    "min_conf":         [0.60, 0.65, 0.70],              # мин. уверенность
    "sell_close_conf":  [0.65, 0.70, 0.75, 1.01],        # порог досрочного выхода (1.01=выкл)
    "breakeven_trig":   [0.8, 1.0, 1.5, 99.0],          # триггер безубытка % (99=выкл)
    "trend_filter":     [True, False],                   # трендовый фильтр вкл/выкл
}


# ── Dataclass результата одной сделки ─────────────────────────────────────────
@dataclass
class Trade:
    symbol:      str
    entry_bar:   int
    exit_bar:    int
    entry_price: float
    exit_price:  float
    exit_reason: str   # TP / SL / SELL_CLOSE / END
    pnl_pct:     float
    pnl_usdt:    float
    holding_bars: int


# ── Загрузка данных ───────────────────────────────────────────────────────────
def load_df(symbol: str) -> pd.DataFrame | None:
    candidates = sorted(DATA_DIR.glob(f"{symbol}_*.csv"))
    if not candidates:
        return None
    # Выбираем самый длинный файл
    best = max(candidates, key=lambda p: p.stat().st_size)
    df = pd.read_csv(best, parse_dates=["timestamp"])
    df = df.sort_values("timestamp").reset_index(drop=True)
    for col in ("open", "high", "low", "close", "volume"):
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")
    return df.dropna(subset=["close"])


def load_model(symbol: str) -> dict | None:
    path = MODELS_DIR / f"{symbol}_model.pkl"
    if not path.exists():
        return None
    with open(path, "rb") as f:
        return pickle.load(f)


# ── Вычисление фичей (упрощённый порт из ml_strategy_engine) ─────────────────
def _rsi(closes: np.ndarray, period: int) -> float:
    if len(closes) < period + 1:
        return 50.0
    delta = np.diff(closes[-period - 1:].astype(float))
    gain  = np.where(delta > 0, delta, 0.0)
    loss  = np.where(delta < 0, -delta, 0.0)
    ag, al = gain.mean(), loss.mean()
    if al < 1e-9:
        return 100.0
    return 100.0 - 100.0 / (1.0 + ag / al)


def build_features(df: pd.DataFrame, i: int, btc_df: pd.DataFrame | None,
                   feature_names: list) -> np.ndarray | None:
    """Строит вектор фичей для бара i."""
    window = df.iloc[max(0, i - WARMUP_BARS): i + 1]
    if len(window) < 210:
        return None

    closes  = window["close"].values.astype(float)
    highs   = window["high"].values.astype(float)   if "high"   in window.columns else closes
    lows    = window["low"].values.astype(float)     if "low"    in window.columns else closes
    volumes = window["volume"].values.astype(float)  if "volume" in window.columns else np.zeros(len(closes))
    cp      = closes[-1]

    feat = {}

    # SMA отклонения
    for name, w in (("50",50),("75",75),("100",100),("150",150),("200",200)):
        sma = np.mean(closes[-w:]) if len(closes) >= w else np.mean(closes)
        feat[f"dev_{name}"] = (cp - sma) / (sma + 1e-9) * 100

    avg_dev = float(np.mean([feat[f"dev_{w}"] for w in ("50","75","100","150","200")]))
    thr = 2.0
    feat["avg_deviation"] = avg_dev
    feat["buy_votes"]     = float(sum(1 for k in ("50","75","100","150","200") if feat[f"dev_{k}"] < -thr))
    feat["sell_votes"]    = float(sum(1 for k in ("50","75","100","150","200") if feat[f"dev_{k}"] >  thr))
    feat["confidence"]    = min(abs(avg_dev) / thr, 1.0)
    feat["dev_spread"]    = feat["dev_50"] - feat["dev_200"]
    feat["dev_momentum"]  = feat["dev_50"] - feat["dev_100"]

    def pct(n):
        return (cp - closes[-n]) / (closes[-n] + 1e-9) * 100 if len(closes) > n else 0.0

    feat["price_change_1m"]  = pct(1)
    feat["price_change_5m"]  = pct(5)
    feat["price_change_15m"] = pct(15)
    feat["price_change_30m"] = pct(30)
    feat["price_change_60m"] = pct(60)

    vol_last  = float(volumes[-1])
    vol_mean  = float(np.mean(volumes[-20:])) if len(volumes) >= 20 else (float(np.mean(volumes)) or 1.0)
    vol_roll5 = float(np.mean(volumes[-5:])) if len(volumes) >= 5 else vol_last
    feat["volume_last"] = vol_last
    feat["vol_ratio"]   = vol_last / (vol_mean + 1e-9)
    feat["vol_change"]  = vol_roll5 / (vol_mean + 1e-9)

    feat["rsi_14"]       = _rsi(closes, 14)
    feat["rsi_7"]        = _rsi(closes, 7)
    feat["rsi_14_change"] = feat["rsi_14"] - (_rsi(closes[:-3], 14) if len(closes) >= 18 else feat["rsi_14"])

    bb_n   = min(20, len(closes))
    bb_mid = float(np.mean(closes[-bb_n:]))
    bb_std = float(np.std(closes[-bb_n:]))
    bb_up  = bb_mid + 2 * bb_std
    bb_low = bb_mid - 2 * bb_std
    feat["bb_position"] = (cp - bb_low) / (bb_up - bb_low + 1e-9)
    feat["bb_width"]    = (bb_up - bb_low) / (bb_mid + 1e-9) * 100
    feat["bb_squeeze"]  = 1.0 if feat["bb_width"] < (((np.std(closes[-25:-5]) * 4 if len(closes) >= 25 else bb_std * 4) / (bb_mid + 1e-9)) * 100) else 0.0

    tr    = np.maximum.reduce([highs[1:] - lows[1:],
                                np.abs(highs[1:] - closes[:-1]),
                                np.abs(lows[1:]  - closes[:-1])])
    atr14 = float(np.mean(tr[-14:])) if len(tr) >= 14 else float(np.mean(tr))
    atr50 = float(np.mean(tr[-50:])) if len(tr) >= 50 else atr14
    feat["atr_pct"]   = atr14 / (cp + 1e-9) * 100
    feat["atr_ratio"] = atr14 / (atr50 + 1e-9)

    ema12 = pd.Series(closes).ewm(span=12, adjust=False).mean().values
    ema26 = pd.Series(closes).ewm(span=26, adjust=False).mean().values
    macd  = ema12 - ema26
    sig   = pd.Series(macd).ewm(span=9, adjust=False).mean().values
    hist  = macd - sig
    feat["macd_hist_norm"] = float(hist[-1]) / (atr14 + 1e-9)
    feat["macd_cross"]     = (1.0  if hist[-1] > 0 and hist[-2] <= 0 else
                              -1.0 if hist[-1] < 0 and hist[-2] >= 0 else 0.0)

    # BTC leading indicators
    if btc_df is not None and len(btc_df) > 60:
        btc_c = btc_df["close"].values.astype(float)
        n     = len(btc_c)
        def bpct(k): return (btc_c[-1] - btc_c[-k]) / (btc_c[-k] + 1e-9) * 100 if n > k else 0.0
        sma50b = np.mean(btc_c[-50:]) if n >= 50 else np.mean(btc_c)
        tr_b   = np.abs(np.diff(btc_c[-15:])) if n >= 15 else np.array([0.0])
        feat["btc_change_5m"]   = bpct(5)
        feat["btc_change_15m"]  = bpct(15)
        feat["btc_change_60m"]  = bpct(60)
        feat["btc_rsi_14"]      = _rsi(btc_c, 14)
        feat["btc_dev_50"]      = (btc_c[-1] - sma50b) / (sma50b + 1e-9) * 100
        feat["btc_atr_pct"]     = float(np.mean(tr_b)) / (btc_c[-1] + 1e-9) * 100
        feat["pair_vs_btc_15m"] = feat["price_change_15m"] - feat["btc_change_15m"]

    return np.array([feat.get(f, 0.0) for f in feature_names], dtype=float)


# ── Трендовый фильтр ──────────────────────────────────────────────────────────
def trend_filter(df: pd.DataFrame, i: int, signal: str) -> tuple[bool, str]:
    RED_STREAK = GREEN_STREAK = 5
    MAX_MOVE   = 3.0
    BARS       = 15
    closes = df["close"].values[max(0, i - BARS): i + 1].astype(float)
    opens  = df["open"].values[max(0, i - BARS): i + 1].astype(float) \
             if "open" in df.columns else closes

    n = len(closes)
    if n < RED_STREAK + 1:
        return True, ""

    if signal == "BUY":
        if all(closes[n-1-k] < opens[n-1-k] for k in range(RED_STREAK)):
            return False, f"{RED_STREAK} красных подряд"
        if n >= BARS:
            drop = (closes[n - BARS] - closes[-1]) / (closes[n - BARS] + 1e-9) * 100
            if drop >= MAX_MOVE:
                return False, f"падение {drop:.1f}%"
    elif signal == "SELL":
        if all(closes[n-1-k] > opens[n-1-k] for k in range(GREEN_STREAK)):
            return False, f"{GREEN_STREAK} зелёных подряд"
        if n >= BARS:
            pump = (closes[-1] - closes[n - BARS]) / (closes[n - BARS] + 1e-9) * 100
            if pump >= MAX_MOVE:
                return False, f"рост {pump:.1f}%"
    return True, ""


# ── Симуляция одной сделки ────────────────────────────────────────────────────
def simulate_trade(df: pd.DataFrame, symbol: str, entry_bar: int,
                   entry_price: float, params: dict,
                   pre_signals: list | None = None) -> Trade:
    """
    Симулирует одну открытую позицию вперёд по барам.
    pre_signals: list[(sig, conf) | None] длиной len(df)
    """
    tp_price     = entry_price * (1 + params["tp_pct"])
    sl_price     = entry_price * (1 - params["sl_pct"])
    be_price     = entry_price * 1.003
    be_triggered = False

    n      = len(df)
    closes = df["close"].values.astype(float)
    highs  = df["high"].values.astype(float)  if "high"  in df.columns else closes
    lows   = df["low"].values.astype(float)   if "low"   in df.columns else closes

    def make_trade(exit_bar, exit_p, reason):
        pnl = (exit_p * (1 - SELL_FEE) - entry_price * (1 + BUY_FEE)) / \
              (entry_price * (1 + BUY_FEE)) * 100
        return Trade(symbol, entry_bar, exit_bar, entry_price, exit_p,
                     reason, round(pnl, 4), round(pnl, 4), exit_bar - entry_bar)

    for j in range(entry_bar + 1, min(entry_bar + 1440, n)):
        hi = highs[j]
        lo = lows[j]
        cp = closes[j]

        # Безубыток
        if (not be_triggered
                and params["breakeven_trig"] < 99.0
                and cp >= entry_price * (1 + params["breakeven_trig"] / 100)):
            sl_price     = be_price
            be_triggered = True

        # TP
        if hi >= tp_price:
            return make_trade(j, tp_price, "TP")

        # SL / безубыток
        if lo <= sl_price:
            return make_trade(j, sl_price, "BE" if be_triggered else "SL")

        # Досрочный выход по SELL сигналу
        if (pre_signals is not None
                and params["sell_close_conf"] < 1.0
                and j < len(pre_signals)
                and pre_signals[j] is not None):
            sig_j, conf_j = pre_signals[j]
            if sig_j == "SELL" and conf_j >= params["sell_close_conf"]:
                return make_trade(j, cp, "SELL_CLOSE")

    # Принудительное закрытие
    end_bar = min(entry_bar + 1439, n - 1)
    return make_trade(end_bar, closes[end_bar], "END")


# ── Бэктест одной пары с заданными параметрами ───────────────────────────────
def backtest_pair(symbol: str, params: dict,
                  df: pd.DataFrame, payload: dict,
                  btc_df: pd.DataFrame | None,
                  pre_signals: list | None = None) -> dict:
    """
    pre_signals: список (signal, conf) длиной len(df) — если уже вычислен,
                 иначе вычисляем на лету.
    """
    model         = payload["model"]
    label_encoder = payload.get("label_encoder")
    feature_names = payload["meta"]["features"]
    uses_btc      = payload["meta"].get("btc_features", False) and symbol != "BTCUSDT"

    n      = len(df)
    closes = df["close"].values.astype(float)

    # Если сигналы не предвычислены — считаем
    if pre_signals is None:
        pre_signals = [None] * n
        for i in range(WARMUP_BARS, n):
            btc_window = btc_df.iloc[max(0, i - WARMUP_BARS): i + 1] \
                         if uses_btc and btc_df is not None else None
            fv = build_features(df, i, btc_window, feature_names)
            if fv is None:
                pre_signals[i] = ("HOLD", 0.0)
                continue
            try:
                proba = model.predict_proba(
                    pd.DataFrame(fv.reshape(1, -1), columns=feature_names)
                )[0]
                best  = int(np.argmax(proba))
                conf  = float(proba[best])
                raw   = model.classes_[best]
                raw_s = str(raw)
                if raw_s in ("BUY", "SELL", "HOLD"):
                    sig = raw_s
                elif label_encoder is not None:
                    sig = str(label_encoder.inverse_transform([int(raw)])[0])
                else:
                    sig = "HOLD"
            except Exception:
                sig, conf = "HOLD", 0.0
            pre_signals[i] = (sig, conf)

    trades: list[Trade] = []
    in_position          = False
    entry_bar_idx        = -1
    max_positions        = 1   # бэктест per-pair = 1 позиция одновременно

    for i in range(WARMUP_BARS, n):
        if pre_signals[i] is None:
            continue
        sig, conf = pre_signals[i]

        if not in_position:
            if sig == "BUY" and conf >= params["min_conf"]:
                # Трендовый фильтр
                if params["trend_filter"]:
                    ok, _ = trend_filter(df, i, "BUY")
                    if not ok:
                        continue
                in_position   = True
                entry_bar_idx = i
                entry_price   = closes[i]

        else:
            # Проверяем закрытие
            trade = simulate_trade(
                df, symbol, entry_bar_idx, entry_price, params,
                pre_signals=pre_signals if params["sell_close_conf"] < 1.0 else None
            )
            # Записываем сделку только если уже должна закрыться
            if trade.exit_bar <= i or sig in ("TP", "SL"):
                trades.append(trade)
                in_position = False
            # Иначе ждём дальше — позиция ещё открыта

    # Незакрытая позиция — закрываем END
    if in_position and entry_bar_idx >= 0:
        trade = simulate_trade(df, symbol, entry_bar_idx, closes[entry_bar_idx], params)
        trades.append(trade)

    return _calc_stats(symbol, params, trades)


# ── Быстрый бэктест через предвычисленные сигналы ────────────────────────────
def backtest_pair_fast(symbol: str, params: dict,
                       df: pd.DataFrame, pre_signals: list) -> dict:
    """Быстрый бэктест — сигналы уже готовы, перебираем только параметры."""
    n      = len(df)
    closes = df["close"].values.astype(float)
    highs  = df["high"].values.astype(float)  if "high"  in df.columns else closes
    lows   = df["low"].values.astype(float)   if "low"   in df.columns else closes

    trades:    list[Trade] = []
    in_pos     = False
    entry_bar  = -1
    entry_p    = 0.0
    tp_p = sl_p = be_p = 0.0
    be_triggered = False

    for i in range(WARMUP_BARS, n):
        ps = pre_signals[i]
        if ps is None:
            continue
        sig, conf = ps

        if not in_pos:
            if sig == "BUY" and conf >= params["min_conf"]:
                if params["trend_filter"]:
                    ok, _ = trend_filter(df, i, "BUY")
                    if not ok:
                        continue
                in_pos       = True
                entry_bar    = i
                entry_p      = closes[i]
                tp_p         = entry_p * (1 + params["tp_pct"])
                sl_p         = entry_p * (1 - params["sl_pct"])
                be_p         = entry_p * 1.003
                be_triggered = False
        else:
            hi = highs[i]
            lo = lows[i]
            cp = closes[i]

            # Безубыток
            if (not be_triggered
                    and params["breakeven_trig"] < 99.0
                    and cp >= entry_p * (1 + params["breakeven_trig"] / 100)):
                sl_p         = be_p
                be_triggered = True

            exit_p = reason = None

            if hi >= tp_p:
                exit_p, reason = tp_p, "TP"
            elif lo <= sl_p:
                exit_p, reason = sl_p, "BE" if be_triggered else "SL"
            elif (params["sell_close_conf"] < 1.0
                  and sig == "SELL"
                  and conf >= params["sell_close_conf"]):
                exit_p, reason = cp, "SELL_CLOSE"
            elif i - entry_bar >= 1440:   # принудительное закрытие через 24ч
                exit_p, reason = cp, "END"

            if exit_p is not None:
                pnl = (exit_p * (1 - SELL_FEE) - entry_p * (1 + BUY_FEE)) / \
                      (entry_p * (1 + BUY_FEE)) * 100
                trades.append(Trade(symbol, entry_bar, i,
                                    entry_p, exit_p, reason,
                                    round(pnl, 4), round(pnl, 4),
                                    i - entry_bar))
                in_pos = False

    # Незакрытая
    if in_pos:
        cp  = closes[-1]
        pnl = (cp * (1 - SELL_FEE) - entry_p * (1 + BUY_FEE)) / \
              (entry_p * (1 + BUY_FEE)) * 100
        trades.append(Trade(symbol, entry_bar, n - 1,
                            entry_p, cp, "END",
                            round(pnl, 4), round(pnl, 4), n - 1 - entry_bar))

    return _calc_stats(symbol, params, trades)


# ── Подсчёт статистики ────────────────────────────────────────────────────────
def _calc_stats(symbol: str, params: dict, trades: list[Trade]) -> dict:
    if not trades:
        return {"symbol": symbol, "params": params, "n_trades": 0,
                "total_pnl": 0.0, "win_rate": 0.0, "profit_factor": 0.0,
                "max_dd": 0.0, "sharpe": 0.0, "avg_pnl": 0.0,
                "avg_win": 0.0, "avg_loss": 0.0, "score": -999.0}

    pnls    = [t.pnl_pct for t in trades]
    wins    = [p for p in pnls if p > 0]
    losses  = [p for p in pnls if p <= 0]

    total_pnl = sum(pnls)
    win_rate  = len(wins) / len(pnls) if pnls else 0
    avg_win   = float(np.mean(wins))   if wins   else 0.0
    avg_loss  = float(np.mean(losses)) if losses else 0.0

    gross_profit = sum(wins)
    gross_loss   = abs(sum(losses))
    pf = gross_profit / (gross_loss + 1e-9)

    # Максимальная просадка по кумулятивному PnL
    cum = np.cumsum(pnls)
    peak = np.maximum.accumulate(cum)
    dd   = float(np.min(cum - peak))

    # Sharpe (ежесделочный)
    sharpe = float(np.mean(pnls) / (np.std(pnls) + 1e-9)) * math.sqrt(252) \
             if len(pnls) >= 3 else 0.0

    # Итоговый скор — несколько улучшений:
    # 1. Если PnL отрицательный — скор сразу отрицательный (без артефактов)
    # 2. dd_factor зажат в [0, 1] — исключает двойной минус при просадке >100%
    # 3. Штраф за маленькое число сделок и за слишком большое (> 500/90д = шум)
    n_penalty  = min(1.0, len(trades) / 10)
    trade_spam = max(1.0, len(trades) / 500)  # штраф за > 500 сделок
    dd_factor  = max(0.0, 1.0 - abs(dd) / 100)   # зажато в [0, 1]
    if total_pnl <= 0:
        score = total_pnl  # убыточные стратегии — просто отрицательный скор
    else:
        score = total_pnl * win_rate * dd_factor * n_penalty / trade_spam

    exit_counts = {}
    for t in trades:
        exit_counts[t.exit_reason] = exit_counts.get(t.exit_reason, 0) + 1

    return {
        "symbol":       symbol,
        "params":       params,
        "n_trades":     len(trades),
        "total_pnl":    round(total_pnl, 3),
        "win_rate":     round(win_rate, 3),
        "avg_pnl":      round(float(np.mean(pnls)), 4),
        "avg_win":      round(avg_win,  4),
        "avg_loss":     round(avg_loss, 4),
        "profit_factor": round(pf, 3),
        "max_dd":       round(dd, 3),
        "sharpe":       round(sharpe, 3),
        "score":        round(score, 4),
        "exit_counts":  exit_counts,
        "trades":       trades,
    }


# ── Оптимизация параметров ────────────────────────────────────────────────────
def optimize(symbol: str, df: pd.DataFrame, payload: dict,
             btc_df: pd.DataFrame | None) -> list[dict]:
    """
    1. Собираем все фичи в матрицу
    2. predict_proba вызываем ОДИН РАЗ на пару (batch) — ускорение ×800
    3. Быстро перебираем все комбинации параметров
    """
    model         = payload["model"]
    label_encoder = payload.get("label_encoder")
    feature_names = payload["meta"]["features"]
    uses_btc      = payload["meta"].get("btc_features", False) and symbol != "BTCUSDT"

    n = len(df)
    print(f"  [{symbol}] Вычисляем фичи для {n:,} баров...", end="", flush=True)

    # Кэшируем BTC closes как numpy array — избегаем iloc на каждом баре
    btc_closes_cache = btc_df["close"].values.astype(float) \
                       if uses_btc and btc_df is not None else None

    # ── Шаг 1: собираем фичи по всем барам в матрицу ─────────────────────────
    pre_signals = [None] * n
    valid_bars: list[int]    = []   # индексы баров с валидными фичами
    feature_rows: list[np.ndarray] = []  # векторы фичей

    for i in range(WARMUP_BARS, n):
        if btc_closes_cache is not None:
            btc_slice = btc_closes_cache[max(0, i - WARMUP_BARS): i + 1]
            btc_w = pd.DataFrame({"close": btc_slice})
        else:
            btc_w = None
        fv = build_features(df, i, btc_w, feature_names)
        if fv is None:
            pre_signals[i] = ("HOLD", 0.0)
        else:
            valid_bars.append(i)
            feature_rows.append(fv)

    print(f" {len(valid_bars):,} баров | predict...", end="", flush=True)

    # ── Шаг 2: ОДИН batch predict_proba вместо 129,600 одиночных вызовов ─────
    # Benchmark показал: DataFrame(1 строка)/сек = 234 → 120 мин
    #                    batch(129k строк)/сек = 187,388 → 0.1 мин  (×800 быстрее!)
    if feature_rows:
        try:
            batch_matrix = np.array(feature_rows, dtype=float)
            batch_df     = pd.DataFrame(batch_matrix, columns=feature_names)
            all_probas   = model.predict_proba(batch_df)   # ОДИН вызов!

            for idx, bar in enumerate(valid_bars):
                proba = all_probas[idx]
                best  = int(np.argmax(proba))
                conf  = float(proba[best])
                raw   = model.classes_[best]
                raw_s = str(raw)
                if raw_s in ("BUY", "SELL", "HOLD"):
                    sig = raw_s
                elif label_encoder is not None:
                    try:
                        sig = str(label_encoder.inverse_transform([int(raw)])[0])
                    except Exception:
                        sig = "HOLD"
                else:
                    sig = "HOLD"
                pre_signals[bar] = (sig, conf)
        except Exception as e:
            # Фолбэк на поштучный режим если batch не сработал
            print(f"\n  ⚠️ batch predict упал ({e}), переключаемся на поштучный...",
                  end="", flush=True)
            for idx, bar in enumerate(valid_bars):
                try:
                    fv_df = pd.DataFrame(feature_rows[idx].reshape(1, -1),
                                         columns=feature_names)
                    proba = model.predict_proba(fv_df)[0]
                    best  = int(np.argmax(proba))
                    conf  = float(proba[best])
                    raw   = str(model.classes_[best])
                    sig   = raw if raw in ("BUY", "SELL", "HOLD") else "HOLD"
                    pre_signals[bar] = (sig, conf)
                except Exception:
                    pre_signals[bar] = ("HOLD", 0.0)

    # Строим сетку
    keys   = list(PARAM_GRID.keys())
    values = list(PARAM_GRID.values())
    combos = list(itertools.product(*values))
    buy_count = sum(1 for p in pre_signals if p and p[0] == "BUY")
    print(f" {buy_count:,} BUY | {len(combos)} комбо")

    results = []
    for combo in combos:
        params = dict(zip(keys, combo))
        r = backtest_pair_fast(symbol, params, df, pre_signals)
        results.append(r)

    results.sort(key=lambda x: x["score"], reverse=True)
    return results


# ── HTML отчёт ────────────────────────────────────────────────────────────────
HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="UTF-8">
<title>Backtest Report — {timestamp}</title>
<link href="https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@400;700&family=Inter:wght@300;400;600;700&display=swap" rel="stylesheet">
<style>
:root {{
  --bg: #080a0f; --panel: #0d1117; --border: #1c2333;
  --accent: #58a6ff; --green: #3fb950; --red: #f85149;
  --yellow: #d29922; --text: #c9d1d9; --muted: #6e7681;
  --mono: 'JetBrains Mono', monospace; --sans: 'Inter', sans-serif;
}}
* {{ box-sizing: border-box; margin: 0; padding: 0; }}
body {{ background: var(--bg); color: var(--text); font-family: var(--sans); font-size: 13px; }}
.page {{ max-width: 1400px; margin: 0 auto; padding: 24px 20px; }}
h1 {{ font-size: 20px; font-weight: 700; color: #fff; margin-bottom: 4px; }}
.subtitle {{ color: var(--muted); font-size: 12px; margin-bottom: 28px; font-family: var(--mono); }}

/* Summary cards */
.cards {{ display: grid; grid-template-columns: repeat(auto-fill, minmax(160px, 1fr)); gap: 12px; margin-bottom: 32px; }}
.card {{ background: var(--panel); border: 1px solid var(--border); border-radius: 10px; padding: 14px 16px; }}
.card-label {{ font-size: 10px; color: var(--muted); text-transform: uppercase; letter-spacing: 1.5px; margin-bottom: 6px; }}
.card-value {{ font-size: 22px; font-weight: 700; font-family: var(--mono); }}
.pos {{ color: var(--green); }} .neg {{ color: var(--red); }} .neu {{ color: var(--accent); }}

/* Best params */
.best-box {{ background: var(--panel); border: 1px solid var(--border); border-radius: 10px;
             padding: 18px 20px; margin-bottom: 32px; }}
.best-box h2 {{ font-size: 13px; text-transform: uppercase; letter-spacing: 2px; color: var(--accent);
                margin-bottom: 14px; }}
.param-grid {{ display: flex; flex-wrap: wrap; gap: 10px; }}
.param-chip {{ background: #161b22; border: 1px solid var(--border); border-radius: 6px;
               padding: 6px 12px; font-family: var(--mono); font-size: 12px; }}
.param-chip span {{ color: var(--accent); font-weight: 700; }}

/* Tabs */
.tabs {{ display: flex; gap: 4px; margin-bottom: 16px; border-bottom: 1px solid var(--border); padding-bottom: 0; }}
.tab {{ padding: 8px 16px; cursor: pointer; border-radius: 6px 6px 0 0; font-size: 12px;
        color: var(--muted); background: none; border: none; transition: all .15s; }}
.tab.active {{ background: var(--panel); color: #fff; border: 1px solid var(--border); border-bottom: 1px solid var(--panel); }}
.tab-content {{ display: none; }} .tab-content.active {{ display: block; }}

/* Tables */
table {{ width: 100%; border-collapse: collapse; font-size: 12px; }}
th {{ background: var(--panel); color: var(--muted); text-align: left; padding: 8px 10px;
      font-weight: 600; font-size: 10px; text-transform: uppercase; letter-spacing: 1px;
      border-bottom: 1px solid var(--border); position: sticky; top: 0; }}
td {{ padding: 7px 10px; border-bottom: 1px solid #0d1117; font-family: var(--mono); }}
tr:hover td {{ background: rgba(88,166,255,.04); }}
.tbl-wrap {{ background: var(--panel); border: 1px solid var(--border); border-radius: 10px; overflow: hidden; max-height: 600px; overflow-y: auto; }}

/* Per-pair results */
.pair-section {{ margin-bottom: 28px; }}
.pair-header {{ display: flex; align-items: center; gap: 12px; margin-bottom: 10px; }}
.pair-name {{ font-size: 15px; font-weight: 700; color: #fff; }}
.pair-score {{ font-family: var(--mono); font-size: 12px; padding: 3px 10px; border-radius: 4px;
               background: rgba(88,166,255,.12); color: var(--accent); }}
.mini-stats {{ display: flex; gap: 16px; font-family: var(--mono); font-size: 11px; color: var(--muted); }}

/* PnL chart */
.chart-bar {{ height: 4px; border-radius: 2px; display: inline-block; }}

/* Score badge */
.score-badge {{ font-family: var(--mono); font-size: 11px; padding: 2px 8px; border-radius: 12px; }}
.score-hi  {{ background: rgba(63,185,80,.15);  color: var(--green); }}
.score-mid {{ background: rgba(210,153,34,.15); color: var(--yellow); }}
.score-lo  {{ background: rgba(248,81,73,.15);  color: var(--red); }}

.top-badge {{ font-size: 10px; background: linear-gradient(135deg,#58a6ff,#bc8cff);
              color: #000; padding: 2px 8px; border-radius: 10px; font-weight: 700; }}
</style>
</head>
<body>
<div class="page">
  <h1>⚡ Backtest &amp; Optimization Report</h1>
  <div class="subtitle">{timestamp} · данные: {data_dir} · пар: {n_pairs} · комбинаций: {n_combos}</div>

  <!-- Summary -->
  <div class="cards">
    <div class="card"><div class="card-label">Лучший PnL/пару</div>
      <div class="card-value {best_pnl_cls}">{best_pnl}%</div></div>
    <div class="card"><div class="card-label">Средний Win Rate</div>
      <div class="card-value neu">{avg_wr}%</div></div>
    <div class="card"><div class="card-label">Средний Sharpe</div>
      <div class="card-value neu">{avg_sharpe}</div></div>
    <div class="card"><div class="card-label">Всего сделок</div>
      <div class="card-value neu">{total_trades}</div></div>
    <div class="card"><div class="card-label">Profit Factor</div>
      <div class="card-value {pf_cls}">{avg_pf}</div></div>
    <div class="card"><div class="card-label">Макс просадка</div>
      <div class="card-value neg">{worst_dd}%</div></div>
  </div>

  <!-- Best params -->
  <div class="best-box">
    <h2>🏆 Лучшие параметры (по совокупному скору)</h2>
    <div class="param-grid">
      <div class="param-chip">TP <span>{bp_tp}%</span></div>
      <div class="param-chip">SL <span>{bp_sl}%</span></div>
      <div class="param-chip">Min Conf <span>{bp_conf}%</span></div>
      <div class="param-chip">Выход SELL ≥ <span>{bp_sc}%</span></div>
      <div class="param-chip">Безубыток <span>{bp_be}%</span></div>
      <div class="param-chip">Тренд фильтр <span>{bp_tf}</span></div>
    </div>
  </div>

  <!-- Tabs -->
  <div class="tabs">
    <button class="tab active" onclick="switchTab('top')">Топ параметров</button>
    <button class="tab" onclick="switchTab('pairs')">По парам</button>
    <button class="tab" onclick="switchTab('trades')">Все сделки</button>
  </div>

  <!-- Tab: Top params -->
  <div id="tab-top" class="tab-content active">
    <div class="tbl-wrap">
    <table>
      <thead><tr>
        <th>#</th><th>TP</th><th>SL</th><th>Conf</th>
        <th>SellClose</th><th>Безубыток</th><th>ТрендФ</th>
        <th>Сделок</th><th>PnL %</th><th>WR</th><th>PF</th>
        <th>MaxDD</th><th>Sharpe</th><th>Score</th>
      </tr></thead>
      <tbody>{top_rows}</tbody>
    </table></div>
  </div>

  <!-- Tab: Per-pair -->
  <div id="tab-pairs" class="tab-content">
    {pair_sections}
  </div>

  <!-- Tab: All trades -->
  <div id="tab-trades" class="tab-content">
    <div class="tbl-wrap">
    <table>
      <thead><tr>
        <th>Пара</th><th>Вход бар</th><th>Выход бар</th>
        <th>Цена входа</th><th>Цена выхода</th><th>Причина</th>
        <th>PnL %</th><th>Баров</th>
      </tr></thead>
      <tbody>{all_trade_rows}</tbody>
    </table></div>
  </div>
</div>

<script>
function switchTab(name) {{
  document.querySelectorAll('.tab-content').forEach(el => el.classList.remove('active'));
  document.querySelectorAll('.tab').forEach(el => el.classList.remove('active'));
  document.getElementById('tab-' + name).classList.add('active');
  event.target.classList.add('active');
}}
</script>
</body></html>"""


def build_report(all_results: dict, best_params: dict,
                 data_dir: Path, n_combos: int) -> str:
    """Строит HTML отчёт из результатов оптимизации."""

    # Агрегируем по параметрам (сумма score по всем парам)
    params_agg: dict[str, dict] = {}
    for sym, results in all_results.items():
        for r in results[:50]:   # топ-50 по каждой паре
            key = json.dumps(r["params"], sort_keys=True)
            if key not in params_agg:
                params_agg[key] = {"params": r["params"], "score": 0.0,
                                   "n_trades": 0, "total_pnl": 0.0,
                                   "win_rates": [], "sharpes": [], "pfs": [], "dds": []}
            params_agg[key]["score"]     += r["score"]
            params_agg[key]["n_trades"]  += r["n_trades"]
            params_agg[key]["total_pnl"] += r["total_pnl"]
            params_agg[key]["win_rates"].append(r["win_rate"])
            params_agg[key]["sharpes"].append(r["sharpe"])
            params_agg[key]["pfs"].append(r["profit_factor"])
            params_agg[key]["dds"].append(r["max_dd"])

    top_combined = sorted(params_agg.values(), key=lambda x: x["score"], reverse=True)[:30]

    # Top rows
    top_rows_html = ""
    for rank, row in enumerate(top_combined, 1):
        p  = row["params"]
        sc = row["score"]
        wr = round(float(np.mean(row["win_rates"])) * 100, 1)
        pf = round(float(np.mean(row["pfs"])), 2)
        sh = round(float(np.mean(row["sharpes"])), 2)
        dd = round(float(np.min(row["dds"])), 2)
        sc_cls = "score-hi" if sc > 5 else ("score-mid" if sc > 0 else "score-lo")
        badge  = f'<span class="top-badge">TOP {rank}</span>' if rank <= 3 else ""
        sc_val = f'<span class="score-badge {sc_cls}">{sc:.2f}</span>'
        be_disp = "ВЫКЛ" if p["breakeven_trig"] >= 99 else f"{p['breakeven_trig']}%"
        sc_disp = "ВЫКЛ" if p["sell_close_conf"] >= 1 else f"{p['sell_close_conf']*100:.0f}%"
        top_rows_html += (
            f"<tr><td>{badge}{rank}</td>"
            f"<td>{p['tp_pct']*100:.1f}%</td><td>{p['sl_pct']*100:.1f}%</td>"
            f"<td>{p['min_conf']*100:.0f}%</td><td>{sc_disp}</td>"
            f"<td>{be_disp}</td><td>{'ВКЛ' if p['trend_filter'] else 'ВЫКЛ'}</td>"
            f"<td>{row['n_trades']}</td>"
            f"<td class='{'pos' if row['total_pnl']>0 else 'neg'}'>{row['total_pnl']:+.1f}%</td>"
            f"<td>{wr}%</td><td>{pf}</td><td class='neg'>{dd:.1f}%</td>"
            f"<td>{sh}</td><td>{sc_val}</td></tr>\n"
        )

    # Per-pair sections
    pair_sections_html = ""
    all_best_trades = []
    for sym, results in sorted(all_results.items()):
        if not results:
            continue
        best = results[0]
        p    = best["params"]
        be_d = "ВЫКЛ" if p["breakeven_trig"] >= 99 else f"{p['breakeven_trig']}%"
        sc_d = "ВЫКЛ" if p["sell_close_conf"] >= 1 else f"{p['sell_close_conf']*100:.0f}%"
        trades_html = ""
        for t in (best.get("trades") or [])[:50]:
            pcls = "pos" if t.pnl_pct > 0 else "neg"
            trades_html += (
                f"<tr><td>{t.entry_bar}</td><td>{t.exit_bar}</td>"
                f"<td>{t.entry_price:.5g}</td><td>{t.exit_price:.5g}</td>"
                f"<td>{t.exit_reason}</td>"
                f"<td class='{pcls}'>{t.pnl_pct:+.3f}%</td>"
                f"<td>{t.holding_bars}</td></tr>\n"
            )
        ec = best.get("exit_counts", {})
        ec_str = " | ".join(f"{k}:{v}" for k, v in ec.items())
        pair_sections_html += f"""
        <div class="pair-section">
          <div class="pair-header">
            <span class="pair-name">{sym}</span>
            <span class="pair-score">score {best['score']:.2f}</span>
          </div>
          <div class="mini-stats">
            <span>Сделок: <b>{best['n_trades']}</b></span>
            <span class="{'pos' if best['total_pnl']>0 else 'neg'}">PnL: {best['total_pnl']:+.2f}%</span>
            <span>WR: {best['win_rate']*100:.1f}%</span>
            <span>PF: {best['profit_factor']}</span>
            <span>DD: {best['max_dd']:.1f}%</span>
            <span>Sharpe: {best['sharpe']}</span>
            <span style="color:#6e7681">{ec_str}</span>
          </div>
          <div style="margin:8px 0 10px; font-size:11px; color:#6e7681; font-family:var(--mono)">
            TP={p['tp_pct']*100:.1f}% SL={p['sl_pct']*100:.1f}%
            Conf≥{p['min_conf']*100:.0f}%
            SellClose={sc_d} BE={be_d}
            Filter={'ВКЛ' if p['trend_filter'] else 'ВЫКЛ'}
          </div>
          <div class="tbl-wrap" style="max-height:300px">
          <table><thead><tr>
            <th>Вход</th><th>Выход</th><th>Цена вх</th><th>Цена вых</th>
            <th>Причина</th><th>PnL %</th><th>Баров</th>
          </tr></thead><tbody>{trades_html}</tbody></table>
          </div>
        </div>"""

        all_best_trades.extend(best.get("trades") or [])

    # All trades
    all_best_trades.sort(key=lambda t: t.pnl_pct, reverse=True)
    all_trade_rows = ""
    for t in all_best_trades[:200]:
        pcls = "pos" if t.pnl_pct > 0 else "neg"
        all_trade_rows += (
            f"<tr><td>{t.symbol}</td><td>{t.entry_bar}</td><td>{t.exit_bar}</td>"
            f"<td>{t.entry_price:.5g}</td><td>{t.exit_price:.5g}</td>"
            f"<td>{t.exit_reason}</td>"
            f"<td class='{pcls}'>{t.pnl_pct:+.3f}%</td>"
            f"<td>{t.holding_bars}</td></tr>\n"
        )

    # Summary stats
    all_top = [r for rs in all_results.values() for r in rs[:1]]
    best_pnl  = max((r["total_pnl"] for r in all_top), default=0)
    avg_wr    = round(float(np.mean([r["win_rate"] for r in all_top])) * 100, 1) if all_top else 0
    avg_sh    = round(float(np.mean([r["sharpe"] for r in all_top])), 2) if all_top else 0
    total_tr  = sum(r["n_trades"] for r in all_top)
    avg_pf    = round(float(np.mean([r["profit_factor"] for r in all_top])), 2) if all_top else 0
    worst_dd  = round(float(np.min([r["max_dd"] for r in all_top])), 2) if all_top else 0

    bp = best_params
    be_bp  = "ВЫКЛ" if bp.get("breakeven_trig", 99) >= 99 else f"{bp['breakeven_trig']}%"
    sc_bp  = "ВЫКЛ" if bp.get("sell_close_conf", 1.01) >= 1 else f"{bp['sell_close_conf']*100:.0f}%"

    return HTML_TEMPLATE.format(
        timestamp   = datetime.now().strftime("%Y-%m-%d %H:%M"),
        data_dir    = str(data_dir),
        n_pairs     = len(all_results),
        n_combos    = n_combos,
        best_pnl    = f"{best_pnl:+.2f}",
        best_pnl_cls= "pos" if best_pnl > 0 else "neg",
        avg_wr      = avg_wr,
        avg_sharpe  = avg_sh,
        total_trades= total_tr,
        avg_pf      = avg_pf,
        pf_cls      = "pos" if avg_pf > 1 else "neg",
        worst_dd    = worst_dd,
        bp_tp       = bp.get("tp_pct", 0.02) * 100,
        bp_sl       = bp.get("sl_pct", 0.01) * 100,
        bp_conf     = int(bp.get("min_conf", 0.6) * 100),
        bp_sc       = sc_bp,
        bp_be       = be_bp,
        bp_tf       = "ВКЛ" if bp.get("trend_filter", True) else "ВЫКЛ",
        top_rows    = top_rows_html,
        pair_sections = pair_sections_html,
        all_trade_rows= all_trade_rows,
    )


# ── Точка входа ───────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(description="Backtester ML стратегии")
    parser.add_argument("--pair",        default=None, help="Одна пара (напр. BTCUSDT)")
    parser.add_argument("--no-optimize", action="store_true", help="Только бэктест, без оптимизации")
    parser.add_argument("--top",         type=int, default=5, help="Показать топ-N параметров")
    args = parser.parse_args()

    pairs = [args.pair] if args.pair else PAIRS

    print("=" * 65)
    print("  ⚡ Backtester + Optimizer")
    print("=" * 65)
    print(f"  Данные  : {DATA_DIR}")
    print(f"  Модели  : {MODELS_DIR}")
    print(f"  Пары    : {len(pairs)}")
    if not args.no_optimize:
        n_combos = 1
        for v in PARAM_GRID.values(): n_combos *= len(v)
        print(f"  Комбин. : {n_combos} на пару")
    print("=" * 65)

    # Загружаем BTC один раз
    btc_df = None
    raw_btc = load_df("BTCUSDT")
    if raw_btc is not None:
        btc_df = raw_btc
        print(f"  BTC     : {len(btc_df):,} свечей загружено")

    all_results: dict[str, list] = {}

    for symbol in pairs:
        payload = load_model(symbol)
        if payload is None:
            print(f"  ⚠️  {symbol}: модель не найдена — пропускаем")
            continue

        df = load_df(symbol)
        if df is None or len(df) < WARMUP_BARS + 100:
            print(f"  ⚠️  {symbol}: недостаточно данных — пропускаем")
            continue

        if args.no_optimize:
            default_params = {
                "tp_pct": 0.020, "sl_pct": 0.010, "min_conf": 0.60,
                "sell_close_conf": 0.70, "breakeven_trig": 1.0, "trend_filter": True,
            }
            n_combos = 1
            r = backtest_pair(symbol, default_params, df, payload, btc_df)
            all_results[symbol] = [r]
            print(f"  {symbol:12s} → trades={r['n_trades']:3d}  "
                  f"pnl={r['total_pnl']:+.2f}%  "
                  f"wr={r['win_rate']*100:.1f}%  "
                  f"sharpe={r['sharpe']:.2f}")
        else:
            n_combos = 1
            for v in PARAM_GRID.values(): n_combos *= len(v)
            results = optimize(symbol, df, payload,
                               btc_df if symbol != "BTCUSDT" else None)
            all_results[symbol] = results
            best = results[0]
            p    = best["params"]
            print(f"  {symbol:12s} → TOP: tp={p['tp_pct']*100:.1f}% "
                  f"sl={p['sl_pct']*100:.1f}% "
                  f"conf={p['min_conf']*100:.0f}%  "
                  f"trades={best['n_trades']}  "
                  f"pnl={best['total_pnl']:+.2f}%  "
                  f"wr={best['win_rate']*100:.1f}%  "
                  f"score={best['score']:.2f}")

    if not all_results:
        print("❌ Нет результатов для отчёта")
        return

    # Находим глобально лучшие параметры (сумма score по всем парам)
    params_score: dict[str, float] = {}
    params_map:   dict[str, dict]  = {}
    for sym, results in all_results.items():
        for r in results[:20]:
            key = json.dumps(r["params"], sort_keys=True)
            params_score[key] = params_score.get(key, 0.0) + r["score"]
            params_map[key]   = r["params"]

    best_key    = max(params_score, key=params_score.__getitem__)
    best_params = params_map[best_key]

    # Сохраняем лучшие параметры
    best_out = {
        "generated_at": datetime.now().isoformat(),
        "params":        best_params,
        "combined_score": round(params_score[best_key], 4),
        "note": "Применить: скопируй значения в настройки бота",
    }
    bp_path = OUT_DIR / "best_params.json"
    with open(bp_path, "w", encoding="utf-8") as f:
        json.dump(best_out, f, indent=2, ensure_ascii=False)

    # ── Сохраняем индивидуальные параметры по парам ───────────────────────────
    pair_params_out: dict = {
        "_meta": {
            "description": "Индивидуальные параметры TP/SL/conf для каждой пары",
            "generated_at": datetime.now().isoformat(),
            "source": f"backtester.py — данные: {DATA_DIR}",
            "bars": max((len(load_df(s) or []) for s in all_results), default=0),
            "note": "Автогенерация. Редактировать вручную или перезапустить бэктестер."
        }
    }
    for sym, results in all_results.items():
        if not results:
            continue
        best = results[0]
        p = best["params"]
        pair_params_out[sym] = {
            "tp_pct":   round(p["tp_pct"] * 100, 1),
            "sl_pct":   round(p["sl_pct"] * 100, 1),
            "min_conf": round(p["min_conf"], 2),
            "n_trades": best["n_trades"],
            "pnl":      best["total_pnl"],
            "wr":       round(best["win_rate"] * 100, 1),
            "score":    best["score"],
        }

    # Сохраняем рядом с ботом (не в backtest_results, а в корень проекта)
    pp_path = BASE_DIR / "pair_params.json"
    with open(pp_path, "w", encoding="utf-8") as f:
        json.dump(pair_params_out, f, indent=2, ensure_ascii=False)

    # Также копию в backtest_results для истории
    pp_path_archive = OUT_DIR / f"pair_params_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    with open(pp_path_archive, "w", encoding="utf-8") as f:
        json.dump(pair_params_out, f, indent=2, ensure_ascii=False)

    # Генерируем HTML отчёт
    ts        = datetime.now().strftime("%Y%m%d_%H%M%S")
    html      = build_report(all_results, best_params, DATA_DIR, n_combos)
    html_path = OUT_DIR / f"report_{ts}.html"
    with open(html_path, "w", encoding="utf-8") as f:
        f.write(html)

    print()
    print("=" * 65)
    print(f"  🏆 Лучшие параметры (глобальные):")
    print(f"     TP={best_params['tp_pct']*100:.1f}%  "
          f"SL={best_params['sl_pct']*100:.1f}%  "
          f"Conf≥{best_params['min_conf']*100:.0f}%")
    be_d = "ВЫКЛ" if best_params["breakeven_trig"] >= 99 else f"{best_params['breakeven_trig']}%"
    sc_d = "ВЫКЛ" if best_params["sell_close_conf"] >= 1 else f"{best_params['sell_close_conf']*100:.0f}%"
    print(f"     Выход SELL≥{sc_d}  "
          f"Безубыток={be_d}  "
          f"ТрендФильтр={'ВКЛ' if best_params['trend_filter'] else 'ВЫКЛ'}")
    print()
    print(f"  📌 Индивидуальные параметры по парам:")
    for sym, p in pair_params_out.items():
        if sym.startswith("_"):
            continue
        print(f"     {sym:12s} tp={p['tp_pct']:.1f}%  sl={p['sl_pct']:.1f}%  "
              f"conf={int(p['min_conf']*100)}%  "
              f"pnl={p['pnl']:>+7.2f}%  wr={p['wr']:.1f}%")
    print()
    print(f"  💾 Глобальные  → {bp_path}")
    print(f"  📊 По парам    → {pp_path}")
    print(f"  📄 Отчёт       → {html_path}")
    print("=" * 65)


if __name__ == "__main__":
    main()
