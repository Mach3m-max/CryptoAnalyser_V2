from typing import Optional
#!/usr/bin/env python3
"""
compute_historical_indicators.py
=================================
Ретроактивно вычисляет все индикаторы на исторических данных и добавляет
авто-разметку BUY/SELL/HOLD.

v1 verbatim — ЕДИНСТВЕННОЕ изменение: все пути → data/historical/

Источник данных (в порядке приоритета):
  1. data/historical/{SYMBOL}_*.csv       ← загруженные download_history.py
  2. data/historical/{SYMBOL}_30d.csv     ← текущий кэш бота

Результат:
  data/historical/{SYMBOL}_indicators_labeled.csv

Запуск:
  python compute_historical_indicators.py
  python compute_historical_indicators.py --pairs BTCUSDT ETHUSDT
  python compute_historical_indicators.py --tp 0.02 --sl 0.01 --lookahead 120
"""

from __future__ import annotations
import argparse
import os
import sys
import time
import csv
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

# ── Пути (v2: всё в data/historical/) ────────────────────────────────────────
BASE_DIR = Path(__file__).parent
DATA_DIR = BASE_DIR / "data" / "historical"
OUT_DIR  = DATA_DIR   # пишем туда же

# ── Параметры разметки (совпадают с train.py) ─────────────────────────────────
TP_PCT    = 0.020    # +2% = Take Profit
SL_PCT    = 0.010    # -1% = Stop Loss
LOOKAHEAD = 120      # минут вперёд для определения исхода

# ── Портфель ─────────────────────────────────────────────────────────────────
DEFAULT_PAIRS = [
    "BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT",
    "AVAXUSDT", "APTUSDT", "WUSDT",  "OPUSDT",
    "TIAUSDT",  "ATOMUSDT","WIFUSDT","ARBUSDT",
    "XAUTUSDT", "INJUSDT", "SUIUSDT",
]


# ══════════════════════════════════════════════════════════════════════════════
# Загрузка данных
# ══════════════════════════════════════════════════════════════════════════════

def find_csv(symbol: str) -> Optional[Path]:
    """Ищет лучший CSV для символа в data/historical/"""
    if DATA_DIR.exists():
        found = [f for f in DATA_DIR.glob(f"{symbol}_*.csv")
                 if 'labeled' not in f.name and 'indicators' not in f.name]
        if found:
            return max(found, key=lambda p: p.stat().st_size)

    # Фолбек: _30d.csv
    p = DATA_DIR / f"{symbol}_30d.csv"
    if p.exists():
        return p

    return None


def find_all_csvs(symbol: str) -> list:
    """Возвращает все источники данных для символа."""
    sources = []
    if DATA_DIR.exists():
        found = [f for f in DATA_DIR.glob(f"{symbol}_*.csv")
                 if 'labeled' not in f.name and 'indicators' not in f.name]
        if found:
            sources.append(max(found, key=lambda p: p.stat().st_size))
    fresh = DATA_DIR / f"{symbol}_30d.csv"
    if fresh.exists() and fresh not in sources:
        sources.append(fresh)
    return sources


def load_candles(path: Path) -> Optional[pd.DataFrame]:
    try:
        df = pd.read_csv(path, parse_dates=["timestamp"])
        df = df.sort_values("timestamp").reset_index(drop=True)
        for col in ("open", "high", "low", "close", "volume"):
            if col in df.columns:
                df[col] = pd.to_numeric(df[col], errors="coerce")
        df = df.dropna(subset=["close"])
        return df
    except Exception as e:
        print(f"  ❌ Ошибка загрузки {path.name}: {e}")
        return None


# ══════════════════════════════════════════════════════════════════════════════
# Индикаторы
# ══════════════════════════════════════════════════════════════════════════════

def compute_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """Вычисляет все 51 индикатор для всего датафрейма."""
    c = df["close"].values
    h = df["high"].values
    l = df["low"].values
    v = df["volume"].values if "volume" in df.columns else np.ones(len(c))
    n = len(c)

    # ── SMA и отклонения ─────────────────────────────────────────────────────
    for w in (50, 75, 100, 150, 200):
        sma = pd.Series(c).rolling(w).mean().values
        df[f"sma_{w}"]   = sma
        df[f"dev_{w}"]   = np.where(sma > 0, (c - sma) / sma * 100, np.nan)

    df["avg_deviation"] = df[[f"dev_{w}" for w in (50,75,100,150,200)]].mean(axis=1)

    # ── RSI ───────────────────────────────────────────────────────────────────
    for period in (7, 14, 21):
        delta  = pd.Series(c).diff()
        gain   = delta.clip(lower=0)
        loss   = (-delta).clip(lower=0)
        avg_g  = gain.ewm(alpha=1/period, min_periods=period).mean()
        avg_l  = loss.ewm(alpha=1/period, min_periods=period).mean()
        rs     = avg_g / avg_l.replace(0, 1e-10)
        df[f"rsi_{period}"] = 100 - 100 / (1 + rs)

    # ── Bollinger Bands ───────────────────────────────────────────────────────
    roll20        = pd.Series(c).rolling(20)
    df["bb_mid"]  = roll20.mean()
    bb_std        = roll20.std()
    df["bb_upper"]= df["bb_mid"] + 2 * bb_std
    df["bb_lower"]= df["bb_mid"] - 2 * bb_std
    bb_range      = (df["bb_upper"] - df["bb_lower"]).replace(0, 1e-10)
    df["bb_position"] = (c - df["bb_lower"].values) / bb_range
    df["bb_width"]    = bb_range / df["bb_mid"].replace(0, 1e-10)
    df["bb_squeeze"]  = (df["bb_width"] < df["bb_width"].rolling(50).mean() * 0.8).astype(int)

    # ── ATR ───────────────────────────────────────────────────────────────────
    tr      = np.maximum(h - l, np.maximum(
        np.abs(h - np.roll(c, 1)), np.abs(l - np.roll(c, 1))))
    tr[0]   = h[0] - l[0]
    df["atr_14"]    = pd.Series(tr).ewm(span=14, min_periods=14).mean()
    df["atr_pct"]   = df["atr_14"] / pd.Series(c).replace(0, 1e-10) * 100
    df["atr_ratio"] = df["atr_14"] / df["atr_14"].rolling(50).mean().replace(0, 1e-10)

    # ── MACD ──────────────────────────────────────────────────────────────────
    ema12          = pd.Series(c).ewm(span=12, min_periods=12).mean()
    ema26          = pd.Series(c).ewm(span=26, min_periods=26).mean()
    macd_line      = ema12 - ema26
    signal_line    = macd_line.ewm(span=9, min_periods=9).mean()
    macd_hist      = macd_line - signal_line
    df["macd"]          = macd_line
    df["macd_signal"]   = signal_line
    df["macd_hist"]     = macd_hist
    df["macd_hist_norm"]= macd_hist / df["atr_14"].replace(0, 1e-10)
    df["macd_cross"]    = np.where(
        (macd_hist > 0) & (macd_hist.shift(1) <= 0), 1,
        np.where((macd_hist < 0) & (macd_hist.shift(1) >= 0), -1, 0))

    # ── Изменения цены ────────────────────────────────────────────────────────
    s_c = pd.Series(c)
    for lag, name in ((1,"1m"),(5,"5m"),(15,"15m"),(30,"30m"),(60,"60m")):
        df[f"price_change_{name}"] = s_c.pct_change(lag) * 100

    # ── Объём ─────────────────────────────────────────────────────────────────
    s_v          = pd.Series(v)
    df["volume_last"]  = v
    df["vol_ratio"]    = s_v / s_v.rolling(20).mean().replace(0, 1e-10)
    df["vol_change"]   = s_v.pct_change(5) * 100

    # ── OBV ───────────────────────────────────────────────────────────────────
    delta_c       = np.diff(c, prepend=c[0])
    obv_dir       = np.where(delta_c > 0, v, np.where(delta_c < 0, -v, 0))
    df["obv"]     = np.cumsum(obv_dir)

    # ── Percentile rank / Z-score ─────────────────────────────────────────────
    df["pct_rank_200"] = s_c.rolling(200).apply(
        lambda x: pd.Series(x).rank(pct=True).iloc[-1], raw=False)
    roll200_m  = s_c.rolling(200).mean()
    roll200_s  = s_c.rolling(200).std().replace(0, 1e-10)
    df["z_score_200"] = (s_c - roll200_m) / roll200_s

    # ── Donchian ──────────────────────────────────────────────────────────────
    don_high = pd.Series(h).rolling(100).max()
    don_low  = pd.Series(l).rolling(100).min()
    don_range = (don_high - don_low).replace(0, 1e-10)
    df["don_pos_100"] = (s_c - don_low) / don_range

    # ── Голосование SMA ───────────────────────────────────────────────────────
    buy_votes  = sum((df[f"dev_{w}"] < -2).astype(int) for w in (50,75,100,150,200))
    sell_votes = sum((df[f"dev_{w}"] >  2).astype(int) for w in (50,75,100,150,200))
    df["buy_votes"]    = buy_votes
    df["sell_votes"]   = sell_votes
    df["confidence"]   = df["avg_deviation"].abs() / 2.0

    return df


# ══════════════════════════════════════════════════════════════════════════════
# Разметка BUY/SELL/HOLD
# ══════════════════════════════════════════════════════════════════════════════

def label_data(df: pd.DataFrame, tp_pct: float, sl_pct: float,
               lookahead: int) -> pd.DataFrame:
    """Авторазметка: смотрим lookahead баров вперёд, определяем исход."""
    c     = df["close"].values
    n     = len(c)
    labels    = ["HOLD"] * n
    outcomes  = ["NONE"] * n

    for i in range(n - lookahead):
        entry = c[i]
        tp    = entry * (1 + tp_pct)
        sl    = entry * (1 - sl_pct)
        outcome = "HOLD"
        for j in range(i + 1, min(i + lookahead + 1, n)):
            if c[j] >= tp:
                outcome = "TP"; break
            if c[j] <= sl:
                outcome = "SL"; break
        outcomes[i] = outcome
        if outcome == "TP":
            labels[i] = "BUY"
        elif outcome == "SL":
            labels[i] = "SELL"
        else:
            labels[i] = "HOLD"

    df["label"]   = labels
    df["outcome"] = outcomes
    return df


# ══════════════════════════════════════════════════════════════════════════════
# Основная логика
# ══════════════════════════════════════════════════════════════════════════════

def process_pair(symbol: str, tp_pct: float, sl_pct: float,
                 lookahead: int, verbose: bool = True) -> bool:
    """Обрабатывает одну пару: индикаторы + разметка → CSV."""
    sources = find_all_csvs(symbol)
    if not sources:
        print(f"  ❌ {symbol}: CSV не найден в {DATA_DIR}")
        return False

    frames = []
    for src in sources:
        df_src = load_candles(src)
        if df_src is not None and len(df_src) > 200:
            frames.append(df_src)

    if not frames:
        print(f"  ❌ {symbol}: нет данных после загрузки")
        return False

    # Мержим все источники, дедупликация по timestamp
    df = pd.concat(frames, ignore_index=True)
    df = df.drop_duplicates("timestamp").sort_values("timestamp").reset_index(drop=True)

    if verbose:
        days = (df["timestamp"].max() - df["timestamp"].min()).days
        print(f"  📊 {symbol}: {len(df):,} свечей ({days}д) из {len(sources)} источника(ов)")

    # Индикаторы
    df = compute_indicators(df)

    # Разметка
    df = label_data(df, tp_pct, sl_pct, lookahead)

    # Статистика
    counts = df["label"].value_counts()
    total  = len(df)
    buy_r  = counts.get("BUY",  0) / total * 100
    sell_r = counts.get("SELL", 0) / total * 100
    hold_r = counts.get("HOLD", 0) / total * 100
    if verbose:
        print(f"  🏷️  Метки: BUY={counts.get('BUY',0)}({buy_r:.1f}%)  "
              f"SELL={counts.get('SELL',0)}({sell_r:.1f}%)  "
              f"HOLD={counts.get('HOLD',0)}({hold_r:.1f}%)")

    # Сохраняем
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    out_path = OUT_DIR / f"{symbol}_indicators_labeled.csv"
    df.to_csv(out_path, index=False)
    size_mb = out_path.stat().st_size / 1_048_576
    print(f"  ✅ {symbol}: сохранено → {out_path.name}  ({size_mb:.1f} МБ)")
    return True


def main():
    parser = argparse.ArgumentParser(
        description="Вычисление индикаторов и авторазметка исторических данных"
    )
    parser.add_argument("--pairs",     nargs="+", default=None,  metavar="PAIR")
    parser.add_argument("--tp",        type=float, default=TP_PCT)
    parser.add_argument("--sl",        type=float, default=SL_PCT)
    parser.add_argument("--lookahead", type=int,   default=LOOKAHEAD)
    parser.add_argument("--quiet",     action="store_true")
    args = parser.parse_args()

    pairs = args.pairs if args.pairs else DEFAULT_PAIRS

    print("=" * 65)
    print("  📐 Compute Historical Indicators v2")
    print(f"  TP={args.tp*100:.1f}%  SL={args.sl*100:.1f}%  Lookahead={args.lookahead}м")
    print(f"  Данные: {DATA_DIR}")
    print(f"  Пары:   {', '.join(pairs)}")
    print("=" * 65)

    success, failed = [], []
    t0 = time.time()

    for sym in pairs:
        print(f"\n[{pairs.index(sym)+1}/{len(pairs)}] {sym}")
        ok = process_pair(sym, args.tp, args.sl, args.lookahead, verbose=not args.quiet)
        (success if ok else failed).append(sym)

    print("\n" + "=" * 65)
    print(f"  ✅ Готово: {len(success)}/{len(pairs)}  за {(time.time()-t0)/60:.1f} мин")
    if failed:
        print(f"  ❌ Ошибки: {', '.join(failed)}")
    print(f"  Результаты: {OUT_DIR}")
    print("=" * 65)

    print("\n  Следующий шаг — обучение моделей:")
    print(f"  python ml/train.py")


if __name__ == "__main__":
    main()
