# bybit_client.py  (корень проекта)
# ─────────────────────────────────────────────────────────────────────────────
# Shim для совместимости с v1-модулями (data_loader.py, strategy_engine.py и др.)
# которые делают: from bybit_client import BybitTrader
#
# Перенаправляет в stable_core.bybit_client — единственный источник правды.
# Не дублирует логику, не изменяет поведение.
# ─────────────────────────────────────────────────────────────────────────────
from stable_core.bybit_client import BybitTrader, sign  # noqa: F401

__all__ = ['BybitTrader', 'sign']
