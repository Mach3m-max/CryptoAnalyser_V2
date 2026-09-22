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
    # ── поля v3: "живой" датасет для анализа/дообучения ────────────────────
    "source",           # какая модель дала сигнал: ml_rf / ml_xgb
    "f1_model",         # offline F1-score этой модели (из train.py)
    "rsi_14",
    "atr_pct",
    "bb_position",
    "bb_width",
    "macd_hist_norm",
    "dev_spread",
    "dev_momentum",
    "deviation",
    "indicators_json",  # catch-all: полный сырой словарь signal на момент решения
]


def _today_path() -> str:
    date_str = datetime.now().strftime("%Y-%m-%d")
    return os.path.join(DECISIONS_DIR, f"decisions_{date_str}.csv")


def signal_snapshot(signal: dict) -> dict:
    """
    Извлекает из словаря signal (см. ml_strategy_engine) поля для 'живого'
    датасета — источник модели + ключевые индикаторы на момент решения.
    Использование: log_decision(..., **signal_snapshot(signal))
                   log_close(..., **signal_snapshot(signal))
    Полный сырой signal дополнительно кладётся в indicators_json — на случай,
    если понадобится индикатор, для которого пока нет отдельной колонки.
    """
    if not signal:
        return {}
    import json as _json
    fields = ('source', 'f1_model', 'rsi_14', 'atr_pct', 'bb_position',
              'bb_width', 'macd_hist_norm', 'dev_spread', 'dev_momentum',
              'deviation')
    snap = {f: signal.get(f, "") for f in fields}
    try:
        snap['indicators_json'] = _json.dumps(signal, ensure_ascii=False, default=str)
    except Exception:
        snap['indicators_json'] = ""
    return snap


def _ensure_schema(path: str):
    """
    Если файл уже существует, но его шапка не совпадает с текущими
    FIELDNAMES (например, бот обновили в середине дня, добавив новые поля) —
    переименовываем старый файл в .schema_bak и начинаем новый с актуальной
    шапкой. Смешение разных схем в одном файле ломает и чтение (pandas),
    и сам файл для будущего анализа.
    """
    if not os.path.exists(path):
        return
    try:
        with open(path, "r", encoding="utf-8") as f:
            header_line = f.readline().rstrip("\n\r")
        existing = header_line.split(";")
        if existing != FIELDNAMES:
            bak_path = path + f".schema_bak_{datetime.now().strftime('%H%M%S')}"
            os.replace(path, bak_path)
            print(f"⚠️ Схема decisions-лога изменилась — старый файл сохранён как "
                  f"{os.path.basename(bak_path)}, начат новый с актуальной шапкой")
    except Exception as e:
        print(f"⚠️ Не удалось проверить схему {path}: {e}")


def ensure_today_schema():
    """
    Публичная обёртка — проверяет и при необходимости чинит схему СЕГОДНЯШНЕГО
    файла сразу при старте бота, не дожидаясь первой записи через log_decision().
    Без этого файл, начатый до обновления набора полей, продолжает ломать
    чтение (pandas/analytics) вплоть до первой новой сделки за день.
    """
    with _LOCK:
        _ensure_schema(_today_path())


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
        _ensure_schema(path)
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
