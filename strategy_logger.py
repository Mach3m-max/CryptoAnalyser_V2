# strategy_logger.py
"""
Логирование данных стратегии для последующего анализа.

Файлы (все в historical_data/):
  BTCUSDT_signals.csv  — каждый тик анализа по паре (1 раз в ~60 сек)
  decisions.csv        — каждое событие принятия решения (вход/выход/пропуск)

Колонки signals.csv:
  timestamp, symbol, price,
  sma_50, sma_75, sma_100, sma_150, sma_200,
  dev_50, dev_75, dev_100, dev_150, dev_200,
  avg_deviation, buy_votes, sell_votes, total_votes,
  signal, confidence,
  volume_last, price_change_1m, price_change_5m, price_change_15m,
  entry_threshold

Колонки decisions.csv:
  timestamp, symbol, event_type, signal, confidence, price,
  avg_deviation, buy_votes, sell_votes,
  dev_50, dev_75, dev_100, dev_150, dev_200,
  reason,
  entry_price, tp_price, sl_price, tp_pct, sl_pct,
  usdt_amount, qty,
  portfolio_open_count, total_capital,
  mode
"""

import csv
import os
import threading
from datetime import datetime

_BASE_DIR  = os.path.dirname(os.path.abspath(__file__))
_DATA_DIR  = os.path.join(_BASE_DIR, "historical_data")

# Один лок на все файловые операции — избегаем race condition между потоками
_write_lock = threading.Lock()

# ─────────────────────────────────────────────────────────────────────────────
# Вспомогательные функции
# ─────────────────────────────────────────────────────────────────────────────

def _ensure_dir():
    os.makedirs(_DATA_DIR, exist_ok=True)


def _write_row(filepath: str, header: list, row: dict):
    """Атомарная запись строки в CSV. Создаёт файл с заголовком если нет."""
    _ensure_dir()
    file_exists = os.path.exists(filepath)
    with _write_lock:
        with open(filepath, 'a', newline='', encoding='utf-8') as f:
            writer = csv.DictWriter(f, fieldnames=header, extrasaction='ignore')
            if not file_exists:
                writer.writeheader()
            writer.writerow(row)


# ─────────────────────────────────────────────────────────────────────────────
# SIGNALS LOG — каждый тик по паре
# ─────────────────────────────────────────────────────────────────────────────

SIGNALS_HEADER = [
    'timestamp', 'symbol', 'price',
    'sma_50', 'sma_75', 'sma_100', 'sma_150', 'sma_200',
    'dev_50', 'dev_75', 'dev_100', 'dev_150', 'dev_200',
    'avg_deviation', 'buy_votes', 'sell_votes', 'total_votes',
    'signal', 'confidence',
    'volume_last', 'price_change_1m', 'price_change_5m', 'price_change_15m',
    'entry_threshold',
]


def log_signal_tick(
    symbol: str,
    price: float,
    sma_values: dict,       # {'50': val, '75': val, ...}
    dev_values: dict,       # {'50': val, '75': val, ...}
    avg_deviation: float,
    buy_votes: int,
    sell_votes: int,
    total_votes: int,
    signal: str,
    confidence: float,
    volume_last: float = 0.0,
    price_change_1m: float = 0.0,
    price_change_5m: float = 0.0,
    price_change_15m: float = 0.0,
    entry_threshold: float = 2.0,
):
    """Записывает одну строку в SYMBOL_signals.csv"""
    filepath = os.path.join(_DATA_DIR, f"{symbol}_signals.csv")
    row = {
        'timestamp':       datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        'symbol':          symbol,
        'price':           round(price, 8),
        'sma_50':          round(sma_values.get('50', 0), 8),
        'sma_75':          round(sma_values.get('75', 0), 8),
        'sma_100':         round(sma_values.get('100', 0), 8),
        'sma_150':         round(sma_values.get('150', 0), 8),
        'sma_200':         round(sma_values.get('200', 0), 8),
        'dev_50':          round(dev_values.get('50', 0), 6),
        'dev_75':          round(dev_values.get('75', 0), 6),
        'dev_100':         round(dev_values.get('100', 0), 6),
        'dev_150':         round(dev_values.get('150', 0), 6),
        'dev_200':         round(dev_values.get('200', 0), 6),
        'avg_deviation':   round(avg_deviation, 6),
        'buy_votes':       buy_votes,
        'sell_votes':      sell_votes,
        'total_votes':     total_votes,
        'signal':          signal,
        'confidence':      round(confidence, 6),
        'volume_last':     round(volume_last, 2),
        'price_change_1m': round(price_change_1m, 6),
        'price_change_5m': round(price_change_5m, 6),
        'price_change_15m':round(price_change_15m, 6),
        'entry_threshold': entry_threshold,
    }
    try:
        _write_row(filepath, SIGNALS_HEADER, row)
    except Exception as e:
        print(f"⚠️ [logger] signals write error {symbol}: {e}")


# ─────────────────────────────────────────────────────────────────────────────
# DECISIONS LOG — события принятия решений
# ─────────────────────────────────────────────────────────────────────────────

DECISIONS_HEADER = [
    'timestamp', 'symbol', 'event_type',
    'signal', 'confidence', 'price',
    'avg_deviation', 'buy_votes', 'sell_votes',
    'dev_50', 'dev_75', 'dev_100', 'dev_150', 'dev_200',
    'reason',
    'entry_price', 'tp_price', 'sl_price', 'tp_pct', 'sl_pct',
    'usdt_amount', 'qty',
    'portfolio_open_count', 'total_capital',
    'mode',
]

# Типы событий:
# SIGNAL_BUY / SIGNAL_SELL      — стратегия сгенерировала сигнал
# EXECUTED                       — ордер выставлен
# SKIPPED_CONF                   — пропущен: confidence < min_conf
# SKIPPED_DEBOUNCE               — пропущен: дебаунс 5 мин
# SKIPPED_POSITION               — пропущен: уже в позиции
# SKIPPED_TRADING_OFF            — пропущен: торговля выключена
# SKIPPED_LIMIT                  — пропущен: лимит открытых позиций
# POSITION_OPENED                — позиция зарегистрирована
# POSITION_CLOSED_TP             — закрыта по TP
# POSITION_CLOSED_SL             — закрыта по SL
# POSITION_CLOSED_MANUAL         — закрыта вручную


def log_decision(
    symbol: str,
    event_type: str,
    signal: str             = '',
    confidence: float       = 0.0,
    price: float            = 0.0,
    avg_deviation: float    = 0.0,
    buy_votes: int          = 0,
    sell_votes: int         = 0,
    dev_values: dict        = None,
    reason: str             = '',
    entry_price: float      = 0.0,
    tp_price: float         = 0.0,
    sl_price: float         = 0.0,
    tp_pct: float           = 0.0,
    sl_pct: float           = 0.0,
    usdt_amount: float      = 0.0,
    qty: float              = 0.0,
    portfolio_open_count: int = 0,
    total_capital: float    = 0.0,
    mode: str               = 'DEMO',
):
    """Записывает событие решения в decisions.csv"""
    if dev_values is None:
        dev_values = {}
    filepath = os.path.join(_DATA_DIR, "decisions.csv")
    row = {
        'timestamp':            datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        'symbol':               symbol,
        'event_type':           event_type,
        'signal':               signal,
        'confidence':           round(confidence, 6),
        'price':                round(price, 8),
        'avg_deviation':        round(avg_deviation, 6),
        'buy_votes':            buy_votes,
        'sell_votes':           sell_votes,
        'dev_50':               round(dev_values.get('50', 0), 6),
        'dev_75':               round(dev_values.get('75', 0), 6),
        'dev_100':              round(dev_values.get('100', 0), 6),
        'dev_150':              round(dev_values.get('150', 0), 6),
        'dev_200':              round(dev_values.get('200', 0), 6),
        'reason':               reason,
        'entry_price':          round(entry_price, 8),
        'tp_price':             round(tp_price, 8),
        'sl_price':             round(sl_price, 8),
        'tp_pct':               tp_pct,
        'sl_pct':               sl_pct,
        'usdt_amount':          round(usdt_amount, 2),
        'qty':                  round(qty, 8),
        'portfolio_open_count': portfolio_open_count,
        'total_capital':        round(total_capital, 2),
        'mode':                 mode,
    }
    try:
        _write_row(filepath, DECISIONS_HEADER, row)
    except Exception as e:
        print(f"⚠️ [logger] decisions write error {symbol}: {e}")
