from __future__ import annotations
"""
ml/ml_strategy_engine.py
========================
ML-движок сигналов — замена strategy_engine.py.

Использование в main.py:
    # Было:
    from strategy_engine import StrategyEngine
    # Стало:
    from ml.ml_strategy_engine import MLStrategyEngine as StrategyEngine
"""

import pickle
import logging
import numpy as np
import pandas as pd
from pathlib import Path
from datetime import datetime
import sys as _sys, os as _os
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
from indicator_logger import log_indicators_from_dict

log = logging.getLogger(__name__)

MODELS_DIR = Path(__file__).parent / "models"

# Фичи — должны совпадать с train.py
BASE_FEATURES = [
    'dev_50', 'dev_75', 'dev_100', 'dev_150', 'dev_200',
    'avg_deviation', 'confidence',
    'price_change_1m', 'price_change_5m', 'price_change_15m',
    'volume_last', 'buy_votes', 'sell_votes',
    'dev_spread', 'dev_momentum', 'vol_ratio',
]


class MLStrategyEngine:
    """
    Генератор торговых сигналов на основе обученных ML-моделей.
    Совместим по интерфейсу с оригинальным StrategyEngine.
    """

    def __init__(self):
        self.models:  dict = {}   # pair → payload
        self.signals: dict = {}   # pair → последний сигнал
        self.btc_df:  pd.DataFrame | None = None   # обновляется ботом каждые 5 мин
        self._load_models()

    # ── Загрузка моделей ──────────────────────────────────────────────────────

    def _load_models(self):
        if not MODELS_DIR.exists():
            log.warning(f"Папка моделей не найдена: {MODELS_DIR}. Запусти python ml/train.py")
            return

        for pkl in MODELS_DIR.glob("*_model.pkl"):
            pair = pkl.stem.replace('_model', '')
            try:
                with open(pkl, 'rb') as f:
                    self.models[pair] = pickle.load(f)
                payload = self.models[pair]
                model   = payload['model']

                # Проверяем наличие HOLD в classes_ — только если нет label_encoder
                if hasattr(model, 'classes_') and payload.get('label_encoder') is None:
                    current_classes = [str(c) for c in model.classes_]
                    if 'HOLD' not in current_classes and not all(c.isdigit() for c in current_classes):
                        log.warning(
                            f"⚠️  {pair}: модель обучена без HOLD "
                            f"(классы: {current_classes}). "
                            f"Переобучи: python ml/train.py"
                        )

                log.info(f"✅ Загружена модель: {pair}  "
                         f"({payload['meta']['model_type'].upper()}  "
                         f"F1={payload['meta']['f1_macro']:.3f})")
            except Exception as e:
                log.error(f"❌ Ошибка загрузки {pkl.name}: {e}")

        if not self.models:
            log.warning("⚠️  Нет обученных моделей. Запусти python ml/train.py")

    # ── Подготовка фичей ─────────────────────────────────────────────────────

    def update_btc(self, df_btc: pd.DataFrame):
        """Вызывается ботом при каждом обновлении данных BTC (~каждые 5 мин)."""
        self.btc_df = df_btc

    @staticmethod
    def _build_btc_features(df_btc: pd.DataFrame) -> dict:
        """
        Вычисляет BTC leading indicators из последних свечей.
        Возвращает словарь с BTC_FEATURES или нули если данных нет.
        """
        empty = {
            'btc_change_5m': 0.0, 'btc_change_15m': 0.0, 'btc_change_60m': 0.0,
            'btc_rsi_14': 50.0,   'btc_dev_50': 0.0,
            'btc_atr_pct': 0.0,   'pair_vs_btc_15m': 0.0,
        }
        if df_btc is None or len(df_btc) < 60:
            return empty

        try:
            closes = df_btc['close'].values.astype(float)
            n = len(closes)

            def pct(i):
                if n > i and closes[-i] != 0:
                    return (closes[-1] - closes[-i]) / closes[-i] * 100
                return 0.0

            # RSI-14
            rsi = MLStrategyEngine._compute_rsi(closes, 14)

            # SMA-50 отклонение
            sma50 = np.mean(closes[-50:]) if n >= 50 else np.mean(closes)
            dev50 = (closes[-1] - sma50) / (sma50 + 1e-9) * 100

            # ATR упрощённый (только close-to-close)
            diffs  = np.abs(np.diff(closes[-15:]))
            atr14  = float(np.mean(diffs)) if len(diffs) > 0 else 0.0
            atr_pct = atr14 / (closes[-1] + 1e-9) * 100

            return {
                'btc_change_5m':  pct(5),
                'btc_change_15m': pct(15),
                'btc_change_60m': pct(60),
                'btc_rsi_14':     rsi,
                'btc_dev_50':     dev50,
                'btc_atr_pct':    atr_pct,
                'pair_vs_btc_15m': 0.0,   # заполняется ниже в analyze_pair
            }
        except Exception:
            return empty

    @staticmethod
    def _compute_rsi(closes: np.ndarray, period: int) -> float:
        """RSI по последним closes."""
        if len(closes) < period + 1:
            return 50.0
        delta = np.diff(closes[-(period + 1):])
        gain  = np.where(delta > 0, delta, 0.0).mean()
        loss  = np.where(delta < 0, -delta, 0.0).mean()
        if loss < 1e-9:
            return 100.0
        return 100.0 - 100.0 / (1.0 + gain / loss)

    @staticmethod
    def _build_features(df: pd.DataFrame, current_price: float) -> pd.DataFrame | None:
        """
        Строим вектор фичей из последних свечей df и текущей цены.
        df — минутные OHLCV свечи (минимум 200 строк).
        Возвращает pd.DataFrame с именами колонок (устраняет UserWarning sklearn).
        """
        if len(df) < 200:
            return None

        closes  = df['close'].values.astype(float)
        highs   = df['high'].values.astype(float)   if 'high'   in df.columns else closes
        lows    = df['low'].values.astype(float)     if 'low'    in df.columns else closes
        volumes = df['volume'].values.astype(float)  if 'volume' in df.columns else np.zeros(len(closes))

        # ── SMA-отклонения ──────────────────────────────────────────────────
        windows = {'50': 50, '75': 75, '100': 100, '150': 150, '200': 200}
        devs = {}
        for name, w in windows.items():
            sma = np.mean(closes[-w:])
            devs[f'dev_{name}'] = (current_price - sma) / sma * 100

        avg_dev    = float(np.mean(list(devs.values())))
        threshold  = 2.0
        buy_votes  = sum(1 for v in devs.values() if v < -threshold)
        sell_votes = sum(1 for v in devs.values() if v >  threshold)
        confidence = min(abs(avg_dev) / threshold, 1.0)

        # ── Динамика цены ───────────────────────────────────────────────────
        def pct(n):
            if len(closes) > n and closes[-n] != 0:
                return (current_price - closes[-n]) / closes[-n] * 100
            return 0.0

        # ── Объём ───────────────────────────────────────────────────────────
        vol_last = float(volumes[-1])
        vol_mean = float(np.mean(volumes[-20:])) if len(volumes) >= 20 else (float(np.mean(volumes)) or 1.0)
        vol_roll5 = float(np.mean(volumes[-5:])) if len(volumes) >= 5 else vol_last

        # ── RSI ─────────────────────────────────────────────────────────────
        rsi14 = MLStrategyEngine._compute_rsi(closes, 14)
        rsi7  = MLStrategyEngine._compute_rsi(closes, 7)
        # RSI-14 изменение за 3 свечи
        if len(closes) >= 18:
            rsi14_3 = MLStrategyEngine._compute_rsi(closes[:-3], 14)
            rsi14_change = rsi14 - rsi14_3
        else:
            rsi14_change = 0.0

        # ── Bollinger Bands (20) ─────────────────────────────────────────────
        bb_n = min(20, len(closes))
        bb_mid = float(np.mean(closes[-bb_n:]))
        bb_std = float(np.std(closes[-bb_n:]))
        bb_up  = bb_mid + 2 * bb_std
        bb_low = bb_mid - 2 * bb_std
        bb_wid = (bb_up - bb_low) / (bb_mid + 1e-9) * 100
        bb_pos = (current_price - bb_low) / (bb_up - bb_low + 1e-9)
        # squeeze: текущая ширина < ширина 5 свечей назад
        if len(closes) >= 25:
            bb_mid5 = float(np.mean(closes[-25:-5]))
            bb_std5 = float(np.std(closes[-25:-5]))
            bb_wid5 = (bb_mid5 + 2*bb_std5 - (bb_mid5 - 2*bb_std5)) / (bb_mid5 + 1e-9) * 100
            bb_squeeze = 1.0 if bb_wid < bb_wid5 else 0.0
        else:
            bb_squeeze = 0.0

        # ── ATR (14) ─────────────────────────────────────────────────────────
        n_atr = min(15, len(closes))
        tr_arr = []
        for i in range(1, n_atr):
            idx = -(n_atr - i)
            h = float(highs[idx])
            l = float(lows[idx])
            c_prev = float(closes[idx - 1])
            tr_arr.append(max(h - l, abs(h - c_prev), abs(l - c_prev)))
        atr14 = float(np.mean(tr_arr)) if tr_arr else 0.0

        # ATR-50 для ratio
        n_atr50 = min(51, len(closes))
        tr50 = []
        for i in range(1, n_atr50):
            idx = -(n_atr50 - i)
            h = float(highs[idx])
            l = float(lows[idx])
            c_prev = float(closes[idx - 1])
            tr50.append(max(h - l, abs(h - c_prev), abs(l - c_prev)))
        atr50 = float(np.mean(tr50)) if tr50 else (atr14 or 1e-9)

        atr_pct   = atr14 / (current_price + 1e-9) * 100
        atr_ratio = atr14 / (atr50 + 1e-9)

        # ── MACD (12/26/9) ───────────────────────────────────────────────────
        s = pd.Series(closes)
        ema12 = s.ewm(span=12, adjust=False).mean().iloc[-1]
        ema26 = s.ewm(span=26, adjust=False).mean().iloc[-1]
        macd_line_now = ema12 - ema26
        # signal line нужна история — считаем через pandas
        macd_series  = s.ewm(span=12, adjust=False).mean() - s.ewm(span=26, adjust=False).mean()
        macd_sig_ser = macd_series.ewm(span=9, adjust=False).mean()
        macd_hist_now  = float(macd_series.iloc[-1] - macd_sig_ser.iloc[-1])
        macd_hist_prev = float(macd_series.iloc[-2] - macd_sig_ser.iloc[-2]) if len(closes) > 2 else 0.0
        macd_hist_norm = macd_hist_now / (atr14 + 1e-9)
        macd_cross = 0.0
        if macd_hist_now > 0 and macd_hist_prev <= 0:
            macd_cross = 1.0
        elif macd_hist_now < 0 and macd_hist_prev >= 0:
            macd_cross = -1.0

        # ── Сборка словаря фичей ─────────────────────────────────────────────
        feat = {
            **devs,
            'avg_deviation':    avg_dev,
            'confidence':       confidence,
            'buy_votes':        float(buy_votes),
            'sell_votes':       float(sell_votes),
            'dev_spread':       devs['dev_50'] - devs['dev_200'],
            'dev_momentum':     devs['dev_50'] - devs['dev_100'],
            'price_change_1m':  pct(1),
            'price_change_5m':  pct(5),
            'price_change_15m': pct(15),
            'price_change_30m': pct(30),
            'price_change_60m': pct(60),
            'volume_last':      vol_last,
            'vol_ratio':        vol_last / (vol_mean + 1e-9),
            'vol_change':       vol_roll5 / (vol_mean + 1e-9),
            'rsi_14':           rsi14,
            'rsi_7':            rsi7,
            'rsi_14_change':    rsi14_change,
            'bb_position':      bb_pos,
            'bb_width':         bb_wid,
            'bb_squeeze':       bb_squeeze,
            'atr_pct':          atr_pct,
            'atr_ratio':        atr_ratio,
            'macd_hist':        macd_hist_now,   # абсолютное
            'macd_hist_norm':   macd_hist_norm,
            'macd_cross':       macd_cross,
            'atr_14':           atr14,
            'bb_mid':           bb_mid,
            'bb_upper':         bb_up,
            'bb_lower':         bb_low,
        }

        # ── Расширенные фичи train_v4.py (29 дополнительных) ─────────────────
        n = len(closes)
        s_close = pd.Series(closes)

        # Percentile Rank
        feat['pct_rank_200'] = float(s_close.tail(200).rank(pct=True).iloc[-1]) if n >= 200 else 0.5
        feat['pct_rank_500'] = float(s_close.tail(500).rank(pct=True).iloc[-1]) if n >= 500 else feat['pct_rank_200']

        # Z-score
        for w, key in [(200, 'z_score_200'), (500, 'z_score_500')]:
            if n >= w:
                sl = closes[-w:]
                m, std = np.mean(sl), np.std(sl)
                feat[key] = float(np.clip((current_price - m) / (std + 1e-9), -4, 4))
            else:
                feat[key] = 0.0

        # Donchian position + width
        for w, k_pos, k_wid in [(100,'don_pos_100','don_width_100'), (200,'don_pos_200',None)]:
            if n >= w:
                dh = float(np.max(highs[-w:]))
                dl = float(np.min(lows[-w:]))
                rng = dh - dl + 1e-9
                feat[k_pos] = float(np.clip((current_price - dl) / rng, 0, 1))
                if k_wid:
                    feat[k_wid] = rng / (((dh + dl) / 2) + 1e-9) * 100
            else:
                feat[k_pos] = 0.5
                if k_wid:
                    feat[k_wid] = 0.0

        # Pivot Points (дневные: последние 1440 баров)
        pw = min(1440, n)
        p_hi = float(np.max(highs[-pw:]))
        p_lo = float(np.min(lows[-pw:]))
        p_cl = float(np.mean(closes[-pw:]))
        pivot = (p_hi + p_lo + p_cl) / 3
        r1    = 2 * pivot - p_lo
        s1    = 2 * pivot - p_hi
        feat['dist_to_r1']  = float((r1 - current_price) / (current_price + 1e-9) * 100)
        feat['dist_to_s1']  = float((current_price - s1) / (current_price + 1e-9) * 100)
        feat['above_pivot'] = 1.0 if current_price > pivot else 0.0
        feat['pivot_zone']  = (1 if feat['dist_to_r1'] < 0.5 else
                               -1 if feat['dist_to_s1'] < 0.5 else 0)

        # ADX-14 + DI+/DI-
        if n >= 15:
            try:
                up   = np.diff(highs[-15:]).clip(min=0)
                down = (-np.diff(lows[-15:])).clip(min=0)
                tr   = np.array([max(highs[-14+i]-lows[-14+i],
                                     abs(highs[-14+i]-closes[-15+i]),
                                     abs(lows[-14+i]-closes[-15+i]))
                                 for i in range(14)])
                atr_d = float(np.mean(tr)) + 1e-9
                dmp   = float(np.mean(np.where(up > down, up, 0)))
                dmm   = float(np.mean(np.where(down > up, down, 0)))
                dip   = 100 * dmp / atr_d
                dim   = 100 * dmm / atr_d
                dx    = 100 * abs(dip - dim) / (dip + dim + 1e-9)
                feat['adx_14']    = float(dx)
                feat['adx_trend'] = 1.0 if dx > 25 and dip > dim else (-1.0 if dx > 25 else 0.0)
                feat['di_plus']   = float(min(dip, 100))
                feat['di_minus']  = float(min(dim, 100))
            except Exception:
                feat['adx_14'] = feat['adx_trend'] = feat['di_plus'] = feat['di_minus'] = 0.0
        else:
            feat['adx_14'] = feat['adx_trend'] = feat['di_plus'] = feat['di_minus'] = 0.0

        # 4H тренд (240 баров ≈ 4 часа на 1м свечах)
        if n >= 240:
            sma4h = float(np.mean(closes[-240:]))
            feat['trend_4h'] = 1.0 if current_price > sma4h else -1.0
            feat['dev_4h']   = (current_price - sma4h) / (sma4h + 1e-9) * 100
        else:
            feat['trend_4h'] = 0.0
            feat['dev_4h']   = 0.0

        # OBV trend + slope
        if n >= 50:
            direction = np.sign(np.diff(closes[-51:]))
            obv_vals  = np.cumsum(volumes[-50:] * direction)
            obv_slope = float(obv_vals[-1] - obv_vals[-10]) if len(obv_vals) >= 10 else 0.0
            feat['obv_trend'] = 1.0 if obv_slope > 0 else -1.0
            # Нормализуем slope
            vol_scale = float(np.mean(np.abs(volumes[-50:]))) + 1e-9
            feat['obv_slope'] = float(np.clip(obv_slope / vol_scale / 10, -5, 5))
        else:
            feat['obv_trend'] = 0.0
            feat['obv_slope'] = 0.0

        # Swing High/Low (последние 50 баров)
        if n >= 50:
            recent_hi = float(np.max(highs[-50:]))
            recent_lo = float(np.min(lows[-50:]))
            feat['swing_hi_dist'] = float((recent_hi - current_price) / (current_price + 1e-9) * 100)
            feat['swing_lo_dist'] = float((current_price - recent_lo) / (current_price + 1e-9) * 100)
        else:
            feat['swing_hi_dist'] = 0.0
            feat['swing_lo_dist'] = 0.0

        # Market structure (HH/LL)
        if n >= 20:
            h20 = highs[-20:]
            l20 = lows[-20:]
            hh = float(h20[-1]) > float(np.max(h20[:-5]))
            hl = float(l20[-1]) > float(np.min(l20[:-5]))
            ll = float(l20[-1]) < float(np.min(l20[:-5]))
            lh = float(h20[-1]) < float(np.max(h20[:-5]))
            feat['market_structure'] = (1.0 if hh and hl else -1.0 if ll and lh else 0.0)
        else:
            feat['market_structure'] = 0.0

        return feat   # dict — BTC-фичи добавляются в analyze_pair

    # ── Основной метод анализа ────────────────────────────────────────────────

    @staticmethod
    def _trend_filter(df: pd.DataFrame, signal: str) -> tuple[bool, str]:
        """
        Фильтр трендового контекста — защищает от входа в середину движения.

        Правила для BUY:
          • Не входить если последние RED_STREAK свечей все красные (нисходящий импульс)
          • Не входить если цена потеряла > MAX_DROP_PCT% за последние MOMENTUM_BARS свечей

        Правила для SELL:
          • Симметрично: не входить в середину зелёного импульса

        Возвращает (pass: bool, reason: str)
        """
        RED_STREAK     = 5      # свечей подряд красных → блок BUY
        GREEN_STREAK   = 5      # свечей подряд зелёных → блок SELL
        MAX_DROP_PCT   = 3.0    # % падения за MOMENTUM_BARS → блок BUY
        MAX_PUMP_PCT   = 3.0    # % роста за MOMENTUM_BARS → блок SELL
        MOMENTUM_BARS  = 15     # окно для проверки impulse

        if df is None or len(df) < RED_STREAK + 2:
            return True, ''

        closes = df['close'].values.astype(float)
        opens  = df['open'].values.astype(float) if 'open' in df.columns \
                 else closes  # fallback если нет open

        n = len(closes)

        if signal == 'BUY':
            # 1. N красных свечей подряд
            last_n_red = all(
                closes[n - 1 - i] < opens[n - 1 - i]
                for i in range(RED_STREAK)
            )
            if last_n_red:
                return False, f'{RED_STREAK} красных свечей подряд — импульс вниз'

            # 2. Резкое падение за последние MOMENTUM_BARS свечей
            if n >= MOMENTUM_BARS:
                price_start = closes[n - MOMENTUM_BARS]
                price_now   = closes[-1]
                if price_start > 0:
                    drop_pct = (price_start - price_now) / price_start * 100
                    if drop_pct >= MAX_DROP_PCT:
                        return False, f'падение {drop_pct:.1f}% за {MOMENTUM_BARS} свечей'

        elif signal == 'SELL':
            # 1. N зелёных свечей подряд
            last_n_green = all(
                closes[n - 1 - i] > opens[n - 1 - i]
                for i in range(GREEN_STREAK)
            )
            if last_n_green:
                return False, f'{GREEN_STREAK} зелёных свечей подряд — импульс вверх'

            # 2. Резкий рост
            if n >= MOMENTUM_BARS:
                price_start = closes[n - MOMENTUM_BARS]
                price_now   = closes[-1]
                if price_start > 0:
                    pump_pct = (price_now - price_start) / price_start * 100
                    if pump_pct >= MAX_PUMP_PCT:
                        return False, f'рост {pump_pct:.1f}% за {MOMENTUM_BARS} свечей'

        return True, ''

    def analyze_pair(self, symbol: str, df: pd.DataFrame, current_price: float) -> dict:
        """
        Интерфейс совместим с оригинальным StrategyEngine.analyze_pair().
        Возвращает dict с ключами: signal, confidence, deviation, ...
        """
        fallback = {
            'signal': 'HOLD', 'confidence': 0.0, 'deviation': 0.0,
            'buy_votes': 0, 'sell_votes': 0, 'total_votes': 0,
            'timestamp': datetime.now().isoformat(), 'source': 'ml',
        }

        if symbol not in self.models:
            # Нет модели → HOLD (или можно запустить rule-based fallback)
            log.debug(f"Нет модели для {symbol}, возвращаем HOLD")
            return fallback

        feat_dict = self._build_features(df, current_price)
        if feat_dict is None:
            return fallback

        # BTC leading indicators — добавляем если модель обучена с ними
        payload       = self.models[symbol]
        feature_names = payload['meta']['features']
        uses_btc      = payload['meta'].get('btc_features', False)

        if uses_btc and symbol != 'BTCUSDT':
            btc_feats = self._build_btc_features(self.btc_df)
            # pair_vs_btc_15m = изменение пары за 15м - изменение BTC за 15м
            pair_change_15m = feat_dict.get('price_change_15m', 0.0)
            btc_feats['pair_vs_btc_15m'] = pair_change_15m - btc_feats.get('btc_change_15m', 0.0)
            feat_dict.update(btc_feats)

        feat = pd.DataFrame([feat_dict])

        model         = payload['model']
        label_encoder = payload.get('label_encoder')

        # Выравниваем колонки по обученному набору, передаём DataFrame — без UserWarning
        X = feat.reindex(columns=feature_names).fillna(0.0)

        try:
            proba = model.predict_proba(X)[0]
            best  = int(np.argmax(proba))
            conf  = float(proba[best])
            raw   = model.classes_[best]

            # Определяем формат меток по реальному типу classes_,
            # а не по наличию label_encoder (он может быть в payload но не нужен)
            raw_str = str(raw)
            if raw_str in ('BUY', 'SELL', 'HOLD'):
                # Модель обучена напрямую со строковыми метками
                signal = raw_str
            elif label_encoder is not None and raw_str.lstrip('-').isdigit():
                # Модель обучена с числовыми метками → декодируем
                signal = str(label_encoder.inverse_transform([int(raw)])[0])
            else:
                # Неизвестный формат — fallback HOLD
                log.warning(f"{symbol}: неизвестная метка '{raw_str}', возвращаем HOLD")
                signal = 'HOLD'
        except Exception as e:
            log.error(f"Ошибка предсказания {symbol}: {e}")
            return fallback

        # Читаем фичи из DataFrame
        row        = feat.iloc[0]
        avg_dev    = float(row.get('avg_deviation', 0))
        buy_votes  = int(row.get('buy_votes', 0))
        sell_votes = int(row.get('sell_votes', 0))

        # ── Трендовый фильтр ──────────────────────────────────────────────────
        # Блокируем вход в середину сильного движения против позиции
        if signal in ('BUY', 'SELL'):
            ok, reason = self._trend_filter(df, signal)
            if not ok:
                log.info(
                    f"⛔ ФИЛЬТР {signal} {symbol} — {reason}  "
                    f"(модель conf={conf:.2f}, заблокировано)"
                )
                signal = 'HOLD'
                # Понижаем уверенность чтобы не засорять лог сигналов
                conf   = min(conf, 0.49)

        result = {
            'signal':      signal,
            'confidence':  conf,
            'deviation':   avg_dev,
            'buy_votes':   buy_votes,
            'sell_votes':  sell_votes,
            'total_votes': buy_votes + sell_votes,
            'timestamp':   datetime.now().isoformat(),
            'source':      f"ml_{payload['meta']['model_type']}",
            'f1_model':    payload['meta']['f1_macro'],
            # Доп. фичи для signal_logger (теперь реально вычислены)
            'dev_spread':     float(row.get('dev_spread', 0)),
            'dev_momentum':   float(row.get('dev_momentum', 0)),
            'rsi_14':         float(row.get('rsi_14', 0)),
            'bb_position':    float(row.get('bb_position', 0)),
            'bb_width':       float(row.get('bb_width', 0)),
            'atr_pct':        float(row.get('atr_pct', 0)),
            'macd_hist_norm': float(row.get('macd_hist_norm', 0)),
        }


        # ── Логирование индикаторов каждый тик ───────────────────────────
        try:
            _last = df.iloc[-1]
            _cnd  = {
                'open':   float(getattr(_last, 'open',   current_price)),
                'high':   float(getattr(_last, 'high',   current_price)),
                'low':    float(getattr(_last, 'low',    current_price)),
                'close':  float(current_price),
                'volume': float(getattr(_last, 'volume', 0)),
            }
            _fd = feat.iloc[0].to_dict() if hasattr(feat, 'iloc') else feat_dict
            log_indicators_from_dict(
                symbol=symbol, feat=_fd, candle=_cnd,
                signal=signal, confidence=conf,
                source=result.get('source', 'ml'),
            )
        except Exception:
            pass
        # ─────────────────────────────────────────────────────────────────
        self.signals[symbol] = result

        if signal != 'HOLD':
            log.info(
                f"🔔 ML СИГНАЛ {signal} | {symbol} | "
                f"conf={conf:.2f} | dev={avg_dev:+.2f}% | "
                f"model={payload['meta']['model_type'].upper()} F1={payload['meta']['f1_macro']:.3f}"
            )

        return result

    # ── Вспомогательные методы (совместимость) ────────────────────────────────

    def get_cached_signals(self) -> dict:
        return self.signals

    def get_model_info(self) -> dict:
        """Информация о загруженных моделях (для дашборда)."""
        return {
            pair: {
                'model_type': p['meta']['model_type'],
                'f1_macro':   p['meta']['f1_macro'],
                'trained_at': p['meta']['trained_at'],
                'train_rows': p['meta']['train_rows'],
            }
            for pair, p in self.models.items()
        }

    def reload_models(self):
        """Перезагрузка моделей без перезапуска бота."""
        self.models = {}
        self._load_models()
        log.info(f"🔄 Модели перезагружены: {list(self.models.keys())}")
