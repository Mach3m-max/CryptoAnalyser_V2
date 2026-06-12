# ═══════════════════════════════════════════════════════════════════════════════
# stable_core/__init__.py
#
# HTT v2 — Пакет отлаженных модулей ядра
# ═══════════════════════════════════════════════════════════════════════════════
#
# ПРАВИЛО: Файлы в этой директории — verbatim copy из v1 (BotByBit).
#          Код отлажен в реальных рыночных условиях.
#
# ❌ НЕ ИЗМЕНЯТЬ без:
#    1. Создания теста, который подтверждает что изменение безопасно
#    2. Согласования с командой
#    3. Проверки в DEMO перед деплоем в REAL
#
# ── Состав пакета ─────────────────────────────────────────────────────────────
#
# bybit_client.py
#   Класс BybitTrader — HTTP-клиент Bybit API v5.
#   URL: DEMO = https://api-demo.bybit.com  (testnet=True)
#        REAL = https://api.bybit.com        (testnet=False)
#
#   ВНИМАНИЕ: параметр называется 'testnet', но фактически это DEMO-аккаунт
#   на api-demo.bybit.com, а НЕ https://api-testnet.bybit.com (устаревший testnet).
#
# order_executor.py
#   Класс OrderExecutor — все методы работы с ордерами.
#   Методы: place_order, _cancel_tp_sl, _calc_order_params,
#           _restore_entry_price, _detect_close_reason,
#           _move_sl_to_breakeven, _save/_load_order_ids.
#
# ── Быстрый старт ─────────────────────────────────────────────────────────────
#
#   from stable_core.bybit_client import BybitTrader
#   from stable_core.order_executor import OrderExecutor
#
#   trader = BybitTrader(api_key, api_secret, testnet=True)   # DEMO
#   # или
#   trader = BybitTrader(api_key, api_secret, testnet=False)  # REAL
#
#   executor = OrderExecutor(
#       trader=trader,
#       instruments={},           # заполняется data_loader'ом
#       portfolio=portfolio_mgr,
#       open_order_ids=order_ids_dict,
#       real_mode=False,
#       current_data=current_data_dict,
#       breakeven_activated={},
#   )
#
#   ok = executor.place_order("INJUSDT", "BUY", 350.0, tp_percent=3.5, sl_percent=3.0)
#
# ═══════════════════════════════════════════════════════════════════════════════

from .bybit_client  import BybitTrader
from .order_executor import OrderExecutor

__all__ = ["BybitTrader", "OrderExecutor"]

# URL константы для справки
DEMO_URL = "https://api-demo.bybit.com"
REAL_URL = "https://api.bybit.com"
