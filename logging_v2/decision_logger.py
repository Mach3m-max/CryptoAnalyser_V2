# logging_v2/decision_logger.py
"""
Посуточное логирование торговых решений.
Файлы: data/decisions/decisions_YYYY-MM-DD.csv
Расширяет v1 fields: close_reason, pnl_pct, pnl_usdt.
"""
import os
import csv
import threading
from datetime import datetime

import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from config.app_config import DATA_DIR

_LOCK = threading.Lock()

DECISIONS_DIR = os.path.join(DATA_DIR, "decisions")
os.makedirs(DECISIONS_DIR, exist_ok=True)

FIELDNAMES = [
    "timestamp", "symbol", "event_type", "signal", "confidence",
    "price", "avg_deviation", "buy_votes", "sell_votes",
    "dev_50", "dev_75", "dev_100", "dev_150", "dev_200",
    "reason", "entry_price", "tp_price", "sl_price",
    "tp_pct", "sl_pct", "usdt_amount", "qty",
    "portfolio_open_count", "total_capital", "mode",
    # ── новые поля v2 ──
    "close_reason",   # TP / SL / TRAIL / MANUAL / UNKNOWN
    "pnl_pct",        # PnL в % от входа
    "pnl_usdt",       # PnL в USDT (с учётом комиссий)
]


def _today_path() -> str:
    date_str = datetime.now().strftime("%Y-%m-%d")
    return os.path.join(DECISIONS_DIR, f"decisions_{date_str}.csv")


def log_decision(**kwargs):
    """
    Записывает одно торговое решение в дневной CSV.
    Совместим с v1 strategy_logger.log_decision() — те же kwargs.
    """
    path = _today_path()
    row  = {f: kwargs.get(f, "") for f in FIELDNAMES}
    if not row.get("timestamp"):
        row["timestamp"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    with _LOCK:
        file_exists = os.path.exists(path)
        with open(path, "a", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=FIELDNAMES, delimiter=";")
            if not file_exists:
                writer.writeheader()
            writer.writerow(row)


def log_close(symbol: str, close_reason: str, pnl_pct: float,
              pnl_usdt: float, entry_price: float, exit_price: float,
              mode: str = "DEMO", **kwargs):
    """Удобная обёртка для логирования закрытия позиции."""
    log_decision(
        symbol=symbol,
        event_type=f"POSITION_CLOSED_{close_reason.upper()}",
        close_reason=close_reason,
        pnl_pct=round(pnl_pct, 4),
        pnl_usdt=round(pnl_usdt, 4),
        entry_price=entry_price,
        price=exit_price,
        mode=mode,
        **kwargs,
    )
