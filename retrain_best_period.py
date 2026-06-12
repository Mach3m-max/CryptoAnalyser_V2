#!/usr/bin/env python3
"""
retrain_best_period.py — переобучение на оптимальном периоде данных
====================================================================
Проблема: при добавлении старых данных модель деградирует — рыночный
режим меняется, и данные 2-годичной давности только шумят.
Решение: найти период (N дней) с максимальной плотностью сигналов
и переобучить модель только на нём.

МЕТРИКА ВЫБОРА ПЕРИОДА:
  Оптимизируем signal_score = (BUY_n + SELL_n) / total × balance_factor
  где balance_factor = min(BUY%, SELL%) / max(BUY%, SELL%)  (близость к 1 = баланс)
  Это лучше чем просто BUY% — учитываем и плотность, и сбалансированность.

Запуск:
  python retrain_best_period.py                         # авто, все пары
  python retrain_best_period.py --scan                  # только анализ, без обучения
  python retrain_best_period.py --pairs TIASDT WUSDT    # конкретные пары
  python retrain_best_period.py --days 273              # фиксированный период
  python retrain_best_period.py --days 273 --pairs TIASDT
  python retrain_best_period.py --scan --pairs BTCUSDT ETHUSDT SOLUSDT
"""
import sys, subprocess, shutil, argparse, json
from pathlib import Path
from datetime import datetime
import pandas as pd

BASE_DIR = Path(__file__).parent
DATA_DIR = BASE_DIR / "historical_data"
MODEL_DIR = BASE_DIR / "ml" / "models"

# ── Периоды для перебора (дней) ───────────────────────────────────────────────
TEST_PERIODS = [60, 90, 120, 180, 273, 365, 432]

# ── Все пары портфеля ─────────────────────────────────────────────────────────
ALL_PAIRS = [
    "BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT",   # core
    "AVAXUSDT", "APTUSDT", "WUSDT",  "OPUSDT",    # growth
    "TIASDT",  "ATOMUSDT", "WIFUSDT", "ARBUSDT",   # growth/hedge
    "XAUTUSDT", "SUIUSDT", "INJUSDT", "STXUSDT",  # hedge + new
]

PAIRS_DEFAULT = ALL_PAIRS   # по умолчанию — все пары

# Минимум меток для корректного обучения
MIN_BUY  = 500
MIN_SELL = 500
MIN_ROWS = 1000


# ── Статистика разметки ───────────────────────────────────────────────────────

def get_label_stats(df: pd.DataFrame) -> dict:
    vc    = df["label"].value_counts()
    total = len(df)
    buy_n  = vc.get("BUY",  0)
    sell_n = vc.get("SELL", 0)
    buy_p  = buy_n  / total * 100 if total else 0
    sell_p = sell_n / total * 100 if total else 0

    # balance_factor: 1.0 = идеальный баланс, → 0 при сильном дисбалансе
    if buy_p > 0 and sell_p > 0:
        balance = min(buy_p, sell_p) / max(buy_p, sell_p)
    else:
        balance = 0.0

    # signal_score — главная метрика: плотность × баланс
    signal_density = (buy_n + sell_n) / total if total else 0
    signal_score   = signal_density * balance

    return {
        "buy_pct":       buy_p,
        "sell_pct":      sell_p,
        "buy_n":         buy_n,
        "sell_n":        sell_n,
        "hold_n":        vc.get("HOLD", 0),
        "balance":       balance,
        "signal_score":  signal_score,
        "signal_density": signal_density * 100,
        "days":   (df["timestamp"].max() - df["timestamp"].min()).days if len(df) else 0,
        "rows":   total,
    }


# ── Поиск лучшего периода ─────────────────────────────────────────────────────

def find_best_period(symbol: str, verbose: bool = True) -> tuple[int, dict]:
    """
    Перебирает периоды из TEST_PERIODS и выбирает тот с максимальным signal_score.
    Возвращает (best_days, results_dict).
    """
    src = DATA_DIR / f"{symbol}_indicators_labeled.csv"
    if not src.exists():
        if verbose:
            print(f"  ⚠️  {symbol}_indicators_labeled.csv не найден → используем 273д")
        return 273, {}

    df = pd.read_csv(src, parse_dates=["timestamp"])
    df["timestamp"] = pd.to_datetime(df["timestamp"], errors="coerce")
    df = df.sort_values("timestamp").reset_index(drop=True)
    t_max = df["timestamp"].max()

    best_days  = 273
    best_score = -1.0
    period_results = {}

    if verbose:
        print(f"\n  🔍 {symbol} — анализ периодов:")
        print(f"  {'Дней':>6}  {'Строк':>8}  {'BUY%':>6}  {'SELL%':>6}  "
              f"{'Баланс':>7}  {'Сигнал%':>8}  {'Score':>7}")
        print("  " + "─" * 60)

    for days in TEST_PERIODS:
        cutoff = t_max - pd.Timedelta(days=days)
        part   = df[df["timestamp"] >= cutoff]
        if len(part) < MIN_ROWS:
            continue
        s = get_label_stats(part)
        period_results[days] = s

        marker = ""
        if s["buy_n"] >= MIN_BUY and s["sell_n"] >= MIN_SELL:
            if s["signal_score"] > best_score:
                best_score = s["signal_score"]
                best_days  = days
                marker = " ←"

        if verbose:
            flag = "✅" if (s["buy_n"] >= MIN_BUY and s["sell_n"] >= MIN_SELL) else "⚠️ "
            print(f"  {days:>6}  {s['rows']:>8,}  {s['buy_pct']:>5.1f}%  {s['sell_pct']:>5.1f}%  "
                  f"  {s['balance']:>5.2f}  {s['signal_density']:>7.1f}%  "
                  f"{s['signal_score']*100:>6.2f}{marker}")

    if verbose:
        meta_path = MODEL_DIR / f"{symbol}_meta.json"
        current_f1 = "нет модели"
        if meta_path.exists():
            try:
                meta = json.loads(meta_path.read_text())
                current_f1 = f"F1={meta.get('f1_macro', '?'):.3f} ({meta.get('model_type','?')})"
            except Exception:
                pass
        print(f"\n  ➤ Лучший период: {best_days}д  score={best_score*100:.2f}  {current_f1}")

    return best_days, period_results


# ── Переобучение ──────────────────────────────────────────────────────────────

def crop_and_retrain(symbol: str, days: int, dry_run: bool = False) -> bool:
    src = DATA_DIR / f"{symbol}_indicators_labeled.csv"
    if not src.exists():
        print(f"  ❌ {src.name} не найден")
        return False

    df = pd.read_csv(src, parse_dates=["timestamp"])
    df["timestamp"] = pd.to_datetime(df["timestamp"], errors="coerce")
    df = df.sort_values("timestamp").reset_index(drop=True)

    full_stats = get_label_stats(df)
    print(f"  Полный файл : {full_stats['rows']:,} строк  {full_stats['days']}д  "
          f"BUY={full_stats['buy_pct']:.1f}%  SELL={full_stats['sell_pct']:.1f}%")

    t_max    = df["timestamp"].max()
    t_cutoff = t_max - pd.Timedelta(days=days)
    df_cut   = df[df["timestamp"] >= t_cutoff].reset_index(drop=True)
    cut_stats = get_label_stats(df_cut)

    print(f"  Обрезано до : {cut_stats['rows']:,} строк  {cut_stats['days']}д  "
          f"BUY={cut_stats['buy_pct']:.1f}%  SELL={cut_stats['sell_pct']:.1f}%  "
          f"баланс={cut_stats['balance']:.2f}")

    if cut_stats["buy_n"] < MIN_BUY or cut_stats["sell_n"] < MIN_SELL:
        print(f"  ⚠️  Мало меток (BUY={cut_stats['buy_n']} SELL={cut_stats['sell_n']}) — пропускаем")
        return False

    if dry_run:
        print(f"  [DRY RUN] обучение пропущено")
        return True

    bak = src.with_suffix(".csv.bak")
    shutil.copy2(src, bak)
    print(f"  💾 Бэкап: {bak.name}")

    df_cut.to_csv(src, index=False)
    print(f"  ✅ Сохранён обрезанный файл ({days}д)")

    print(f"  🚀 Обучаем {symbol}...")
    result = subprocess.run(
        [sys.executable, "ml/train_v4.py", "--pairs", symbol],
        capture_output=False
    )

    shutil.copy2(bak, src)
    bak.unlink()
    print(f"  ♻️  Восстановлен полный файл")

    return result.returncode == 0


# ── main ──────────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser(description="Переобучение на оптимальном периоде")
    ap.add_argument("--pairs",   nargs="+", default=None,
                    help="Пары (по умолчанию все 15)")
    ap.add_argument("--all",     action="store_true",
                    help="Все пары из портфеля")
    ap.add_argument("--days",    type=int,  default=None,
                    help="Фиксированный период в днях (иначе авто)")
    ap.add_argument("--scan",    action="store_true",
                    help="Только анализ периодов, без переобучения (= --dry-run)")
    ap.add_argument("--dry-run", action="store_true",
                    help="Показать анализ, не переобучать")
    args = ap.parse_args()

    dry = args.scan or args.dry_run

    if args.all or args.pairs is None:
        pairs = ALL_PAIRS
    else:
        pairs = args.pairs

    print("=" * 65)
    print("  🔧 АНАЛИЗ И ПЕРЕОБУЧЕНИЕ НА ОПТИМАЛЬНОМ ПЕРИОДЕ")
    print("=" * 65)
    print(f"  Пары    : {len(pairs)} ({', '.join(pairs[:5])}{'...' if len(pairs) > 5 else ''})")
    print(f"  Период  : {'авто (signal_score)' if args.days is None else str(args.days) + 'д'}")
    print(f"  Режим   : {'📊 АНАЛИЗ' if dry else '🔧 ОБУЧЕНИЕ'}")
    print()
    print("  Метрика: signal_score = (BUY+SELL)/total × min(BUY%,SELL%)/max(BUY%,SELL%)")
    print("  Чем выше — тем больше чистых сигналов и лучше баланс BUY/SELL")

    summary = {}

    for symbol in pairs:
        print(f"\n{'═'*65}")
        print(f"  {symbol}")
        print('═' * 65)

        if args.days is not None:
            days = args.days
            _, _ = find_best_period(symbol, verbose=True)  # показываем для информации
        else:
            days, _ = find_best_period(symbol, verbose=True)

        ok = crop_and_retrain(symbol, days, dry_run=dry)
        summary[symbol] = {"days": days, "ok": ok}

    # Итоговая таблица
    print(f"\n{'='*65}")
    print(f"  {'ПАРА':12s}  {'ПЕРИОД':>7}  {'СТАТУС'}")
    print(f"  {'─'*12}  {'─'*7}  {'─'*20}")
    for sym, r in summary.items():
        icon = "✅" if r["ok"] else ("📊" if dry else "❌")
        label = "проанализировано" if dry else ("OK" if r["ok"] else "ОШИБКА")
        print(f"  {sym:12s}  {r['days']:>5}д  {icon} {label}")
    print(f"{'='*65}")

    if not dry:
        print()
        print("  Следующий шаг:")
        print("  python simulator.py")
        print("  python backtester.py")


if __name__ == "__main__":
    main()



def get_label_stats(df: pd.DataFrame) -> dict:
    vc = df["label"].value_counts()
    total = len(df)
    return {
        "buy_pct":  vc.get("BUY",  0) / total * 100,
        "sell_pct": vc.get("SELL", 0) / total * 100,
        "buy_n":    vc.get("BUY",  0),
        "sell_n":   vc.get("SELL", 0),
        "days":     (df["timestamp"].max() - df["timestamp"].min()).days,
        "rows":     total,
    }


def crop_and_retrain(symbol: str, days: int, dry_run: bool = False) -> bool:
    src = DATA_DIR / f"{symbol}_indicators_labeled.csv"
    if not src.exists():
        print(f"  ❌ {src.name} не найден")
        return False

    print(f"\n  📂 Загружаем {src.name}...")
    df = pd.read_csv(src, parse_dates=["timestamp"])
    df["timestamp"] = pd.to_datetime(df["timestamp"], errors="coerce")
    df = df.sort_values("timestamp").reset_index(drop=True)

    full_stats = get_label_stats(df)
    print(f"  Полный файл: {full_stats['rows']:,} строк  {full_stats['days']}д  "
          f"BUY={full_stats['buy_pct']:.1f}%  SELL={full_stats['sell_pct']:.1f}%")

    # Обрезаем до нужного периода
    t_max    = df["timestamp"].max()
    t_cutoff = t_max - pd.Timedelta(days=days)
    df_cut   = df[df["timestamp"] >= t_cutoff].reset_index(drop=True)
    cut_stats = get_label_stats(df_cut)

    print(f"  Обрезано до: {cut_stats['rows']:,} строк  {cut_stats['days']}д  "
          f"BUY={cut_stats['buy_pct']:.1f}%  SELL={cut_stats['sell_pct']:.1f}%")

    if cut_stats["buy_n"] < 500 or cut_stats["sell_n"] < 500:
        print(f"  ⚠️  Мало меток (BUY={cut_stats['buy_n']} SELL={cut_stats['sell_n']}) — пропускаем")
        return False

    if dry_run:
        print(f"  [DRY RUN] Переобучение не запускается")
        return True

    # Бэкапим оригинал
    bak = src.with_suffix(".csv.bak")
    shutil.copy2(src, bak)
    print(f"  💾 Бэкап: {bak.name}")

    # Сохраняем обрезанный файл
    df_cut.to_csv(src, index=False)
    print(f"  ✅ Сохранён обрезанный файл ({days}д)")

    # Запускаем train_v4.py
    print(f"  🚀 Обучаем {symbol}...")
    result = subprocess.run(
        [sys.executable, "ml/train_v4.py", "--pairs", symbol],
        capture_output=False
    )

    # Восстанавливаем полный файл
    shutil.copy2(bak, src)
    bak.unlink()
    print(f"  ♻️  Восстановлен полный файл")

    return result.returncode == 0


def find_best_period(symbol: str) -> int:
    """Ищет период при котором BUY% максимален (больше сигналов = лучше для TIASDT)."""
    src = DATA_DIR / f"{symbol}_indicators_labeled.csv"
    if not src.exists():
        return 273

    df = pd.read_csv(src, parse_dates=["timestamp"])
    df["timestamp"] = pd.to_datetime(df["timestamp"], errors="coerce")
    df = df.sort_values("timestamp").reset_index(drop=True)
    t_max = df["timestamp"].max()

    best_days = 273
    best_buy_pct = 0.0

    print(f"\n  🔍 Анализ периодов для {symbol}:")
    print(f"  {'Дней':>6}  {'Строк':>8}  {'BUY%':>6}  {'SELL%':>6}  {'BUY_n':>7}")
    print("  " + "-" * 45)

    for days in TEST_PERIODS:
        cutoff = t_max - pd.Timedelta(days=days)
        part   = df[df["timestamp"] >= cutoff]
        if len(part) < 1000:
            continue
        vc  = part["label"].value_counts()
        tot = len(part)
        bp  = vc.get("BUY", 0) / tot * 100
        sp  = vc.get("SELL", 0) / tot * 100
        bn  = vc.get("BUY", 0)
        print(f"  {days:>6}  {tot:>8,}  {bp:>5.1f}%  {sp:>5.1f}%  {bn:>7,}")
        if bp > best_buy_pct and bn >= 500:
            best_buy_pct = bp
            best_days    = days

    print(f"\n  ➤ Лучший период: {best_days}д (BUY%={best_buy_pct:.1f}%)")
    return best_days


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pairs",  nargs="+", default=PAIRS_DEFAULT)
    ap.add_argument("--days",   type=int,  default=None,
                    help="Фиксированный период (если не указан — авто)")
    ap.add_argument("--dry-run", action="store_true",
                    help="Только показать статистику, не переобучать")
    args = ap.parse_args()

    print("=" * 60)
    print("  🔧 ПЕРЕОБУЧЕНИЕ НА ОПТИМАЛЬНОМ ПЕРИОДЕ")
    print("=" * 60)
    print(f"  Пары    : {', '.join(args.pairs)}")
    print(f"  Период  : {'авто' if args.days is None else str(args.days) + 'д'}")
    print(f"  Dry run : {args.dry_run}")

    results = {}
    for symbol in args.pairs:
        print(f"\n{'═'*60}")
        print(f"  {symbol}")
        print('═'*60)

        if args.days is not None:
            days = args.days
        else:
            days = find_best_period(symbol)

        ok = crop_and_retrain(symbol, days, dry_run=args.dry_run)
        results[symbol] = {"days": days, "ok": ok}

    print(f"\n{'='*60}")
    print("  ИТОГО:")
    for sym, r in results.items():
        status = "✅" if r["ok"] else "❌"
        print(f"  {status} {sym:12s} → {r['days']}д")
    print("=" * 60)
    print("\n  Следующий шаг:")
    print("  python simulator.py --days 90")


if __name__ == "__main__":
    main()
