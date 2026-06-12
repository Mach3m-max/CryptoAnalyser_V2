# logging_v2/indicator_logger.py
import os, csv, threading
from datetime import datetime
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from config.app_config import DATA_DIR

_LOCK = threading.Lock()
_HEADERS = [
    "timestamp", "pair", "price",
    "sma_50", "sma_100", "sma_200",
    "dev_50", "dev_100", "dev_200",
    "rsi_14", "macd", "macd_signal",
    "bb_upper", "bb_lower", "bb_pct",
    "atr_14", "adx_14", "obv_trend",
    "volume", "signal", "confidence", "mode",
]

def _path(pair: str) -> str:
    date_str = datetime.now().strftime("%Y-%m-%d")
    d = os.path.join(DATA_DIR, "indicators", pair)
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, f"{pair}_{date_str}.csv")

def log_indicators(pair: str, data: dict):
    path = _path(pair)
    row = {f: data.get(f, "") for f in _HEADERS}
    if not row.get("timestamp"):
        row["timestamp"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with _LOCK:
        exists = os.path.exists(path)
        with open(path, "a", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=_HEADERS, delimiter=";")
            if not exists:
                w.writeheader()
            w.writerow(row)
