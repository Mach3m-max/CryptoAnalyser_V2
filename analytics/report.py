#!/usr/bin/env python3
# analytics/report.py
"""
analytics/report.py — CLI-скрипт глубокого разбора торговли.

Использует тот же движок (analytics/core.py), что и страница /analytics
в дашборде — так что цифры здесь и там всегда совпадают. Дополнительно
показывает список последних сделок и подсвечивает подозрительные
кластеры повторных закрытий (см. find_duplicate_clusters).

Запуск:
    python analytics/report.py                        # последние 3 дня
    python analytics/report.py --days 7
    python analytics/report.py --from 2026-09-15 --to 2026-09-21
    python analytics/report.py --mode REAL --days 7
    python analytics/report.py --days 7 --no-html      # только консоль
    python analytics/report.py --days 7 --limit 50     # 50 последних сделок в HTML

Выход:
    analytics_report_ДАТА.html   — визуальный отчёт (если не --no-html)
"""
from __future__ import annotations

import os
import sys
import argparse
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from analytics.core import (
    load_decisions, prepare_closed, build_report,
    CONFIDENCE_BUCKETS,
)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))


# ─────────────────────────────────────────────────────────────────────────────
# Консольный вывод
# ─────────────────────────────────────────────────────────────────────────────

def print_report(report: dict, closed_df) -> None:
    s = report["summary"]
    print()
    print("=" * 70)
    print(f"  📊 ОТЧЁТ ПО ТОРГОВЛЕ  [{report['mode']}]  "
          f"{report['period_from']} → {report['period_to']}")
    print("=" * 70)
    print(f"  Всего решений в логе : {report['raw_trade_count']:,}")
    print(f"  Закрытых сделок      : {report['closed_trade_count']:,}")
    print()
    print(f"  Win rate  : {s['win_rate']}%  ({s.get('wins', 0)}/{s['total']})")
    print(f"  PnL суммарно : {s['total_pnl_usdt']:+.2f} USDT")
    print(f"  PnL средний  : {s['avg_pnl_pct']:+.2f}% за сделку")

    if report["by_direction"]:
        print()
        print("  ── По направлению ──────────────────────────────")
        for direction, stats in report["by_direction"].items():
            print(f"    {direction:6s}  сделок={stats['total']:>4}  "
                  f"WR={stats['win_rate']:>5.1f}%  "
                  f"PnL={stats['total_pnl_usdt']:>+8.2f} USDT")

    if report["by_reason"]:
        print()
        print("  ── По причине закрытия ─────────────────────────")
        for stats in report["by_reason"]:
            print(f"    {stats['reason']:14s}  сделок={stats['total']:>4}  "
                  f"WR={stats['win_rate']:>5.1f}%  "
                  f"PnL={stats['total_pnl_usdt']:>+8.2f} USDT")

    if report["by_pair"]:
        print()
        print("  ── По паре (топ-10 по PnL) ─────────────────────")
        for stats in report["by_pair"][:10]:
            print(f"    {stats['symbol']:12s}  сделок={stats['total']:>4}  "
                  f"WR={stats['win_rate']:>5.1f}%  "
                  f"PnL={stats['total_pnl_usdt']:>+8.2f} USDT")
        if len(report["by_pair"]) > 10:
            print(f"    ... и ещё {len(report['by_pair']) - 10} пар")

    conf_rows = [r for r in report["by_confidence"] if r["total"] > 0]
    if conf_rows:
        print()
        print("  ── Калибровка модели (confidence → win rate) ───")
        for stats in conf_rows:
            print(f"    {stats['bucket']:8s}  сделок={stats['total']:>4}  "
                  f"WR={stats['win_rate']:>5.1f}%")

    if report["by_source"]:
        print()
        print("  ── По источнику модели ─────────────────────────")
        for stats in report["by_source"]:
            print(f"    {stats['symbol']:12s} / {stats['source']:10s}  "
                  f"сделок={stats['total']:>4}  WR={stats['win_rate']:>5.1f}%  "
                  f"PnL={stats['total_pnl_usdt']:>+8.2f} USDT")

    if report["duplicate_clusters"]:
        print()
        print("  ⚠️  ПОДОЗРИТЕЛЬНЫЕ КЛАСТЕРЫ (повторные закрытия) ──")
        for c in report["duplicate_clusters"]:
            print(f"    {c['symbol']:10s}  entry={c['entry_price']}  "
                  f"{c['close_reason']:10s}  {c['count']}× за "
                  f"[{c['first']} → {c['last']}]  "
                  f"PnL={c['total_pnl_usdt']:+.2f} USDT")

    print()
    print("=" * 70)


# ─────────────────────────────────────────────────────────────────────────────
# HTML-отчёт
# ─────────────────────────────────────────────────────────────────────────────

HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="ru"><head>
<meta charset="utf-8">
<title>Отчёт по торговле — __PERIOD__</title>
<style>
  :root {
    --bg: #0d1117; --card: #161b22; --border: #30363d;
    --text: #c9d1d9; --muted: #8b949e;
    --green: #3fb950; --red: #f85149; --yellow: #d29922; --blue: #58a6ff;
  }
  * { box-sizing: border-box; }
  body {
    background: var(--bg); color: var(--text); margin: 0; padding: 24px;
    font-family: -apple-system, Segoe UI, Roboto, sans-serif;
  }
  h1 { font-size: 20px; margin: 0 0 4px; }
  .sub { color: var(--muted); font-size: 13px; margin-bottom: 24px; }
  .grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(160px, 1fr));
          gap: 12px; margin-bottom: 20px; }
  .card { background: var(--card); border: 1px solid var(--border);
          border-radius: 8px; padding: 14px 16px; }
  .lbl { font-size: 11px; color: var(--muted); text-transform: uppercase;
         margin-bottom: 6px; }
  .val { font-size: 22px; font-weight: 700; font-family: monospace; }
  .g { color: var(--green); } .r { color: var(--red); } .y { color: var(--yellow); }
  section { background: var(--card); border: 1px solid var(--border);
            border-radius: 8px; padding: 16px; margin-bottom: 16px; }
  section h2 { font-size: 14px; margin: 0 0 12px; color: var(--blue); }
  table { width: 100%; border-collapse: collapse; font-size: 13px; }
  th { text-align: left; color: var(--muted); font-weight: 600; font-size: 11px;
       text-transform: uppercase; padding: 6px 8px; border-bottom: 1px solid var(--border); }
  td { padding: 6px 8px; border-bottom: 1px solid rgba(48,54,61,.5); font-family: monospace; }
  .warn { border-color: rgba(210,153,34,.5); background: rgba(210,153,34,.05); }
  .empty { color: var(--muted); font-style: italic; padding: 12px 0; }
</style>
</head><body>

<h1>📊 Отчёт по торговле</h1>
<div class="sub">Режим __MODE__ · __PERIOD__ · сгенерировано __GENERATED__</div>

<div class="grid">
  <div class="card"><div class="lbl">Закрытых сделок</div>
    <div class="val">__TOTAL__</div></div>
  <div class="card"><div class="lbl">Win Rate</div>
    <div class="val __WR_CLS__">__WR__%</div></div>
  <div class="card"><div class="lbl">PnL суммарно</div>
    <div class="val __PNL_CLS__">__PNL__ USDT</div></div>
  <div class="card"><div class="lbl">PnL средний / сделку</div>
    <div class="val">__AVG_PNL__%</div></div>
</div>

<section>
<h2>По направлению</h2>
__DIRECTION_TABLE__
</section>

<section>
<h2>По причине закрытия</h2>
__REASON_TABLE__
</section>

<section>
<h2>По паре</h2>
__PAIR_TABLE__
</section>

<section>
<h2>Калибровка модели (confidence → win rate)</h2>
__CONFIDENCE_TABLE__
</section>

<section>
<h2>По источнику модели</h2>
__SOURCE_TABLE__
</section>

__DUPLICATE_SECTION__

<section>
<h2>Последние сделки (__RECENT_COUNT__)</h2>
__RECENT_TABLE__
</section>

</body></html>"""


def _table(headers: list[str], rows: list[list[str]]) -> str:
    if not rows:
        return '<div class="empty">Нет данных за этот период</div>'
    th = "".join(f"<th>{h}</th>" for h in headers)
    body = ""
    for row in rows:
        body += "<tr>" + "".join(f"<td>{c}</td>" for c in row) + "</tr>"
    return f"<table><thead><tr>{th}</tr></thead><tbody>{body}</tbody></table>"


def _fmt_pnl(v: float) -> str:
    return f"{v:+.2f}"


def build_html(report: dict, closed_df, recent_limit: int) -> str:
    s = report["summary"]

    direction_rows = [
        [d, str(st["total"]), f"{st['win_rate']}%", _fmt_pnl(st["total_pnl_usdt"])]
        for d, st in report["by_direction"].items()
    ]
    reason_rows = [
        [st["reason"], str(st["total"]), f"{st['win_rate']}%", _fmt_pnl(st["total_pnl_usdt"])]
        for st in report["by_reason"]
    ]
    pair_rows = [
        [st["symbol"], str(st["total"]), f"{st['win_rate']}%", _fmt_pnl(st["total_pnl_usdt"])]
        for st in report["by_pair"]
    ]
    conf_rows = [
        [st["bucket"], str(st["total"]), f"{st['win_rate']}%"]
        for st in report["by_confidence"] if st["total"] > 0
    ]
    source_rows = [
        [st["symbol"], st["source"], str(st["total"]), f"{st['win_rate']}%", _fmt_pnl(st["total_pnl_usdt"])]
        for st in report["by_source"]
    ]

    duplicate_section = ""
    if report["duplicate_clusters"]:
        dup_rows = [
            [c["symbol"], str(c["entry_price"]), c["close_reason"], f"{c['count']}×",
             c["first"], c["last"], _fmt_pnl(c["total_pnl_usdt"])]
            for c in report["duplicate_clusters"]
        ]
        duplicate_section = (
            '<section class="warn"><h2>⚠️ Подозрительные кластеры (повторные закрытия)</h2>'
            + _table(["Пара", "Entry", "Причина", "Раз", "Первое", "Последнее", "PnL USDT"], dup_rows)
            + "</section>"
        )

    recent_rows = []
    if closed_df is not None and not closed_df.empty:
        recent = closed_df.sort_values("timestamp", ascending=False).head(recent_limit)
        for _, r in recent.iterrows():
            pnl = r.get("pnl_usdt")
            recent_rows.append([
                str(r.get("timestamp"))[:19],
                str(r.get("symbol", "")),
                str(r.get("direction", "")),
                str(r.get("close_reason_clean", "")),
                f"{r.get('pnl_pct', 0):+.2f}%" if pd_notna(r.get("pnl_pct")) else "—",
                f"{pnl:+.2f}" if pd_notna(pnl) else "—",
            ])

    html = HTML_TEMPLATE
    replacements = {
        "__PERIOD__":     f"{report['period_from']} — {report['period_to']}",
        "__MODE__":       report["mode"],
        "__GENERATED__":  datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "__TOTAL__":      str(s["total"]),
        "__WR__":         str(s["win_rate"]),
        "__WR_CLS__":     "g" if s["win_rate"] >= 50 else "r",
        "__PNL__":        _fmt_pnl(s["total_pnl_usdt"]),
        "__PNL_CLS__":    "g" if s["total_pnl_usdt"] >= 0 else "r",
        "__AVG_PNL__":    _fmt_pnl(s["avg_pnl_pct"]),
        "__DIRECTION_TABLE__":  _table(["Направление", "Сделок", "WR", "PnL USDT"], direction_rows),
        "__REASON_TABLE__":     _table(["Причина", "Сделок", "WR", "PnL USDT"], reason_rows),
        "__PAIR_TABLE__":       _table(["Пара", "Сделок", "WR", "PnL USDT"], pair_rows),
        "__CONFIDENCE_TABLE__": _table(["Уверенность", "Сделок", "WR"], conf_rows),
        "__SOURCE_TABLE__":     _table(["Пара", "Источник", "Сделок", "WR", "PnL USDT"], source_rows),
        "__DUPLICATE_SECTION__": duplicate_section,
        "__RECENT_COUNT__":     str(len(recent_rows)),
        "__RECENT_TABLE__":     _table(["Время", "Пара", "Направление", "Причина", "PnL %", "PnL USDT"], recent_rows),
    }
    for k, v in replacements.items():
        html = html.replace(k, v)
    return html


def pd_notna(v) -> bool:
    """Небольшая обёртка, чтобы не тащить весь pandas в сигнатуру для одной проверки."""
    try:
        import pandas as pd
        return pd.notna(v)
    except Exception:
        return v is not None


# ─────────────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser(description="Глубокий разбор торговли HTT v2")
    ap.add_argument("--days", type=int, default=3,
                    help="Последние N дней (по умолчанию 3, игнорируется при --from/--to)")
    ap.add_argument("--from", dest="date_from", default=None, help="Начало периода YYYY-MM-DD")
    ap.add_argument("--to", dest="date_to", default=None, help="Конец периода YYYY-MM-DD")
    ap.add_argument("--mode", choices=["DEMO", "REAL"], default=None,
                    help="Фильтр по режиму (по умолчанию — оба вместе)")
    ap.add_argument("--limit", type=int, default=30, help="Сколько последних сделок в HTML")
    ap.add_argument("--out", default=None, help="Путь для HTML (по умолчанию — рядом со скриптом)")
    ap.add_argument("--no-html", action="store_true", help="Только консольный вывод")
    args = ap.parse_args()

    date_from = datetime.strptime(args.date_from, "%Y-%m-%d") if args.date_from else None
    date_to = (datetime.strptime(args.date_to, "%Y-%m-%d").replace(hour=23, minute=59, second=59)
              if args.date_to else None)

    report = build_report(days=args.days, date_from=date_from, date_to=date_to, mode=args.mode)

    # Для таблицы "последние сделки" нужен сам closed DataFrame — build_report()
    # его не возвращает (отдаёт только агрегаты), поэтому считаем его ещё раз
    # тем же путём, что и внутри build_report — дёшево, файлы уже прочитаны с диска раз.
    raw = load_decisions(days=args.days, date_from=date_from, date_to=date_to, mode=args.mode)
    closed = prepare_closed(raw)

    print_report(report, closed)

    if not args.no_html:
        html = build_html(report, closed, args.limit)
        if args.out:
            out_path = args.out
        else:
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            out_path = os.path.join(BASE_DIR, f"analytics_report_{ts}.html")
        with open(out_path, "w", encoding="utf-8") as f:
            f.write(html)
        print(f"  📄 HTML: {out_path}\n")


if __name__ == "__main__":
    main()
