from typing import Optional
"""
download_history.py — Автономная загрузка исторических данных
=============================================================
Запуск:  python download_history.py
         python download_history.py --days 365
         python download_history.py --days 180 --pairs BTCUSDT ETHUSDT

Результат: data/historical/{SYMBOL}_{DAYS}d.csv

Публичный API Bybit — авторизация НЕ нужна.
Скорость: ~45 сек на пару при 365 днях (минутные свечи).
"""

from __future__ import annotations
import argparse
import os
import sys
import time
from datetime import datetime
from pathlib import Path

import pandas as pd
import requests

# ── Конфигурация ──────────────────────────────────────────────────────────────

BASE_URL   = "https://api.bybit.com"          # публичный, без авторизации
OUTPUT_DIR = Path(__file__).parent / "data" / "historical"

DEFAULT_DAYS  = 365
BATCH_SIZE    = 1000    # макс свечей за 1 запрос (лимит Bybit)
INTERVAL      = "1"     # минутные свечи
RATE_DELAY    = 0.12    # сек между запросами (≈8 req/s — безопасно)
RETRY_LIMIT   = 3
RETRY_DELAY   = 5       # сек перед повтором при ошибке

PAIRS = [
    "BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT",
    "AVAXUSDT", "APTUSDT", "WUSDT",  "OPUSDT",
    "TIAUSDT",  "ATOMUSDT","WIFUSDT","ARBUSDT",
    "XAUTUSDT",
]

# ── Вспомогательные функции ───────────────────────────────────────────────────

def _fetch_batch(symbol: str, end_ms: int, limit: int = BATCH_SIZE) -> Optional[list]:
    """Один запрос к /v5/market/kline. Возвращает список свечей или None при ошибке."""
    params = {
        "category": "spot",
        "symbol":   symbol,
        "interval": INTERVAL,
        "limit":    limit,
        "end":      str(end_ms),
    }
    for attempt in range(1, RETRY_LIMIT + 1):
        try:
            r = requests.get(
                BASE_URL + "/v5/market/kline",
                params=params, timeout=15
            )
            data = r.json()
            if data.get("retCode") == 0:
                return data["result"]["list"]   # desc: новые → старые
            else:
                code = data.get("retCode")
                msg  = data.get("retMsg", "")
                print(f"    ⚠️  retCode={code} {msg}  (попытка {attempt}/{RETRY_LIMIT})")
        except requests.exceptions.RequestException as e:
            print(f"    ⚠️  Сеть: {e}  (попытка {attempt}/{RETRY_LIMIT})")

        if attempt < RETRY_LIMIT:
            time.sleep(RETRY_DELAY)
    return None


def _klines_to_df(klines: list) -> pd.DataFrame:
    """Конвертирует сырые свечи Bybit в DataFrame."""
    df = pd.DataFrame(
        klines,
        columns=["timestamp", "open", "high", "low", "close", "volume", "turnover"]
    )
    df["timestamp"] = pd.to_datetime(df["timestamp"].astype(float), unit="ms")
    for col in ("open", "high", "low", "close", "volume"):
        df[col] = pd.to_numeric(df[col], errors="coerce")
    return df.sort_values("timestamp").reset_index(drop=True)


def _progress(done: int, total: int, symbol: str, candles: int) -> str:
    pct  = done / total * 100
    bar  = "█" * int(pct / 5) + "░" * (20 - int(pct / 5))
    return f"  [{bar}] {pct:5.1f}%  {symbol:10s}  {candles:>7,} свечей"


# ── Основная загрузка одной пары ──────────────────────────────────────────────

def download_pair(symbol: str, days: int, out_dir: Path) -> bool:
    """
    Загружает `days` дней минутных свечей для `symbol`.
    Сохраняет в out_dir/{symbol}_{days}d.csv.
    Возвращает True при успехе.
    """
    out_path  = out_dir / f"{symbol}_{days}d.csv"
    now_ms    = int(datetime.now().timestamp() * 1000)
    cutoff_ms = now_ms - days * 24 * 60 * 60 * 1000

    # Если файл уже есть — дочитываем только недостающее
    existing_df = None
    if out_path.exists() and out_path.stat().st_size > 0:
        try:
            existing_df = pd.read_csv(out_path)
            existing_df["timestamp"] = pd.to_datetime(existing_df["timestamp"])
            oldest_existing = int(existing_df["timestamp"].min().timestamp() * 1000)
            if oldest_existing <= cutoff_ms + 60_000:
                # Уже есть всё что нужно — обновим только хвост
                last_ms   = int(existing_df["timestamp"].max().timestamp() * 1000)
                tail_rows = _fetch_tail(symbol, last_ms, now_ms)
                if tail_rows:
                    tail_df   = _klines_to_df(tail_rows)
                    merged    = pd.concat([existing_df, tail_df], ignore_index=True)
                    merged    = merged.drop_duplicates("timestamp").sort_values("timestamp")
                    merged.to_csv(out_path, index=False)
                    diff = len(merged) - len(existing_df)
                    print(f"  ✅ {symbol}: файл актуален, +{diff} новых свечей → {len(merged):,}")
                else:
                    print(f"  ✅ {symbol}: файл актуален, новых свечей нет")
                return True
            else:
                # Файл есть но данных не хватает — докачиваем назад
                print(f"  🔄 {symbol}: файл есть, докачиваем до {days}д...")
                end_ms = oldest_existing - 60_000
        except Exception as e:
            print(f"  ⚠️  Не удалось прочитать кэш {out_path.name}: {e} — загружаем заново")
            existing_df = None
            end_ms      = now_ms
    else:
        end_ms = now_ms

    # Максимальное число батчей с запасом
    max_batches = days * 24 * 60 // BATCH_SIZE + 10
    all_klines  = []
    batches     = 0

    print(f"  📡 {symbol}: загрузка {days}д...", end="", flush=True)

    for _ in range(max_batches):
        klines = _fetch_batch(symbol, end_ms)
        if klines is None:
            print(f"\n  ❌ {symbol}: ошибка запроса на батче {batches}")
            return False
        if not klines:
            break   # нет больше данных

        all_klines.extend(klines)
        oldest_ms = int(klines[-1][0])   # Bybit desc → последний = самый старый
        batches  += 1

        # Печатаем прогресс каждые 20 батчей
        if batches % 20 == 0:
            loaded_days = (now_ms - oldest_ms) / (24 * 60 * 60 * 1000)
            pct = min(loaded_days / days * 100, 100)
            bar = "█" * int(pct / 5) + "░" * (20 - int(pct / 5))
            print(f"\r  [{bar}] {pct:5.1f}%  {symbol}  {len(all_klines):>8,} свечей", end="", flush=True)

        if oldest_ms <= cutoff_ms:
            break   # достигли нужной глубины

        end_ms = oldest_ms - 1
        time.sleep(RATE_DELAY)

    if not all_klines:
        print(f"\n  ⚠️  {symbol}: нет данных")
        return False

    # Сборка DataFrame
    df = _klines_to_df(all_klines)

    # Обрезаем точно по cutoff
    cutoff_dt = pd.Timestamp(cutoff_ms, unit="ms")
    df = df[df["timestamp"] >= cutoff_dt].reset_index(drop=True)

    # Мержим с существующим если было
    if existing_df is not None:
        df = pd.concat([df, existing_df], ignore_index=True)
        df = df.drop_duplicates("timestamp").sort_values("timestamp").reset_index(drop=True)

    # Сохраняем
    df.to_csv(out_path, index=False)

    actual_days = (df["timestamp"].max() - df["timestamp"].min()).days
    size_mb     = out_path.stat().st_size / 1_048_576
    print(f"\r  ✅ {symbol}: {len(df):>8,} свечей  ({actual_days}д)  {size_mb:.1f} МБ  → {out_path.name}")
    return True


def _fetch_tail(symbol: str, since_ms: int, now_ms: int) -> list:
    """Загружает только новые свечи после since_ms."""
    result = []
    start  = since_ms + 60_000
    while start < now_ms:
        klines = _fetch_batch_forward(symbol, start, min(start + BATCH_SIZE * 60_000, now_ms))
        if not klines:
            break
        result.extend(klines)
        last = int(klines[0][0])   # desc → первый = новейший
        start = last + 60_000
        time.sleep(RATE_DELAY)
    return result


def _fetch_batch_forward(symbol: str, start_ms: int, end_ms: int) -> Optional[list]:
    params = {
        "category": "spot", "symbol": symbol,
        "interval": INTERVAL, "limit": BATCH_SIZE,
        "start": str(start_ms), "end": str(end_ms),
    }
    try:
        r = requests.get(BASE_URL + "/v5/market/kline", params=params, timeout=15)
        d = r.json()
        if d.get("retCode") == 0:
            return d["result"]["list"]
    except Exception:
        pass
    return None


# ── Точка входа ───────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Загрузка исторических свечей Bybit в папку Old"
    )
    parser.add_argument(
        "--days", type=int, default=DEFAULT_DAYS,
        help=f"Глубина загрузки в днях (по умолчанию: {DEFAULT_DAYS})"
    )
    parser.add_argument(
        "--pairs", nargs="+", default=None,
        metavar="PAIR",
        help="Конкретные пары (по умолчанию: все 13 из портфеля)"
    )
    parser.add_argument(
        "--url", default=None,
        help="Bybit base URL (по умолчанию: api.bybit.com)"
    )
    args = parser.parse_args()

    global BASE_URL
    if args.url:
        BASE_URL = args.url.rstrip("/")

    days    = args.days
    pairs   = args.pairs if args.pairs else PAIRS
    out_dir = OUTPUT_DIR

    # Создаём папку если нет
    out_dir.mkdir(parents=True, exist_ok=True)

    # Оценка
    minutes_total = days * 24 * 60
    batches_pair  = minutes_total // BATCH_SIZE + 1
    est_sec       = batches_pair * RATE_DELAY * len(pairs)
    est_min       = est_sec / 60
    size_est_mb   = minutes_total * len(pairs) * 95 / 1_048_576   # ~95 байт/строка

    print("=" * 65)
    print("  📦 Загрузчик исторических данных Bybit")
    print("=" * 65)
    print(f"  Глубина   : {days} дней  ({days * 1440:,} минутных свечей/пару)")
    print(f"  Пары      : {len(pairs)}  →  {', '.join(pairs)}")
    print(f"  Папка     : {out_dir}")
    print(f"  Размер    : ~{size_est_mb:.0f} МБ")
    print(f"  Время     : ~{est_min:.0f} мин  (≈{est_sec/len(pairs):.0f} сек/пару)")
    print("=" * 65)
    print()

    success = []
    failed  = []
    t_start = time.time()

    for i, symbol in enumerate(pairs, 1):
        print(f"[{i}/{len(pairs)}] {symbol}")
        t0 = time.time()
        ok = download_pair(symbol, days, out_dir)
        elapsed = time.time() - t0
        if ok:
            success.append(symbol)
        else:
            failed.append(symbol)
        # Небольшая пауза между парами
        if i < len(pairs):
            time.sleep(1.0)

    total_elapsed = time.time() - t_start

    print()
    print("=" * 65)
    print(f"  ✅ Успешно  : {len(success)}/{len(pairs)}")
    if failed:
        print(f"  ❌ Ошибки   : {', '.join(failed)}")

    # Считаем итоговый размер
    total_mb = sum(
        (out_dir / f"{s}_{days}d.csv").stat().st_size / 1_048_576
        for s in success
        if (out_dir / f"{s}_{days}d.csv").exists()
    )
    print(f"  💾 Итого    : {total_mb:.1f} МБ  в {out_dir}")
    print(f"  ⏱️  Время    : {total_elapsed/60:.1f} мин")
    print("=" * 65)

    if failed:
        print(f"\n  Повтори для проблемных пар:")
        print(f"  python download_history.py --days {days} --pairs {' '.join(failed)}")

    print(f"\n  Готово! Файлы в: {out_dir}")
    print(f"  Для обучения моделей на этих данных:")
    print(f"  → В train.py укажи DATA_DIR = BASE_DIR / 'historical_data' / 'Old'")


if __name__ == "__main__":
    main()
