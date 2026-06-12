from typing import Optional
# config.py  (корень проекта)
# ─────────────────────────────────────────────────────────────────────────────
# Shim для совместимости с v1-модулями которые делают:
#   from config import OPTIMAL_PARAMS, PORTFOLIO, TECH_PARAMS, LOG_FLAGS
#   from config import load_api_config, PROJECT_INFO
#
# В v2 конфигурация разделена:
#   - Глобальные параметры  → config/app_config.py
#   - Пары                  → config/pairs_config.json
#   - API-ключи             → security/key_manager.py
#
# Этот файл строит v1-совместимые объекты на лету из v2-конфигов.
# НЕ редактируй этот файл — меняй config/app_config.py и config/pairs_config.json
# ─────────────────────────────────────────────────────────────────────────────

import os
import json

# ── Загружаем v2-конфиг ───────────────────────────────────────────────────────
_ROOT = os.path.dirname(os.path.abspath(__file__))

# pairs_config.json → PORTFOLIO (v1-формат)
_pairs_path = os.path.join(_ROOT, 'config', 'pairs_config.json')
try:
    with open(_pairs_path, encoding='utf-8') as _f:
        _pairs_cfg = json.load(_f)
except Exception:
    _pairs_cfg = {}

# ── PROJECT_INFO ──────────────────────────────────────────────────────────────
PROJECT_INFO = {
    'name':    'Harmonic Trading Terminal',
    'version': '2.0.0',
}

# ── PORTFOLIO (v1-формат) ─────────────────────────────────────────────────────
# Строим из pairs_config.json для обратной совместимости
PORTFOLIO = {}
_priority = 1
for _sym, _cfg in _pairs_cfg.items():
    PORTFOLIO[_sym] = {
        'weight':    _cfg.get('weight', 0.05),
        'group':     _cfg.get('group', 'growth'),
        'priority':  _priority,
        'tp_pct':    _cfg.get('tp_pct', 3.5),
        'sl_pct':    _cfg.get('sl_pct', 3.0),
        'min_conf':  _cfg.get('min_conf', 0.60),
    }
    _priority += 1

# ── OPTIMAL_PARAMS ────────────────────────────────────────────────────────────
OPTIMAL_PARAMS = {
    'entry_threshold': 2.0,   # % отклонение от SMA для входа
    'stop_loss':       3.0,   # % SL по умолчанию
    'take_profit':     3.5,   # % TP по умолчанию
    'risk_percent':    1.0,
    'SMA':             100,
    'AUTO_TRADE':      False,
}

# ── TECH_PARAMS ───────────────────────────────────────────────────────────────
TECH_PARAMS = {
    'sma_windows':  [50, 75, 100, 150, 200],
    'timeframe':    '1',       # минутные свечи
    'min_votes':    3,
    'candle_limit': 220,
}

# ── LOG_FLAGS ─────────────────────────────────────────────────────────────────
LOG_FLAGS = {
    'dashboard_updates': False,
    'candle_updates':    False,
    'instruments_load':  False,
    'signal_details':    False,
}

# ── BUY_FEE / SELL_FEE (используется в ml_strategy_engine) ───────────────────
BUY_FEE  = 0.0028
SELL_FEE = 0.0028

# ── CORRELATION_DATA (хедж-пары, для совместимости) ──────────────────────────
CORRELATION_DATA = {
    'BTCUSDT': {'hedge_pairs': ['ETHUSDT'],   'hedge_correlation': 0.92},
    'ETHUSDT': {'hedge_pairs': ['BTCUSDT'],   'hedge_correlation': 0.92},
    'SOLUSDT': {'hedge_pairs': ['AVAXUSDT'],  'hedge_correlation': 0.78},
}

# ── load_api_config (v1 совместимость) ────────────────────────────────────────
def load_api_config(real_mode: bool = False) -> dict:
    """
    v1-совместимый загрузчик API-ключей.
    В v2 ключи хранятся зашифрованными — используй security.load_credentials().
    Этот метод оставлен для совместимости с v1-модулями и bot/core.py НЕ должен его вызывать.
    """
    # Пробуем v1-файлы (для миграции / резервного пути)
    fname = 'configReal.json' if real_mode else 'config.json'
    fpath = os.path.join(_ROOT, fname)
    if os.path.exists(fpath):
        with open(fpath, encoding='utf-8') as f:
            return json.load(f)
    # Возвращаем пустой шаблон — bot/core.py получит ключи через security.load_credentials
    return {'api_key': '', 'api_secret': '', 'testnet': not real_mode}


# ── Вспомогательные функции (v1 совместимость) ────────────────────────────────
def get_portfolio_stats() -> dict:
    groups = {'core': 0.0, 'growth': 0.0, 'hedge': 0.0}
    for data in PORTFOLIO.values():
        g = data.get('group', 'growth')
        groups[g] = groups.get(g, 0) + data.get('weight', 0)
    return groups


def get_balanced_allocation(total_capital: float = 10000) -> dict:
    return {
        sym: {
            'amount':   total_capital * data['weight'],
            'weight':   data['weight'],
            'group':    data['group'],
            'priority': data['priority'],
        }
        for sym, data in PORTFOLIO.items()
    }


def get_main_pair(symbol: str) -> Optional[str]:
    for sym, data in CORRELATION_DATA.items():
        if symbol in data.get('hedge_pairs', []):
            return sym
    return None
