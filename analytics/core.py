# analytics/core.py
"""
Общий движок анализа торговли (decisions_*.csv).
Используется:
  - analytics/report.py       (CLI-скрипт, глубокий разбор + сверка с биржей)
  - dashboard/app.py /analytics (страница в дашборде, быстрый взгляд)

Ничего не пишет и не мутирует — только читает CSV и считает метрики.
"""
from __future__ import annotations

import os
import sys
import glob
import json
from datetime import datetime, timedelta

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from config.app_config import DATA_DIR

DECISIONS_DIR = os.path.join(DATA_DIR, "decisions")

# Причины закрытия, которые считаются "закрытием позиции" (а не открытием)
CLOSE_MARKERS = ("POSITION_CLOSED", "SHORT_CLOSED")

# Бакеты уверенности для анализа калибровки модели
CONFIDENCE_BUCKETS = [
    (0.0, 0.60, "<60%"),
    (0.60, 0.70, "60-70%"),
    (0.70, 0.80, "70-80%"),
    (0.80, 0.90, "80-90%"),
    (0.90, 1.01, "90%+"),
]


# ─────────────────────────────────────────────────────────────────────────────
# Загрузка
# ─────────────────────────────────────────────────────────────────────────────

def _daily_files(date_from: datetime, date_to: datetime) -> list[str]:
    """Список путей decisions_*.csv, чьи даты попадают в [date_from, date_to]."""
    if not os.path.isdir(DECISIONS_DIR):
        return []
    paths = []
    for fpath in sorted(glob.glob(os.path.join(DECISIONS_DIR, "decisions_*.csv"))):
        name = os.path.basename(fpath)
        try:
            d = datetime.strptime(name, "decisions_%Y-%m-%d.csv")
        except ValueError:
            continue
        if date_from.date() <= d.date() <= date_to.date():
            paths.append(fpath)
    return paths


def load_decisions(days: int | None = None,
                    date_from: datetime | None = None,
                    date_to: datetime | None = None,
                    mode: str | None = None) -> pd.DataFrame:
    """
    Читает decisions_*.csv за период и возвращает единый DataFrame.

    Период задаётся ЛИБО через days (последние N дней от сейчас),
    ЛИБО через date_from/date_to (включительно). Если ничего не задано —
    берутся последние 3 дня.

    mode: 'DEMO' | 'REAL' | None (None = оба режима вместе).
    """
    now = datetime.now()
    if date_from is None and date_to is None:
        days = days or 3
        date_to = now
        # FIX: обрезаем до полуночи — иначе days=1 даёт date_from = now - 0 дней
        # = now, то есть окно [сейчас, сейчас] нулевой ширины. Реальный симптом:
        # /history?days=1 показывал "История сделок пуста", хотя сделки за
        # сегодня были — просто все они оказались "раньше начала окна".
        date_from = (now - timedelta(days=days - 1)).replace(
            hour=0, minute=0, second=0, microsecond=0)
    elif date_to is None:
        date_to = now
    elif date_from is None:
        date_from = (date_to - timedelta(days=(days or 3) - 1)).replace(
            hour=0, minute=0, second=0, microsecond=0)

    paths = _daily_files(date_from, date_to)
    if not paths:
        return pd.DataFrame()

    frames = []
    for p in paths:
        try:
            df = pd.read_csv(p, delimiter=";", encoding="utf-8", dtype=str)
            frames.append(df)
        except Exception as e:
            print(f"⚠️ Не удалось прочитать {p}: {e}")

    if not frames:
        return pd.DataFrame()

    df = pd.concat(frames, ignore_index=True, sort=False)

    # ── Типизация ────────────────────────────────────────────────────────────
    df["timestamp"] = pd.to_datetime(df.get("timestamp"), errors="coerce")
    for col in ("confidence", "price", "entry_price", "tp_pct", "sl_pct",
                "usdt_amount", "qty", "pnl_pct", "pnl_usdt", "total_capital",
                "rsi_14", "atr_pct", "bb_position", "bb_width",
                "macd_hist_norm", "dev_spread", "dev_momentum", "deviation",
                "f1_model"):
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")

    # Точный фильтр по времени внутри дня (файлы дневные, но границы периода
    # могут быть внутри суток — обрежем точнее, чем по дате файла)
    df = df[(df["timestamp"] >= date_from) & (df["timestamp"] <= date_to)]

    if mode:
        df = df[df.get("mode", "").fillna("DEMO") == mode.upper()]

    return df.reset_index(drop=True)


# ─────────────────────────────────────────────────────────────────────────────
# Производные колонки
# ─────────────────────────────────────────────────────────────────────────────

def _is_close(event_type: str) -> bool:
    event_type = event_type or ""
    return any(m in event_type for m in CLOSE_MARKERS)


_EMPTY_CLOSED_COLUMNS = [
    "timestamp", "symbol", "event_type", "signal", "confidence", "price",
    "entry_price", "pnl_pct", "pnl_usdt", "mode", "close_reason",
    "source", "direction", "close_reason_clean", "win", "confidence_bucket",
]


def _empty_closed_frame() -> pd.DataFrame:
    """Пустой DataFrame, но со всеми колонками, которые ждут downstream-функции —
    иначе groupby('direction') и т.п. падают с KeyError на пустых данных."""
    return pd.DataFrame({c: pd.Series(dtype="object") for c in _EMPTY_CLOSED_COLUMNS})


def prepare_closed(df: pd.DataFrame) -> pd.DataFrame:
    """
    Из полного df решений выделяет только ЗАКРЫТИЯ и добавляет
    удобные для анализа колонки: direction, close_reason_clean, win.
    """
    if df.empty or "event_type" not in df.columns:
        return _empty_closed_frame()

    closed = df[df["event_type"].fillna("").apply(_is_close)].copy()
    if closed.empty:
        return _empty_closed_frame()

    raw_reason = closed["close_reason"].fillna(
        closed["event_type"].fillna("").str.replace("POSITION_CLOSED_", "", regex=False)
    )
    is_short = raw_reason.str.contains("SHORT", na=False) | closed["event_type"].fillna("").str.contains("SHORT", na=False)
    closed["direction"] = is_short.map({True: "SHORT", False: "LONG"})
    closed["close_reason_clean"] = raw_reason.str.replace("SHORT_", "", regex=False).replace("", "UNKNOWN").fillna("UNKNOWN")
    closed["win"] = closed["pnl_pct"] > 0

    def _bucket(c):
        if pd.isna(c):
            return None
        for lo, hi, label in CONFIDENCE_BUCKETS:
            if lo <= c < hi:
                return label
        return None

    closed["confidence_bucket"] = closed["confidence"].apply(_bucket)
    return closed


# ─────────────────────────────────────────────────────────────────────────────
# Метрики
# ─────────────────────────────────────────────────────────────────────────────

def summary_stats(closed: pd.DataFrame) -> dict:
    """Общая сводка: сколько сделок, win rate, суммарный PnL."""
    if closed.empty:
        return {"total": 0, "wins": 0, "win_rate": 0.0,
                "total_pnl_usdt": 0.0, "avg_pnl_pct": 0.0}
    total = len(closed)
    wins = int(closed["win"].sum())
    return {
        "total": total,
        "wins": wins,
        "losses": total - wins,
        "win_rate": round(wins / total * 100, 1) if total else 0.0,
        "total_pnl_usdt": round(closed["pnl_usdt"].sum(skipna=True), 2),
        "avg_pnl_pct": round(closed["pnl_pct"].mean(skipna=True), 2) if total else 0.0,
    }


def breakdown_by_direction(closed: pd.DataFrame) -> dict:
    """LONG vs SHORT: сколько сделок, win rate, PnL по каждому направлению."""
    out = {}
    for direction, grp in closed.groupby("direction"):
        out[direction] = summary_stats(grp)
    return out


def breakdown_by_reason(closed: pd.DataFrame) -> list[dict]:
    """По причине закрытия (TP/SL/TRAIL/BUY_SIGNAL/MANUAL/...), сортировка по кол-ву."""
    rows = []
    for reason, grp in closed.groupby("close_reason_clean"):
        stats = summary_stats(grp)
        stats["reason"] = reason
        rows.append(stats)
    return sorted(rows, key=lambda r: r["total"], reverse=True)


def breakdown_by_pair(closed: pd.DataFrame) -> list[dict]:
    """По паре — какие монеты в плюсе, какие тянут вниз. Сортировка по PnL."""
    rows = []
    for symbol, grp in closed.groupby("symbol"):
        stats = summary_stats(grp)
        stats["symbol"] = symbol
        rows.append(stats)
    return sorted(rows, key=lambda r: r["total_pnl_usdt"], reverse=True)


def breakdown_by_confidence(closed: pd.DataFrame) -> list[dict]:
    """
    Ключевая диагностика калибровки модели: растёт ли win rate вместе
    с заявленной уверенностью, или confidence не несёт сигнала.
    Только строки, где confidence реально известен (не 0/NaN — типично
    для ручных сделок с дашборда, у них confidence не от модели).
    """
    with_conf = closed[closed["confidence_bucket"].notna() & (closed["confidence"] > 0)]
    rows = []
    for _, _, label in CONFIDENCE_BUCKETS:
        grp = with_conf[with_conf["confidence_bucket"] == label]
        stats = summary_stats(grp)
        stats["bucket"] = label
        rows.append(stats)
    return rows


def breakdown_by_source(closed: pd.DataFrame) -> list[dict]:
    """Какая модель (ml_rf / ml_xgb) реально прибыльнее в бою, по парам."""
    if "source" not in closed.columns:
        return []
    with_source = closed[closed["source"].notna() & (closed["source"] != "")]
    rows = []
    for (symbol, source), grp in with_source.groupby(["symbol", "source"]):
        stats = summary_stats(grp)
        stats["symbol"] = symbol
        stats["source"] = source
        rows.append(stats)
    return sorted(rows, key=lambda r: (r["symbol"], r["source"]))


def find_duplicate_clusters(closed: pd.DataFrame, window_seconds: int = 300) -> list[dict]:
    """
    Ищет подозрительные кластеры: несколько закрытий одной пары с одинаковым
    entry_price и close_reason в пределах window_seconds друг от друга.
    Это сигнатура известного класса багов (неполное закрытие/погашение,
    рестарт-гонка) — не обязательно баг, но всегда стоит внимания.
    """
    if closed.empty:
        return []
    clusters = []
    sorted_df = closed.sort_values("timestamp")
    for (symbol, entry_price, reason), grp in sorted_df.groupby(
            ["symbol", "entry_price", "close_reason_clean"]):
        if len(grp) < 2:
            continue
        times = grp["timestamp"].tolist()
        # Кластеризуем последовательные события, где разрыв <= window_seconds
        cluster = [times[0]]
        for t in times[1:]:
            if (t - cluster[-1]).total_seconds() <= window_seconds:
                cluster.append(t)
            else:
                if len(cluster) >= 2:
                    clusters.append(_cluster_info(symbol, entry_price, reason, cluster, grp))
                cluster = [t]
        if len(cluster) >= 2:
            clusters.append(_cluster_info(symbol, entry_price, reason, cluster, grp))
    return clusters


def _cluster_info(symbol, entry_price, reason, cluster_times, grp) -> dict:
    sub = grp[grp["timestamp"].isin(cluster_times)]
    return {
        "symbol": symbol,
        "entry_price": entry_price,
        "close_reason": reason,
        "count": len(cluster_times),
        "first": cluster_times[0].strftime("%Y-%m-%d %H:%M:%S"),
        "last": cluster_times[-1].strftime("%Y-%m-%d %H:%M:%S"),
        "total_pnl_usdt": round(sub["pnl_usdt"].sum(skipna=True), 2),
    }


# ─────────────────────────────────────────────────────────────────────────────
# Сборка полного отчёта
# ─────────────────────────────────────────────────────────────────────────────

def build_report(days: int | None = None,
                  date_from: datetime | None = None,
                  date_to: datetime | None = None,
                  mode: str | None = None) -> dict:
    """
    Собирает весь набор метрик за период в один словарь —
    им пользуются и report.py (рендер в HTML), и dashboard/app.py (Jinja).
    """
    raw = load_decisions(days=days, date_from=date_from, date_to=date_to, mode=mode)
    closed = prepare_closed(raw)

    period_from = date_from or (datetime.now() - timedelta(days=(days or 3) - 1))
    period_to = date_to or datetime.now()

    return {
        "period_from": period_from.strftime("%Y-%m-%d"),
        "period_to": period_to.strftime("%Y-%m-%d"),
        "mode": mode or "ALL",
        "summary": summary_stats(closed),
        "by_direction": breakdown_by_direction(closed),
        "by_reason": breakdown_by_reason(closed),
        "by_pair": breakdown_by_pair(closed),
        "by_confidence": breakdown_by_confidence(closed),
        "by_source": breakdown_by_source(closed),
        "duplicate_clusters": find_duplicate_clusters(closed),
        "raw_trade_count": len(raw),
        "closed_trade_count": len(closed),
    }
