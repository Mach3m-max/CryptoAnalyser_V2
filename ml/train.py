from __future__ import annotations
"""
ml/train.py  v3
===============
Обучение ML-моделей (XGBoost vs RandomForest) на исторических данных бота.
Запуск: python ml/train.py

Улучшения v3:
  1. BTC leading indicators — 7 новых фичей для всех пар кроме BTC
     (btc_change_5m/15m/60m, btc_rsi_14, btc_dev_50, btc_atr_pct, pair_vs_btc_15m)
  2. BTC движется первым в ~70% случаев — снижает шум при разметке альтов
"""

import os, sys, json, pickle, logging
import numpy as np
import pandas as pd
from pathlib import Path
from collections import Counter
from datetime import datetime

try:
    from sklearn.ensemble import RandomForestClassifier
    from sklearn.metrics import classification_report, f1_score
    from sklearn.preprocessing import LabelEncoder
    from sklearn.utils.class_weight import compute_class_weight
except ImportError:
    print("❌ Установи sklearn: pip install scikit-learn")
    sys.exit(1)

try:
    import xgboost as xgb
    HAS_XGB = True
except ImportError:
    print("⚠️  XGBoost не найден, будет только RandomForest. Установи: pip install xgboost")
    HAS_XGB = False

BASE_DIR       = Path(__file__).parent.parent          # CryptoAnalyzer/
_OLD_DIR       = BASE_DIR / "historical_data" / "Old"  # Old/ если скачано через download_history.py
DATA_DIR       = _OLD_DIR if _OLD_DIR.exists() and any(_OLD_DIR.glob("*.csv")) \
                 else BASE_DIR / "historical_data"      # иначе обычная папка
MODELS_DIR     = Path(__file__).parent / "models"
MODELS_DIR.mkdir(parents=True, exist_ok=True)

TP_PCT         = 0.02   # было 0.04 — снизили чтобы BUY/SELL меток стало больше
SL_PCT         = 0.01   # было 0.02 — симметрично TP (соотношение 2:1)
LOOKAHEAD      = 120    # было 240 — 2 часа вместо 4-х: меньше шума в разметке
MIN_CANDLES    = 300
TEST_RATIO     = 0.2
MIN_SIGNAL_PCT = 0.005
HOLD_RATIO     = 5.0    # undersample: макс HOLD = HOLD_RATIO x (BUY+SELL)
MIN_HOLD       = 60     # гарантируем минимум HOLD-сэмплов (защита от unseen label)

FEATURES = [
    'dev_50', 'dev_75', 'dev_100', 'dev_150', 'dev_200',
    'avg_deviation', 'confidence', 'buy_votes', 'sell_votes',
    'dev_spread', 'dev_momentum',
    'price_change_1m', 'price_change_5m', 'price_change_15m',
    'price_change_30m', 'price_change_60m',
    'volume_last', 'vol_ratio', 'vol_change',
    'rsi_14', 'rsi_7', 'rsi_14_change',
    'bb_position', 'bb_width', 'bb_squeeze',
    'atr_pct', 'atr_ratio',
    'macd_hist_norm', 'macd_cross',
]

# BTC-фичи добавляются для всех пар КРОМЕ самого BTC
BTC_FEATURES = [
    'btc_change_5m',    # % изменение BTC за 5 мин   — мгновенный импульс
    'btc_change_15m',   # % изменение BTC за 15 мин  — краткосрочный тренд
    'btc_change_60m',   # % изменение BTC за 60 мин  — часовое направление
    'btc_rsi_14',       # RSI BTC — перекуплен/перепродан рынок в целом
    'btc_dev_50',       # отклонение BTC от SMA-50   — где BTC относительно нормы
    'btc_atr_pct',      # волатильность BTC           — риск-режим рынка
    'pair_vs_btc_15m',  # % пары - % BTC за 15м       — опережает/отстаёт пара
]

logging.basicConfig(level=logging.INFO,
                    format='%(asctime)s  %(levelname)s  %(message)s',
                    datefmt='%H:%M:%S')
log = logging.getLogger(__name__)


def compute_rsi(closes: pd.Series, period: int) -> pd.Series:
    delta = closes.diff()
    gain  = delta.clip(lower=0).rolling(period, min_periods=period).mean()
    loss  = (-delta.clip(upper=0)).rolling(period, min_periods=period).mean()
    return 100 - (100 / (1 + gain / (loss + 1e-9)))


def compute_indicators(candles: pd.DataFrame) -> pd.DataFrame:
    df     = candles.copy()
    closes = df['close'].astype(float)
    highs  = df['high'].astype(float)   if 'high'   in df.columns else closes
    lows   = df['low'].astype(float)    if 'low'    in df.columns else closes
    vols   = df['volume'].astype(float) if 'volume' in df.columns else pd.Series(0.0, index=df.index)

    # SMA-отклонения
    windows = {'50': 50, '75': 75, '100': 100, '150': 150, '200': 200}
    for name, w in windows.items():
        sma = closes.rolling(w).mean()
        df[f'dev_{name}'] = (closes - sma) / sma * 100

    dev_cols = [f'dev_{w}' for w in windows]
    df['avg_deviation'] = df[dev_cols].mean(axis=1)
    thr = 2.0
    df['buy_votes']    = (df[dev_cols] < -thr).sum(axis=1).astype(float)
    df['sell_votes']   = (df[dev_cols] >  thr).sum(axis=1).astype(float)
    df['confidence']   = (df['avg_deviation'].abs() / thr).clip(upper=1.0)
    df['dev_spread']   = df['dev_50'] - df['dev_200']
    df['dev_momentum'] = df['dev_50'] - df['dev_100']

    # Динамика цены
    df['price_change_1m']  = closes.pct_change(1)  * 100
    df['price_change_5m']  = closes.pct_change(5)  * 100
    df['price_change_15m'] = closes.pct_change(15) * 100
    df['price_change_30m'] = closes.pct_change(30) * 100
    df['price_change_60m'] = closes.pct_change(60) * 100

    # Объём
    df['volume_last'] = vols
    vol_mean20        = vols.rolling(20).mean()
    df['vol_ratio']   = vols / (vol_mean20 + 1e-9)
    df['vol_change']  = vols.rolling(5).mean() / (vol_mean20 + 1e-9)

    # RSI
    rsi14 = compute_rsi(closes, 14)
    rsi7  = compute_rsi(closes, 7)
    df['rsi_14']        = rsi14
    df['rsi_7']         = rsi7
    df['rsi_14_change'] = rsi14.diff(3)

    # Bollinger Bands
    bb_mid = closes.rolling(20).mean()
    bb_std = closes.rolling(20).std()
    bb_up  = bb_mid + 2 * bb_std
    bb_low = bb_mid - 2 * bb_std
    bb_wid = (bb_up - bb_low) / (bb_mid + 1e-9) * 100
    df['bb_position'] = (closes - bb_low) / (bb_up - bb_low + 1e-9)
    df['bb_width']    = bb_wid
    df['bb_squeeze']  = (bb_wid < bb_wid.shift(5)).astype(float)

    # ATR
    tr = pd.concat([
        highs - lows,
        (highs - closes.shift()).abs(),
        (lows  - closes.shift()).abs(),
    ], axis=1).max(axis=1)
    atr14 = tr.rolling(14).mean()
    atr50 = tr.rolling(50).mean()
    df['atr_pct']   = atr14 / (closes + 1e-9) * 100
    df['atr_ratio'] = atr14 / (atr50 + 1e-9)

    # MACD
    ema12       = closes.ewm(span=12, adjust=False).mean()
    ema26       = closes.ewm(span=26, adjust=False).mean()
    macd_line   = ema12 - ema26
    macd_signal = macd_line.ewm(span=9, adjust=False).mean()
    macd_hist   = macd_line - macd_signal
    df['macd_hist_norm'] = macd_hist / (atr14 + 1e-9)
    prev_hist = macd_hist.shift(1)
    df['macd_cross'] = np.where(
        (macd_hist > 0) & (prev_hist <= 0),  1.0,
        np.where((macd_hist < 0) & (prev_hist >= 0), -1.0, 0.0)
    )

    return df


def compute_btc_features(df_pair: pd.DataFrame, df_btc: pd.DataFrame) -> pd.DataFrame:
    """
    Мержит BTC-индикаторы в датафрейм пары по временной метке.
    Возвращает df_pair с добавленными BTC_FEATURES колонками.
    """
    btc = df_btc[['timestamp', 'close']].copy().rename(columns={'close': 'btc_close'})
    btc = btc.sort_values('timestamp').reset_index(drop=True)

    # Вычисляем BTC-индикаторы
    c = btc['btc_close'].astype(float)

    btc['btc_change_5m']  = c.pct_change(5)  * 100
    btc['btc_change_15m'] = c.pct_change(15) * 100
    btc['btc_change_60m'] = c.pct_change(60) * 100
    btc['btc_rsi_14']     = compute_rsi(c, 14)

    sma50 = c.rolling(50).mean()
    btc['btc_dev_50'] = (c - sma50) / (sma50 + 1e-9) * 100

    highs = btc['btc_close']   # у нас только close — ATR упрощённый
    tr    = c.diff().abs()
    btc['btc_atr_pct'] = tr.rolling(14).mean() / (c + 1e-9) * 100

    btc = btc.drop(columns=['btc_close'])

    # Merge as-of по ближайшей метке (tolerance 2 мин)
    df_pair = df_pair.sort_values('timestamp').reset_index(drop=True)
    merged  = pd.merge_asof(
        df_pair, btc,
        on='timestamp', direction='nearest',
        tolerance=pd.Timedelta('2min')
    )

    # pair_vs_btc_15m: насколько пара опережает/отстаёт от BTC за 15 мин
    if 'price_change_15m' in merged.columns and 'btc_change_15m' in merged.columns:
        merged['pair_vs_btc_15m'] = (
            merged['price_change_15m'] - merged['btc_change_15m']
        )
    else:
        merged['pair_vs_btc_15m'] = 0.0

    return merged


def label_candles(prices: list, tp=TP_PCT, sl=SL_PCT, lookahead=LOOKAHEAD) -> list:
    n = len(prices)
    labels = ['HOLD'] * n
    for i in range(n - lookahead):
        entry = prices[i]
        tp_buy = entry * (1 + tp);  sl_buy  = entry * (1 - sl)
        tp_sel = entry * (1 - tp);  sl_sel  = entry * (1 + sl)
        b = s = None
        for j in range(i + 1, i + lookahead):
            p = prices[j]
            if b is None:
                if p >= tp_buy: b = 'TP'
                elif p <= sl_buy: b = 'SL'
            if s is None:
                if p <= tp_sel: s = 'TP'
                elif p >= sl_sel: s = 'SL'
            if b and s: break
        if b == 'TP' and s != 'TP': labels[i] = 'BUY'
        elif s == 'TP' and b != 'TP': labels[i] = 'SELL'
    return labels


def load_candles(pair: str):
    """
    Ищет CSV для пары в DATA_DIR.
    Приоритет: больше свечей = лучше.
    Поддерживает: _365d.csv, _180d.csv, _90d.csv, _30d.csv и любые другие суффиксы.
    """
    candidates = sorted(DATA_DIR.glob(f"{pair}_*.csv"))
    if not candidates:
        log.warning(f"  Нет файла: {pair}_*.csv в {DATA_DIR}")
        return None

    # Выбираем файл с наибольшим числом строк (больше данных = лучше)
    best_path  = None
    best_lines = 0
    for p in candidates:
        try:
            lines = sum(1 for _ in open(p)) - 1   # минус заголовок
            if lines > best_lines:
                best_lines = lines
                best_path  = p
        except Exception:
            continue

    if best_path is None:
        log.warning(f"  Не удалось прочитать файлы для {pair}")
        return None

    log.info(f"  Файл: {best_path.name}  ({best_lines:,} строк)")
    df = pd.read_csv(best_path, parse_dates=['timestamp'])
    df = df.sort_values('timestamp').reset_index(drop=True)
    for col in ('open', 'high', 'low', 'close', 'volume'):
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors='coerce')
    return df


def build_dataset(pair: str, df_btc: pd.DataFrame | None = None):
    candles = load_candles(pair)
    if candles is None or len(candles) < MIN_CANDLES:
        return None, None, None

    log.info(f"  Разметка {len(candles)} свечей...")
    candles['label'] = label_candles(candles['close'].tolist())
    df = compute_indicators(candles)

    # BTC leading indicators — для всех пар кроме самого BTC
    use_btc_feats = (pair != 'BTCUSDT') and (df_btc is not None) and (len(df_btc) > 60)
    all_features  = FEATURES + (BTC_FEATURES if use_btc_feats else [])

    if use_btc_feats:
        df = compute_btc_features(df, df_btc)
        log.info(f"  ✅ BTC leading indicators добавлены (+{len(BTC_FEATURES)} фичей)")

    available = [f for f in all_features if f in df.columns]
    missing   = set(all_features) - set(available)
    if missing:
        log.warning(f"  Отсутствуют фичи: {missing}")

    X = df[available].copy()
    y = df['label'].copy()
    mask = X.notna().all(axis=1)
    X, y = X[mask], y[mask]

    cnt    = Counter(y)
    active = cnt['BUY'] + cnt['SELL']
    log.info(f"  До undersample  : BUY={cnt['BUY']} SELL={cnt['SELL']} HOLD={cnt['HOLD']}")

    if active == 0 or active / len(y) < MIN_SIGNAL_PCT:
        log.warning(f"  Мало активных сигналов ({active}), пропускаем")
        return None, None, None

    # Undersample HOLD — сохраняем хронологию
    # MIN_HOLD гарантирует, что HOLD класс присутствует в обучающей выборке
    max_hold   = max(int(active * HOLD_RATIO), MIN_HOLD)
    hold_idx   = y[y == 'HOLD'].index.tolist()
    active_idx = y[y != 'HOLD'].index.tolist()
    if len(hold_idx) > max_hold:
        rng      = np.random.default_rng(42)
        hold_idx = sorted(rng.choice(hold_idx, size=max_hold, replace=False).tolist())

    keep = sorted(active_idx + hold_idx)
    X = X.loc[keep].reset_index(drop=True)
    y = y.loc[keep].reset_index(drop=True)

    cnt2 = Counter(y)
    log.info(f"  После undersample: BUY={cnt2['BUY']} SELL={cnt2['SELL']} HOLD={cnt2.get('HOLD', 0)}")

    # Все три класса обязаны присутствовать — иначе модель не знает HOLD при инференсе
    missing = {'BUY', 'SELL', 'HOLD'} - set(cnt2.keys())
    if missing:
        log.error(f"  ❌ Отсутствуют классы: {missing} — переобучение невозможно, пропускаем")
        return None, None, None

    return X, y, available


def train_pair(pair: str, df_btc: pd.DataFrame | None = None):
    log.info(f"\n{'='*60}\n  Пара: {pair}\n{'='*60}")

    X, y, feature_names = build_dataset(pair, df_btc)
    if X is None:
        return None

    cnt   = Counter(y)
    split = int(len(X) * (1 - TEST_RATIO))
    X_tr, X_te = X.iloc[:split], X.iloc[split:]
    y_tr, y_te = y.iloc[:split], y.iloc[split:]

    classes = np.unique(y_tr)
    # Защита: если в обучающей выборке нет всех трёх классов — пропускаем
    missing_in_train = {'BUY', 'SELL', 'HOLD'} - set(classes)
    if missing_in_train:
        log.error(f"  ❌ Train-выборка не содержит классы {missing_in_train} "
                  f"(возможно, они попали только в test). Переобучение невозможно.")
        return None
    weights = compute_class_weight('balanced', classes=classes, y=y_tr)
    cw      = dict(zip(classes, weights))
    log.info(f"  Class weights: { {k: round(v,2) for k,v in cw.items()} }")

    results = {}

    # RandomForest
    log.info("  Обучаем RandomForest...")
    rf = RandomForestClassifier(n_estimators=400, max_depth=10, min_samples_leaf=3,
                                max_features='sqrt', class_weight='balanced',
                                random_state=42, n_jobs=-1)
    rf.fit(X_tr, y_tr)
    p_rf  = rf.predict(X_te)
    f1_rf = f1_score(y_te, p_rf, average='macro', zero_division=0)
    log.info(f"  RF  macro-F1 = {f1_rf:.4f}")
    log.info("\n" + classification_report(y_te, p_rf, zero_division=0))
    results['rf'] = (rf, f1_rf)

    # Feature importance
    fi = sorted(zip(feature_names, rf.feature_importances_), key=lambda x: x[1], reverse=True)[:8]
    log.info(f"  Top фичи RF: {[(n, round(v,3)) for n,v in fi]}")

    # XGBoost
    if HAS_XGB:
        log.info("  Обучаем XGBoost...")
        le   = LabelEncoder()
        y_tr_enc = le.fit_transform(y_tr)
        sw   = np.array([cw.get(c, 1.0) for c in y_tr])
        xgbm = xgb.XGBClassifier(n_estimators=400, max_depth=7, learning_rate=0.05,
                                   subsample=0.8, colsample_bytree=0.8, min_child_weight=3,
                                   eval_metric='mlogloss', random_state=42, n_jobs=-1)
        xgbm.fit(X_tr, y_tr_enc, sample_weight=sw, verbose=False)
        p_xgb  = le.inverse_transform(xgbm.predict(X_te))
        f1_xgb = f1_score(y_te, p_xgb, average='macro', zero_division=0)
        log.info(f"  XGB macro-F1 = {f1_xgb:.4f}")
        log.info("\n" + classification_report(y_te, p_xgb, zero_division=0))
        results['xgb']    = (xgbm, f1_xgb)
        results['xgb_le'] = le

    best_name  = max([(k, v[1]) for k, v in results.items() if k != 'xgb_le'], key=lambda x: x[1])[0]
    best_model = results[best_name][0]
    best_f1    = results[best_name][1]
    log.info(f"  ✅ Победитель: {best_name.upper()} (F1={best_f1:.4f})")

    meta = {
        'pair': pair, 'model_type': best_name, 'f1_macro': best_f1,
        'features': feature_names, 'trained_at': datetime.now().isoformat(),
        'train_rows': len(X_tr), 'test_rows': len(X_te),
        'label_counts': {k: int(v) for k, v in cnt.items()},
        'tp_pct': TP_PCT, 'sl_pct': SL_PCT, 'lookahead': LOOKAHEAD,
        'hold_ratio': HOLD_RATIO, 'version': 3,
        'btc_features': pair != 'BTCUSDT' and df_btc is not None,
    }
    payload = {'model': best_model, 'meta': meta, 'label_encoder': results.get('xgb_le')}

    with open(MODELS_DIR / f"{pair}_model.pkl", 'wb') as f:
        pickle.dump(payload, f)
    with open(MODELS_DIR / f"{pair}_meta.json", 'w') as f:
        json.dump(meta, f, indent=2, ensure_ascii=False)
    log.info(f"  💾 Сохранено: {MODELS_DIR / f'{pair}_model.pkl'}")

    return meta


def get_pairs() -> list:
    info_path = BASE_DIR / "historical_data" / "instruments_info.json"
    if info_path.exists():
        with open(info_path) as f:
            return list(json.load(f).keys())
    # Собираем уникальные пары из любых CSV-файлов в DATA_DIR
    seen, pairs = set(), []
    for p in sorted(DATA_DIR.glob("*.csv")):
        # Убираем суффикс вида _30d / _365d / _180d
        name = p.stem   # BTCUSDT_365d
        for sep in ('_30d', '_90d', '_180d', '_365d', '_'):
            if name.endswith(sep.rstrip('_')) or sep in name:
                name = name.split('_')[0]
                break
        if name not in seen and name.endswith('USDT'):
            seen.add(name)
            pairs.append(name)
    return pairs


if __name__ == "__main__":
    log.info("🚀 ML train.py v3  |  BTC leading indicators + RSI + BB + ATR + MACD")
    log.info(f"   Данные : {DATA_DIR}")
    src_label = "Old (длинная история)" if DATA_DIR == _OLD_DIR else "historical_data (текущий кэш)"
    log.info(f"   Источник: {src_label}")
    log.info(f"   Фичей  : {len(FEATURES)} базовых + {len(BTC_FEATURES)} BTC (для альтов)")

    pairs = get_pairs()
    log.info(f"   Пар    : {len(pairs)}\n")

    # Загружаем BTC один раз — будет использован как leading indicator для всех альтов
    df_btc = load_candles('BTCUSDT')
    if df_btc is not None:
        df_btc = compute_indicators(df_btc)
        log.info(f"✅ BTC загружен: {len(df_btc):,} свечей — будет leading indicator для {len(pairs)-1} пар\n")
    else:
        log.warning("⚠️  BTC данные не найдены — обучение без BTC-фичей\n")

    summary = []
    for pair in pairs:
        meta = train_pair(pair, df_btc)
        if meta:
            summary.append(meta)

    v2 = {'BTCUSDT': 0.328, 'ETHUSDT': 0.336, 'SOLUSDT': 0.318,
          'BNBUSDT': 0.303, 'AVAXUSDT': 0.321, 'APTUSDT': 0.402,
          'WUSDT': 0.354,  'OPUSDT': 0.358,  'TIAUSDT': 0.356,
          'ATOMUSDT': 0.377, 'WIFUSDT': 0.387, 'ARBUSDT': 0.404,
          'XAUTUSDT': 0.333}

    log.info(f"\n{'='*60}")
    log.info("📊 ИТОГИ  (v2 → v3 с BTC leading indicators)")
    log.info(f"{'='*60}")
    for m in summary:
        old   = v2.get(m['pair'], 0)
        diff  = m['f1_macro'] - old
        arrow = f"▲+{diff:.3f}" if diff >= 0 else f"▼{diff:.3f}"
        btc_mark = " [+BTC]" if m.get('btc_features') else ""
        log.info(f"  {m['pair']:12s}  {m['model_type'].upper():3s}  "
                 f"F1={m['f1_macro']:.3f}  {arrow}{btc_mark}  "
                 f"BUY={m['label_counts'].get('BUY',0)}  "
                 f"SELL={m['label_counts'].get('SELL',0)}")

    f1s = [m['f1_macro'] for m in summary]
    if f1s:
        log.info(f"\n  Средний F1: {sum(f1s)/len(f1s):.3f}  (v2 было: 0.352)")

    with open(MODELS_DIR / "training_summary.json", 'w') as f:
        json.dump(summary, f, indent=2, ensure_ascii=False)
    log.info("✅ Готово!")
