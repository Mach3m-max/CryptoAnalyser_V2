# config/app_config.py
"""
Глобальные константы HTT v2.
Параметры пар — в config/pairs_config.json (не здесь).
"""

# ── Версия ────────────────────────────────────────────────────────────────────
PROJECT_NAME    = "Harmonic Trading Terminal"
PROJECT_VERSION = "2.0.0"

# ── Торговые параметры (глобальные дефолты) ───────────────────────────────────
ABS_RESERVE          = 5.50   # USDT — минимальный свободный остаток
FEE_PCT              = 0.0028 # 0.28% комиссия Bybit Spot (taker)
LOW_BALANCE_FREEZE_M = 5      # минут заморозки при нехватке баланса

# ── Управление позициями ──────────────────────────────────────────────────────
DEFAULT_MAX_POSITIONS   = 3
DEFAULT_POSITION_DIVIDER = 3

# ── Трейлинг и безубыток (глобальные дефолты; переопределяются в pairs_config) ─
BREAKEVEN_TRIGGER   = 2.5   # % роста для активации безубытка
TRAILING_PCT        = 1.5   # % отступа SL от максимума
TRAILING_MIN_MOVE   = 0.3   # минимальный % движения для обновления trail
SELL_CLOSE_CONF     = 0.70  # уверенность SELL для досрочного выхода
BREAKEVEN_OFFSET    = 0.003 # SL = entry * (1 + 0.3%) — покрывает комиссии
MAX_HOLD_BARS       = 1440  # принудительное закрытие через 24ч (1440 мин)

# ── Таймауты ──────────────────────────────────────────────────────────────────
ANALYZE_INTERVAL_SEC  = 60   # пауза между циклами анализа рынка
MONITOR_INTERVAL_SEC  = 10   # пауза между циклами мониторинга позиций
SHORTS_INTERVAL_SEC   = 15   # пауза между циклами мониторинга шортов
DEBOUNCE_SEC          = 240  # антиспам: мин. интервал между сигналами по одной паре
API_TIMEOUT_SEC       = 10   # таймаут HTTP-запросов к Bybit

# ── SMA параметры (для feature generation) ────────────────────────────────────
SMA_WINDOWS     = [50, 75, 100, 150, 200]
SMA_TIMEFRAME   = "1"        # минутные свечи
SMA_MIN_VOTES   = 3          # минимум голосов для сигнала (из 5)

# ── Дашборд ───────────────────────────────────────────────────────────────────
DASHBOARD_DEMO_PORT = 5000
DASHBOARD_REAL_PORT = 5001
DASHBOARD_HOST      = "0.0.0.0"

# ── Пути (относительно корня проекта) ─────────────────────────────────────────
import os
BASE_DIR          = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR          = os.path.join(BASE_DIR, "data")
LOGS_DIR          = os.path.join(BASE_DIR, "logs")
CREDENTIALS_DIR   = os.path.join(BASE_DIR, "credentials")
ML_MODELS_DIR     = os.path.join(BASE_DIR, "ml", "models")
ML_STAGING_DIR    = os.path.join(BASE_DIR, "ml", "staging")
PAIRS_CONFIG_PATH = os.path.join(BASE_DIR, "config", "pairs_config.json")
BOT_SETTINGS_PATH = os.path.join(BASE_DIR, "bot_settings.json")

# Создаём директории если не существуют
for _d in [DATA_DIR, LOGS_DIR, CREDENTIALS_DIR,
           os.path.join(DATA_DIR, "candles"),
           os.path.join(DATA_DIR, "indicators"),
           os.path.join(DATA_DIR, "signals"),
           os.path.join(DATA_DIR, "decisions"),
           os.path.join(DATA_DIR, "historical"),
           ML_MODELS_DIR, ML_STAGING_DIR]:
    os.makedirs(_d, exist_ok=True)
