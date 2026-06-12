# config/__init__.py
# ─────────────────────────────────────────────────────────────────────────────
# Делает `config/` одновременно пакетом (config.app_config работает)
# И v1-совместимым модулем (from config import OPTIMAL_PARAMS работает).
#
# v1-модули делают: from config import OPTIMAL_PARAMS, PORTFOLIO, ...
# v2-модули делают: from config.app_config import ABS_RESERVE, ...
# Оба варианта работают благодаря этому файлу.
# ─────────────────────────────────────────────────────────────────────────────

import os
import json

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# ── Загружаем pairs_config.json ───────────────────────────────────────────────
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
PORTFOLIO = {}
_priority = 1
for _sym, _cfg in _pairs_cfg.items():
    PORTFOLIO[_sym] = {
        'weight':   _cfg.get('weight', 0.05),
        'group':    _cfg.get('group', 'growth'),
        'priority': _priority,
        'tp_pct':   _cfg.get('tp_pct', 3.5),
        'sl_pct':   _cfg.get('sl_pct', 3.0),
        'min_conf': _cfg.get('min_conf', 0.60),
    }
    _priority += 1

# ── OPTIMAL_PARAMS ────────────────────────────────────────────────────────────
OPTIMAL_PARAMS = {
    'entry_threshold': 2.0,
    'stop_loss':       3.0,
    'take_profit':     3.5,
    'risk_percent':    1.0,
    'SMA':             100,
    'AUTO_TRADE':      False,
}

# ── TECH_PARAMS ───────────────────────────────────────────────────────────────
TECH_PARAMS = {
    'sma_windows':  [50, 75, 100, 150, 200],
    'timeframe':    '1',
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

# ── Комиссии ──────────────────────────────────────────────────────────────────
BUY_FEE  = 0.0028
SELL_FEE = 0.0028

# ── CORRELATION_DATA ──────────────────────────────────────────────────────────
CORRELATION_DATA = {
    'BTCUSDT': {'hedge_pairs': ['ETHUSDT'],  'hedge_correlation': 0.92},
    'ETHUSDT': {'hedge_pairs': ['BTCUSDT'],  'hedge_correlation': 0.92},
    'SOLUSDT': {'hedge_pairs': ['AVAXUSDT'], 'hedge_correlation': 0.78},
}

# ── load_api_config (v1 совместимость) ────────────────────────────────────────
def load_api_config(real_mode: bool = False) -> dict:
    """
    v1-совместимость. В v2 ключи хранятся зашифрованными.
    bot/core.py получает ключи через security.load_credentials() напрямую.
    Этот метод — fallback для v1-модулей, которые его импортируют.
    """
    fname = 'configReal.json' if real_mode else 'config.json'
    fpath = os.path.join(_ROOT, fname)
    if os.path.exists(fpath):
        with open(fpath, encoding='utf-8') as f:
            return json.load(f)
    return {'api_key': '', 'api_secret': '', 'testnet': not real_mode}


# ── Вспомогательные функции ───────────────────────────────────────────────────
def get_portfolio_stats() -> dict:
    groups = {'core': 0.0, 'growth': 0.0, 'hedge': 0.0}
    for data in PORTFOLIO.values():
        groups[data.get('group', 'growth')] = (
            groups.get(data.get('group', 'growth'), 0) + data.get('weight', 0)
        )
    return groups


def get_balanced_allocation(total_capital: float = 10000) -> dict:
    return {
        sym: {
            'amount':   total_capital * d['weight'],
            'weight':   d['weight'],
            'group':    d['group'],
            'priority': d['priority'],
        }
        for sym, d in PORTFOLIO.items()
    }


def get_main_pair(symbol: str):
    for sym, data in CORRELATION_DATA.items():
        if symbol in data.get('hedge_pairs', []):
            return sym
    return None
