# strategy_engine.py
"""
Ядро стратегии — анализ и генерация сигналов
"""
import pandas as pd
import numpy as np
from datetime import datetime
from config import OPTIMAL_PARAMS, TECH_PARAMS
from signal_cache import SignalCache
from strategy_logger import log_signal_tick
from indicator_logger import log_indicators


class StrategyEngine:
    """Генератор торговых сигналов на основе SMA-отклонений"""

    def __init__(self):
        self.params  = OPTIMAL_PARAMS
        self.windows = TECH_PARAMS['sma_windows']
        self.cache   = SignalCache()

        self.signals = self.cache.get_all_signals()
        if self.signals:
            print(f"📦 Загружено {len(self.signals)} сигналов из кэша")

    def analyze_pair(self, symbol: str, df: pd.DataFrame, current_price: float) -> dict:
        if len(df) < 200:
            return {'signal': 'HOLD', 'confidence': 0, 'deviation': 0,
                    'buy_votes': 0, 'sell_votes': 0, 'total_votes': 0,
                    'dev_values': {}, 'sma_values': {},
                    'timestamp': datetime.now().isoformat()}

        prices = df['close'].values

        # window_map: читаемое_имя → кол-во_баров → ключ для логов
        window_map = {
            '5d':   (200, '200'),
            '3.3d': (150, '150'),
            '2.5d': (100, '100'),
            '1.7d': (75,  '75'),
            '1.1d': (50,  '50'),
        }

        buy_votes   = 0
        sell_votes  = 0
        total_votes = 0
        deviations  = []

        sma_values = {}   # ключ '50', '75', ...
        dev_values = {}   # ключ '50', '75', ...

        for period_name, (window, key) in window_map.items():
            if len(prices) > window:
                sma       = float(np.mean(prices[-window:]))
                deviation = ((current_price - sma) / sma) * 100

                sma_values[key] = sma
                dev_values[key] = deviation
                deviations.append(deviation)

                if deviation < -self.params['entry_threshold']:
                    buy_votes  += 1
                    total_votes += 1
                elif deviation > self.params['entry_threshold']:
                    sell_votes  += 1
                    total_votes += 1

        avg_deviation = float(np.mean(deviations)) if deviations else 0.0
        confidence    = min(abs(avg_deviation) / self.params['entry_threshold'], 1.0)

        signal = 'HOLD'
        if total_votes >= 3:
            if buy_votes  >= 3:
                signal = 'BUY'
            elif sell_votes >= 3:
                signal = 'SELL'

        # ── Контекстный фильтр (momentum + RSI) ──────────────────────────────
        # Защита от входа в начале нисходящего движения.
        # Проблема без фильтра:
        #   Цена падает на 2.1% от пика → SMA ещё не успел опуститься →
        #   dev_50 = -2.1% → buy_vote → BUY на пике (catching falling knife).
        if signal == 'BUY':
            _filtered, _filter_reason = self._momentum_filter(df, prices)
            if _filtered:
                signal = 'HOLD'
                if False:  # включи True чтобы видеть фильтрацию в консоли
                    print(f"⛔ ФИЛЬТР BUY {symbol}: {_filter_reason}")

        # ── Дополнительные рыночные метрики для логгера ──────────────────
        volume_last      = 0.0
        price_change_1m  = 0.0
        price_change_5m  = 0.0
        price_change_15m = 0.0
        try:
            if len(df) >= 2:
                volume_last     = float(df['volume'].iloc[-1])
                price_change_1m = ((current_price - float(df['close'].iloc[-2]))
                                   / float(df['close'].iloc[-2])) * 100
            if len(df) >= 6:
                price_change_5m = ((current_price - float(df['close'].iloc[-6]))
                                   / float(df['close'].iloc[-6])) * 100
            if len(df) >= 16:
                price_change_15m = ((current_price - float(df['close'].iloc[-16]))
                                    / float(df['close'].iloc[-16])) * 100
        except Exception:
            pass

        # ── Запись тика в лог ────────────────────────────────────────────
        log_signal_tick(
            symbol           = symbol,
            price            = current_price,
            sma_values       = sma_values,
            dev_values       = dev_values,
            avg_deviation    = avg_deviation,
            buy_votes        = buy_votes,
            sell_votes       = sell_votes,
            total_votes      = total_votes,
            signal           = signal,
            confidence       = confidence,
            volume_last      = volume_last,
            price_change_1m  = price_change_1m,
            price_change_5m  = price_change_5m,
            price_change_15m = price_change_15m,
            entry_threshold  = self.params['entry_threshold'],
        )

        # ── Дополнительные индикаторы (RSI, BB, ATR, MACD) для логгера ────────
        _ind = {}
        try:
            _cl  = df['close'].values.astype(float)
            _hi  = df['high'].values.astype(float)  if 'high'   in df.columns else _cl
            _lo  = df['low'].values.astype(float)   if 'low'    in df.columns else _cl
            _vol = df['volume'].values.astype(float) if 'volume' in df.columns else np.zeros(len(_cl))
            _n   = len(_cl)

            # RSI-14 и RSI-7
            def _rsi(arr, per):
                if len(arr) < per + 1: return 50.0
                d = np.diff(arr[-(per+1):].astype(float))
                g = np.where(d > 0, d, 0.0).mean()
                l = np.where(d < 0, -d, 0.0).mean()
                return 100.0 - 100.0 / (1.0 + g / (l + 1e-9)) if l > 1e-9 else 100.0

            _rsi14 = _rsi(_cl, 14)
            _rsi7  = _rsi(_cl, 7)
            _rsi14_prev = _rsi(_cl[:-3], 14) if _n >= 18 else _rsi14

            # Bollinger Bands (20)
            _bn   = min(20, _n)
            _bmid = float(np.mean(_cl[-_bn:]))
            _bstd = float(np.std(_cl[-_bn:]))
            _bup  = _bmid + 2 * _bstd
            _blo  = _bmid - 2 * _bstd
            _bwid = (_bup - _blo) / (_bmid + 1e-9) * 100
            _bpos = (current_price - _blo) / (_bup - _blo + 1e-9)
            _bsq  = 0.0
            if _n >= 25:
                _bm5 = float(np.mean(_cl[-25:-5]))
                _bs5 = float(np.std(_cl[-25:-5]))
                _bw5 = (_bm5 + 2*_bs5 - (_bm5 - 2*_bs5)) / (_bm5 + 1e-9) * 100
                _bsq = 1.0 if _bwid < _bw5 else 0.0

            # ATR-14
            _tr = []
            for _i in range(1, min(15, _n)):
                _idx = -min(15, _n) + _i
                _tr.append(max(float(_hi[_idx]) - float(_lo[_idx]),
                               abs(float(_hi[_idx]) - float(_cl[_idx-1])),
                               abs(float(_lo[_idx]) - float(_cl[_idx-1]))))
            _atr14 = float(np.mean(_tr)) if _tr else 0.0
            _tr50  = []
            for _i in range(1, min(51, _n)):
                _idx = -min(51, _n) + _i
                _tr50.append(max(float(_hi[_idx]) - float(_lo[_idx]),
                                 abs(float(_hi[_idx]) - float(_cl[_idx-1])),
                                 abs(float(_lo[_idx]) - float(_cl[_idx-1]))))
            _atr50 = float(np.mean(_tr50)) if _tr50 else (_atr14 or 1e-9)

            # MACD (12/26/9)
            import pandas as _pd
            _s      = _pd.Series(_cl)
            _macd_s = _s.ewm(span=12, adjust=False).mean() - _s.ewm(span=26, adjust=False).mean()
            _sig_s  = _macd_s.ewm(span=9, adjust=False).mean()
            _mhist  = float(_macd_s.iloc[-1] - _sig_s.iloc[-1])
            _mhprev = float(_macd_s.iloc[-2] - _sig_s.iloc[-2]) if _n > 2 else 0.0
            _mcross = (1.0 if _mhist > 0 and _mhprev <= 0 else
                      -1.0 if _mhist < 0 and _mhprev >= 0 else 0.0)

            # Volume
            _vmean = float(np.mean(_vol[-20:])) if _n >= 20 else (float(np.mean(_vol)) or 1.0)
            _vratio = float(_vol[-1]) / (_vmean + 1e-9) if _n > 0 else 1.0
            _vroll5 = float(np.mean(_vol[-5:])) if _n >= 5 else float(_vol[-1])
            _vchange = _vroll5 / (_vmean + 1e-9)

            # Price changes
            def _pct(k):
                return ((current_price - float(_cl[-k])) / float(_cl[-k]) * 100
                        if _n > k and float(_cl[-k]) != 0 else 0.0)

            _ind = dict(
                rsi_7=_rsi7, rsi_14=_rsi14, rsi_14_change=_rsi14-_rsi14_prev,
                bb_mid=_bmid, bb_upper=_bup, bb_lower=_blo,
                bb_width=_bwid, bb_position=_bpos, bb_squeeze=_bsq,
                atr_14=_atr14, atr_pct=_atr14/(current_price+1e-9)*100,
                atr_ratio=_atr14/(_atr50+1e-9),
                macd_hist=_mhist, macd_hist_norm=_mhist/(_atr14+1e-9),
                macd_cross=_mcross,
                vol_ratio=_vratio, vol_change=_vchange,
                price_change_1m=_pct(1), price_change_5m=_pct(5),
                price_change_15m=_pct(15), price_change_30m=_pct(30),
                price_change_60m=_pct(60),
            )

            # ── Расширенные индикаторы структуры рынка ────────────────────────
            try:
                _s = _pd.Series(_cl)

                # Percentile Rank
                _pr200 = float(_pd.Series(_cl[-200:]).rank(pct=True).iloc[-1]) if _n >= 50 else 0.5
                _pr500 = float(_pd.Series(_cl[-500:]).rank(pct=True).iloc[-1]) if _n >= 100 else 0.5

                # Z-score
                def _zsc(w):
                    arr = _cl[-w:] if _n >= w else _cl
                    return float((arr[-1] - arr.mean()) / (arr.std() + 1e-9))
                _z200 = _zsc(200); _z500 = _zsc(500)

                # Donchian Channel Position
                def _don_pos(w):
                    hi = float(np.max(_hi[-w:])) if _n >= w else float(np.max(_hi))
                    lo = float(np.min(_lo[-w:])) if _n >= w else float(np.min(_lo))
                    return (current_price - lo) / (hi - lo + 1e-9), hi, lo
                _dp100, _dhi100, _dlo100 = _don_pos(100)
                _dp200, _dhi200, _dlo200 = _don_pos(200)
                _dw100 = (_dhi100 - _dlo100) / (current_price + 1e-9) * 100
                _dw200 = (_dhi200 - _dlo200) / (current_price + 1e-9) * 100

                # Pivot Points (из последних 1440 баров = ~1 день)
                _day_bars = min(1440, _n)
                _day_hi  = float(np.max(_hi[-_day_bars:]))
                _day_lo  = float(np.min(_lo[-_day_bars:]))
                _day_cl  = float(_cl[-_day_bars])  # close начала дня
                _pvt = (_day_hi + _day_lo + _day_cl) / 3
                _r1  =  2 * _pvt - _day_lo
                _r2  =  _pvt + _day_hi - _day_lo
                _s1  =  2 * _pvt - _day_hi
                _s2  =  _pvt - (_day_hi - _day_lo)
                _dist_r1 = (_r1 - current_price) / (current_price + 1e-9) * 100
                _dist_s1 = (current_price - _s1) / (current_price + 1e-9) * 100
                _above_pv = 1.0 if current_price > _pvt else 0.0
                _pv_zone  = (2.0 if current_price > _r1 else
                             1.0 if current_price > _pvt else
                             0.0 if current_price > _s1 else -1.0)

                # ADX-14
                if _n >= 15:
                    try:
                        _tr_arr = np.array([
                            max(float(_hi[i])-float(_lo[i]),
                                abs(float(_hi[i])-float(_cl[i-1])),
                                abs(float(_lo[i])-float(_cl[i-1])))
                            for i in range(max(1, _n-14), _n)])
                        _up_arr  = np.diff(np.array([float(x) for x in _hi[-15:]]))
                        _dn_arr  = -np.diff(np.array([float(x) for x in _lo[-15:]]))
                        if len(_tr_arr) > 0 and len(_up_arr) > 0:
                            _dmp = np.where((_up_arr > _dn_arr) & (_up_arr > 0), _up_arr, 0).mean()
                            _dmm = np.where((_dn_arr > _up_arr) & (_dn_arr > 0), _dn_arr, 0).mean()
                            _atr_adx = float(_tr_arr.mean()) + 1e-9
                            _dip = 100 * _dmp / _atr_adx
                            _dim = 100 * _dmm / _atr_adx
                            _dx  = 100 * abs(_dip - _dim) / (_dip + _dim + 1e-9)
                            _adx_val = _dx
                            _adx_trend = (1.0 if _adx_val > 25 and _dip > _dim else
                                         -1.0 if _adx_val > 25 and _dim > _dip else 0.0)
                        else:
                            _adx_val = 20.0; _dip = 0.0; _dim = 0.0; _adx_trend = 0.0
                    except Exception:
                        _adx_val = 20.0; _dip = 0.0; _dim = 0.0; _adx_trend = 0.0
                else:
                    _adx_val = 20.0; _dip = 0.0; _dim = 0.0; _adx_trend = 0.0

                # 4H тренд (approx: SMA-240 vs SMA-480 на 1-минутных данных)
                _t4h = 0.0
                _dev4h = 0.0
                if _n >= 240:
                    _sma240 = float(np.mean(_cl[-240:]))
                    _sma480 = float(np.mean(_cl[-480:])) if _n >= 480 else _sma240
                    _t4h   = 1.0 if _sma240 > _sma480 else -1.0
                    _dev4h = (current_price - _sma240) / (_sma240 + 1e-9) * 100

                # OBV trend
                _dir_arr = np.sign(np.diff(_cl[-25:], prepend=_cl[-26] if _n>25 else _cl[-1]))
                _vol_arr = _vol[-25:] if _n >= 25 else _vol
                _obv_arr = np.cumsum(_dir_arr * _vol_arr[:len(_dir_arr)])
                _obv_tr  = (1.0 if len(_obv_arr) >= 5 and _obv_arr[-1] > _obv_arr[-5]
                            else -1.0 if len(_obv_arr) >= 5 else 0.0)
                _obv_sl  = (_obv_arr[-1] - _obv_arr[-6]) / (abs(_obv_arr).mean() + 1e-9) if len(_obv_arr) >= 6 else 0.0

                # Swing distances (approx)
                _w = min(100, _n)
                _rec_hi = float(np.max(_hi[-_w:]))
                _rec_lo = float(np.min(_lo[-_w:]))
                _sw_hi_dist = (_rec_hi - current_price) / (current_price + 1e-9) * 100
                _sw_lo_dist = (current_price - _rec_lo) / (current_price + 1e-9) * 100

                _ind.update(dict(
                    pct_rank_200=_pr200, pct_rank_500=_pr500,
                    z_score_200=_z200, z_score_500=_z500,
                    don_pos_100=_dp100, don_pos_200=_dp200,
                    don_width_100=_dw100, don_width_200=_dw200,
                    pivot=_pvt, r1=_r1, r2=_r2, s1=_s1, s2=_s2,
                    dist_to_r1=_dist_r1, dist_to_s1=_dist_s1,
                    above_pivot=_above_pv, pivot_zone=_pv_zone,
                    adx_14=_adx_val, di_plus=_dip, di_minus=_dim,
                    adx_trend=_adx_trend,
                    trend_4h=_t4h, dev_4h=_dev4h,
                    obv_trend=_obv_tr, obv_slope=_obv_sl,
                    swing_hi_dist=_sw_hi_dist, swing_lo_dist=_sw_lo_dist,
                    market_structure=0.0,  # упрощённо без scipy в реальном времени
                ))
            except Exception:
                pass  # расширенные индикаторы необязательны
        except Exception as _ie:
            pass  # индикаторы необязательны — не ломаем основной поток

        result = {
            'signal':      signal,
            'confidence':  confidence,
            'deviation':   avg_deviation,
            'buy_votes':   buy_votes,
            'sell_votes':  sell_votes,
            'total_votes': total_votes,
            # передаём в main.py для log_decision
            'dev_values':  dev_values,
            'sma_values':  sma_values,
            'timestamp':   datetime.now().isoformat(),
        }

        if signal != 'HOLD':
            print(f"\n🔔 СИГНАЛ {signal} для {symbol} "
                  f"(уверенность: {confidence * 100:.1f}%, "
                  f"отклонение: {avg_deviation:+.2f}%, "
                  f"голоса: {buy_votes}B/{sell_votes}S)")

        # ── Логирование индикаторов каждый тик ──────────────────────────────
        try:
            _last = df.iloc[-1]
            log_indicators(
                symbol        = symbol,
                candle_open   = float(_last.get('open',  current_price) if hasattr(_last, 'get') else _last.open),
                candle_high   = float(_last.get('high',  current_price) if hasattr(_last, 'get') else _last.high),
                candle_low    = float(_last.get('low',   current_price) if hasattr(_last, 'get') else _last.low),
                candle_close  = float(current_price),
                candle_vol    = float(_last.get('volume', 0) if hasattr(_last, 'get') else _last.volume),
                sma_50        = float(sma_values.get('50',  0)),
                sma_75        = float(sma_values.get('75',  0)),
                sma_100       = float(sma_values.get('100', 0)),
                sma_150       = float(sma_values.get('150', 0)),
                sma_200       = float(sma_values.get('200', 0)),
                dev_50        = float(dev_values.get('50',  0)),
                dev_75        = float(dev_values.get('75',  0)),
                dev_100       = float(dev_values.get('100', 0)),
                dev_150       = float(dev_values.get('150', 0)),
                dev_200       = float(dev_values.get('200', 0)),
                avg_deviation = avg_deviation,
                buy_votes     = buy_votes,
                sell_votes    = sell_votes,
                signal        = signal,
                confidence    = confidence,
                source        = 'rule_based',
                **_ind,
            )
        except Exception as _le:
            pass

        self.cache.save_signal(symbol, result)
        self.signals[symbol] = result
        return result

    def _momentum_filter(self, df: pd.DataFrame, prices: np.ndarray) -> tuple:
        """
        Фильтрует BUY сигнал если цена в нисходящем импульсе.
        Возвращает (заблокировать: bool, причина: str).

        Три условия блокировки (любое одно):
          1. N красных свечей подряд  → начало падения
          2. Падение > MAX_DROP% за MOMENTUM_BARS баров → сильный нисходящий тренд
          3. RSI-14 > RSI_MAX → цена ещё не перепродана (не дно)
        """
        RED_STREAK     = 5      # свечей подряд красных
        MAX_DROP_PCT   = 3.0    # % падения за последние N баров
        MOMENTUM_BARS  = 15     # окно для проверки momentum
        RSI_MAX        = 65     # RSI выше этого → не перепродано

        n = len(prices)
        if n < MOMENTUM_BARS + 2:
            return False, ''

        # 1. N красных свечей подряд (тело свечи)
        if 'open' in df.columns:
            opens  = df['open'].values.astype(float)
            closes = df['close'].values.astype(float)
            red_count = sum(
                1 for i in range(1, RED_STREAK + 1)
                if closes[-i] < opens[-i]
            )
            if red_count >= RED_STREAK:
                return True, f'{RED_STREAK} красных свечей подряд'

        # 2. Падение > MAX_DROP_PCT за последние MOMENTUM_BARS баров
        if n >= MOMENTUM_BARS:
            price_start = prices[-MOMENTUM_BARS]
            price_now   = prices[-1]
            if price_start > 0:
                drop_pct = (price_start - price_now) / price_start * 100
                if drop_pct >= MAX_DROP_PCT:
                    return True, f'падение {drop_pct:.1f}% за {MOMENTUM_BARS} баров'

        # 3. RSI-14 > RSI_MAX → цена не перепродана, рано покупать
        if n >= 15:
            _cl = prices.astype(float)
            _d  = np.diff(_cl[-15:])
            _g  = np.where(_d > 0, _d, 0.0).mean()
            _l  = np.where(_d < 0, -_d, 0.0).mean()
            rsi = 100.0 - 100.0 / (1.0 + _g / (_l + 1e-9)) if _l > 1e-9 else 100.0
            if rsi > RSI_MAX:
                return True, f'RSI={rsi:.1f} > {RSI_MAX} (не перепродано)'

        # 4. Спайк-фильтр — экстремальная свеча отравляет SMA
        # Если за последние SPIKE_WINDOW баров была свеча > SPIKE_PCT
        # (например +4% за 1 минуту) → SMA ещё не очистилась → не входим
        SPIKE_PCT    = 0.04   # 4% за одну свечу
        SPIKE_WINDOW = 100    # блокируем на 100 баров (~1.5 часа)
        if n >= 2:
            check_from = max(0, n - SPIKE_WINDOW)
            for _k in range(check_from + 1, n):
                if prices[_k-1] > 0:
                    move = abs(prices[_k] - prices[_k-1]) / prices[_k-1]
                    if move >= SPIKE_PCT:
                        bars_since = n - 1 - _k
                        return True, (f'спайк {move*100:.1f}% на баре -{bars_since}'
                                      f' (SMA отравлена, жди {SPIKE_WINDOW-bars_since} баров)')

        return False, ''

    def get_cached_signals(self) -> dict:
        """Получение всех кэшированных сигналов"""
        return self.cache.get_all_signals()
