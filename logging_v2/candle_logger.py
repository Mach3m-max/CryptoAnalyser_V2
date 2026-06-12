# logging_v2/candle_logger.py
"""
Посуточная запись минутных свечей.
Файлы: data/candles/{PAIR}/{PAIR}_YYYY-MM-DD.csv
"""
import os
import csv
import threading
from datetime import datetime

import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from config.app_config import DATA_DIR

_LOCK    = threading.Lock()
_HEADERS = ["timestamp", "open", "high", "low", "close", "volume", "pair"]


def _path(pair: str) -> str:
    date_str = datetime.now().strftime("%Y-%m-%d")
    d = os.path.join(DATA_DIR, "candles", pair)
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, f"{pair}_{date_str}.csv")


def log_candle(pair: str, ts: str, o: float, h: float,
               l: float, c: float, vol: float):
    """Записывает одну свечу в дневной файл пары."""
    path = _path(pair)
    row  = [ts, o, h, l, c, vol, pair]
    with _LOCK:
        exists = os.path.exists(path)
        with open(path, "a", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            if not exists:
                writer.writerow(_HEADERS)
            writer.writerow(row)
