# indicator_logger.py
"""
Логирование показаний всех индикаторов вместе со свечами.

Файлы: historical_data/{SYMBOL}_indicators.csv
Частота: каждый тик анализа (~1 мин на пару)
Хранение: бессрочно (ротация не применяется)

КОРРЕЛЯЦИЯ С BYBIT:
  Каждая строка содержит `candle_ts_utc` — UTC-время последней 1-минутной
  свечи из Bybit API. Именно по этому полю нужно искать свечу на Bybit графике.

  Пример: candle_ts_utc = "2026-05-23 10:47:00 UTC"
    → Bybit → TIASDT → 1m chart → прокрутить к 10:47 UTC 23 мая
    → label = BUY означает что в следующие 120 минут цена достигла +2% раньше -1%

  ВАЖНО: `timestamp` — это время АНАЛИЗА (local time бота),
         `candle_ts_utc` — это время СВЕЧИ (UTC, из Bybit API).
         Для поиска на Bybit используй candle_ts_utc.

Колонки (~57):
  OHLCV       : timestamp, candle_ts_utc, symbol, open, high, low, close, volume
  SMA         : sma_50..200
  Отклонения  : dev_50..200, avg_deviation, buy_votes, sell_votes
  RSI         : rsi_7, rsi_14, rsi_14_change
  Bollinger   : bb_mid, bb_upper, bb_lower, bb_width, bb_position, bb_squeeze
  ATR         : atr_14, atr_pct, atr_ratio
  MACD        : macd_hist, macd_hist_norm, macd_cross
  Volume      : vol_ratio, vol_change
  Price chg   : price_change_1m..60m
  BTC leading : btc_change_5m/15m/60m, btc_rsi_14, btc_dev_50, btc_atr_pct
  Сигнал      : signal, confidence, source
"""

import csv
import os
import threading
from datetime import datetime, timezone

_BASE_DIR   = os.path.dirname(os.path.abspath(__file__))
_DATA_DIR   = os.path.join(_BASE_DIR, "historical_data")
_write_lock = threading.Lock()

# ── Заголовок ─────────────────────────────────────────────────────────────────
INDICATORS_HEADER = [
    # ── Время ─────────────────────────────────────────────────────────────────
    # timestamp     : момент анализа (local time бота)
    # candle_ts_utc : UTC-время последней свечи из Bybit API
    #                 ← ИМЕННО ЭТО ПОЛЕ используй для поиска на Bybit 1m chart
    'timestamp',
    'candle_ts_utc',
    'symbol',

    # ── OHLCV последней 1m свечи (совпадает с Bybit 1m chart) ────────────────
    'open', 'high', 'low', 'close', 'volume',

    # ── SMA ───────────────────────────────────────────────────────────────────
    'sma_50', 'sma_75', 'sma_100', 'sma_150', 'sma_200',

    # ── Отклонения от SMA ─────────────────────────────────────────────────────
    'dev_50', 'dev_75', 'dev_100', 'dev_150', 'dev_200',
    'avg_deviation', 'buy_votes', 'sell_votes',

    # ── RSI ───────────────────────────────────────────────────────────────────
    'rsi_7', 'rsi_14', 'rsi_14_change',

    # ── Bollinger Bands (20) ──────────────────────────────────────────────────
    'bb_mid', 'bb_upper', 'bb_lower', 'bb_width', 'bb_position', 'bb_squeeze',

    # ── ATR ───────────────────────────────────────────────────────────────────
    'atr_14', 'atr_pct', 'atr_ratio',

    # ── MACD ──────────────────────────────────────────────────────────────────
    'macd_hist', 'macd_hist_norm', 'macd_cross',

    # ── Объём ─────────────────────────────────────────────────────────────────
    'vol_ratio', 'vol_change',

    # ── Динамика цены ─────────────────────────────────────────────────────────
    'price_change_1m', 'price_change_5m', 'price_change_15m',
    'price_change_30m', 'price_change_60m',

    # ── BTC leading indicators (0.0 если не применимо) ────────────────────────
    'btc_change_5m', 'btc_change_15m', 'btc_change_60m',
    'btc_rsi_14', 'btc_dev_50', 'btc_atr_pct',

    # ── Выход стратегии ───────────────────────────────────────────────────────
    'signal', 'confidence', 'source',
]


def _ensure_dir():
    os.makedirs(_DATA_DIR, exist_ok=True)


def _filepath(symbol: str) -> str:
    return os.path.join(_DATA_DIR, f"{symbol}_indicators.csv")


def _utc_now_str() -> str:
    """Текущее UTC время в формате 'YYYY-MM-DD HH:MM:SS UTC'."""
    return datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')


def log_indicators(
    symbol:       str,
    # UTC-время последней свечи (из df.iloc[-1]['timestamp'] Bybit API)
    # Если None — будет использовано текущее UTC время как приближение
    candle_ts_utc: str = None,
    # OHLCV последней свечи
    candle_open:  float = 0.0,
    candle_high:  float = 0.0,
    candle_low:   float = 0.0,
    candle_close: float = 0.0,
    candle_vol:   float = 0.0,
    # SMA
    sma_50:  float = 0.0,
    sma_75:  float = 0.0,
    sma_100: float = 0.0,
    sma_150: float = 0.0,
    sma_200: float = 0.0,
    # Отклонения
    dev_50:  float = 0.0,
    dev_75:  float = 0.0,
    dev_100: float = 0.0,
    dev_150: float = 0.0,
    dev_200: float = 0.0,
    avg_deviation: float = 0.0,
    buy_votes:  int = 0,
    sell_votes: int = 0,
    # RSI
    rsi_7:         float = 50.0,
    rsi_14:        float = 50.0,
    rsi_14_change: float = 0.0,
    # Bollinger
    bb_mid:      float = 0.0,
    bb_upper:    float = 0.0,
    bb_lower:    float = 0.0,
    bb_width:    float = 0.0,
    bb_position: float = 0.5,
    bb_squeeze:  float = 0.0,
    # ATR
    atr_14:  float = 0.0,
    atr_pct: float = 0.0,
    atr_ratio: float = 1.0,
    # MACD
    macd_hist:      float = 0.0,
    macd_hist_norm: float = 0.0,
    macd_cross:     float = 0.0,
    # Volume
    vol_ratio:  float = 1.0,
    vol_change: float = 1.0,
    # Price changes
    price_change_1m:  float = 0.0,
    price_change_5m:  float = 0.0,
    price_change_15m: float = 0.0,
    price_change_30m: float = 0.0,
    price_change_60m: float = 0.0,
    # BTC leading
    btc_change_5m:  float = 0.0,
    btc_change_15m: float = 0.0,
    btc_change_60m: float = 0.0,
    btc_rsi_14:     float = 50.0,
    btc_dev_50:     float = 0.0,
    btc_atr_pct:    float = 0.0,
    # Сигнал
    signal:     str   = 'HOLD',
    confidence: float = 0.0,
    source:     str   = 'unknown',
):
    """
    Записывает одну строку в {SYMBOL}_indicators.csv.
    Потокобезопасно (общий лок на все файлы).

    candle_ts_utc — UTC-время последней свечи из Bybit API.
    Передавай `str(df.iloc[-1]['timestamp']) + ' UTC'` из ml_strategy_engine.
    Именно это поле позволяет найти свечу на Bybit 1m графике.
    """
    _ensure_dir()
    path = _filepath(symbol)
    file_exists = os.path.exists(path)

    row = {
        'timestamp':        datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        'candle_ts_utc':    candle_ts_utc if candle_ts_utc else _utc_now_str(),
        'symbol':           symbol,
        'open':             round(candle_open,  8),
        'high':             round(candle_high,  8),
        'low':              round(candle_low,   8),
        'close':            round(candle_close, 8),
        'volume':           round(candle_vol,   4),
        'sma_50':           round(sma_50,  6),
        'sma_75':           round(sma_75,  6),
        'sma_100':          round(sma_100, 6),
        'sma_150':          round(sma_150, 6),
        'sma_200':          round(sma_200, 6),
        'dev_50':           round(dev_50,  6),
        'dev_75':           round(dev_75,  6),
        'dev_100':          round(dev_100, 6),
        'dev_150':          round(dev_150, 6),
        'dev_200':          round(dev_200, 6),
        'avg_deviation':    round(avg_deviation,    6),
        'buy_votes':        int(buy_votes),
        'sell_votes':       int(sell_votes),
        'rsi_7':            round(rsi_7,  4),
        'rsi_14':           round(rsi_14, 4),
        'rsi_14_change':    round(rsi_14_change, 4),
        'bb_mid':           round(bb_mid,      8),
        'bb_upper':         round(bb_upper,    8),
        'bb_lower':         round(bb_lower,    8),
        'bb_width':         round(bb_width,    6),
        'bb_position':      round(bb_position, 6),
        'bb_squeeze':       int(bb_squeeze),
        'atr_14':           round(atr_14,  8),
        'atr_pct':          round(atr_pct, 6),
        'atr_ratio':        round(atr_ratio, 6),
        'macd_hist':        round(macd_hist,      8),
        'macd_hist_norm':   round(macd_hist_norm, 6),
        'macd_cross':       round(macd_cross,     1),
        'vol_ratio':        round(vol_ratio,  4),
        'vol_change':       round(vol_change, 4),
        'price_change_1m':  round(price_change_1m,  6),
        'price_change_5m':  round(price_change_5m,  6),
        'price_change_15m': round(price_change_15m, 6),
        'price_change_30m': round(price_change_30m, 6),
        'price_change_60m': round(price_change_60m, 6),
        'btc_change_5m':    round(btc_change_5m,  6),
        'btc_change_15m':   round(btc_change_15m, 6),
        'btc_change_60m':   round(btc_change_60m, 6),
        'btc_rsi_14':       round(btc_rsi_14,  4),
        'btc_dev_50':       round(btc_dev_50,  6),
        'btc_atr_pct':      round(btc_atr_pct, 6),
        'signal':           signal,
        'confidence':       round(confidence, 6),
        'source':           source,
    }

    try:
        with _write_lock:
            with open(path, 'a', newline='', encoding='utf-8') as f:
                writer = csv.DictWriter(f, fieldnames=INDICATORS_HEADER,
                                        extrasaction='ignore')
                if not file_exists:
                    writer.writeheader()
                writer.writerow(row)
    except Exception as e:
        print(f"⚠️ [indicator_logger] write error {symbol}: {e}")


def log_indicators_from_dict(symbol: str, feat: dict,
                              candle: dict = None,
                              signal: str = 'HOLD',
                              confidence: float = 0.0,
                              source: str = 'unknown',
                              candle_ts_utc: str = None):
    """
    Удобная обёртка: принимает dict фичей (из ML _build_features)
    и отдельно dict свечи {'open','high','low','close','volume'}.
    Используется в ml_strategy_engine.py.

    candle_ts_utc: UTC-время последней свечи из df.iloc[-1]['timestamp'].
    Пример передачи из ml_strategy_engine:
        candle_ts = df.iloc[-1].get('timestamp')
        if candle_ts:
            ts_utc = str(candle_ts)[:19] + ' UTC'
        else:
            ts_utc = None
        log_indicators_from_dict(..., candle_ts_utc=ts_utc)
    """
    c = candle or {}
    log_indicators(
        symbol        = symbol,
        candle_ts_utc = candle_ts_utc,
        candle_open   = c.get('open',   feat.get('close', 0.0)),
        candle_high   = c.get('high',   feat.get('close', 0.0)),
        candle_low    = c.get('low',    feat.get('close', 0.0)),
        candle_close  = c.get('close',  feat.get('close', 0.0)),
        candle_vol    = c.get('volume', feat.get('volume_last', 0.0)),
        # SMA — могут отсутствовать в feat (ML не хранит абсолютные SMA)
        sma_50  = feat.get('sma_50',  0.0),
        sma_75  = feat.get('sma_75',  0.0),
        sma_100 = feat.get('sma_100', 0.0),
        sma_150 = feat.get('sma_150', 0.0),
        sma_200 = feat.get('sma_200', 0.0),
        # Отклонения
        dev_50        = feat.get('dev_50',        0.0),
        dev_75        = feat.get('dev_75',        0.0),
        dev_100       = feat.get('dev_100',       0.0),
        dev_150       = feat.get('dev_150',       0.0),
        dev_200       = feat.get('dev_200',       0.0),
        avg_deviation = feat.get('avg_deviation', 0.0),
        buy_votes     = int(feat.get('buy_votes',  0)),
        sell_votes    = int(feat.get('sell_votes', 0)),
        # RSI
        rsi_7         = feat.get('rsi_7',         50.0),
        rsi_14        = feat.get('rsi_14',        50.0),
        rsi_14_change = feat.get('rsi_14_change', 0.0),
        # Bollinger
        bb_mid      = feat.get('bb_mid',      0.0),
        bb_upper    = feat.get('bb_upper',    0.0),
        bb_lower    = feat.get('bb_lower',    0.0),
        bb_width    = feat.get('bb_width',    0.0),
        bb_position = feat.get('bb_position', 0.5),
        bb_squeeze  = feat.get('bb_squeeze',  0.0),
        # ATR
        atr_14    = feat.get('atr_14',    0.0),
        atr_pct   = feat.get('atr_pct',   0.0),
        atr_ratio = feat.get('atr_ratio', 1.0),
        # MACD
        macd_hist      = feat.get('macd_hist',      0.0),
        macd_hist_norm = feat.get('macd_hist_norm', 0.0),
        macd_cross     = feat.get('macd_cross',     0.0),
        # Volume
        vol_ratio  = feat.get('vol_ratio',  1.0),
        vol_change = feat.get('vol_change', 1.0),
        # Price changes
        price_change_1m  = feat.get('price_change_1m',  0.0),
        price_change_5m  = feat.get('price_change_5m',  0.0),
        price_change_15m = feat.get('price_change_15m', 0.0),
        price_change_30m = feat.get('price_change_30m', 0.0),
        price_change_60m = feat.get('price_change_60m', 0.0),
        # BTC leading
        btc_change_5m  = feat.get('btc_change_5m',  0.0),
        btc_change_15m = feat.get('btc_change_15m', 0.0),
        btc_change_60m = feat.get('btc_change_60m', 0.0),
        btc_rsi_14     = feat.get('btc_rsi_14',     50.0),
        btc_dev_50     = feat.get('btc_dev_50',     0.0),
        btc_atr_pct    = feat.get('btc_atr_pct',   0.0),
        # Сигнал
        signal     = signal,
        confidence = confidence,
        source     = source,
    )
