# portfolio_manager.py
"""
Управление позициями и капиталом.
v1 verbatim + FIX: open_position_from_dashboard() для ручных покупок через UI.
"""
import time
import os
import json
from typing import Dict

import sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

try:
    from config.app_config import DEFAULT_MAX_POSITIONS, DEFAULT_POSITION_DIVIDER
except ImportError:
    DEFAULT_MAX_POSITIONS    = 3
    DEFAULT_POSITION_DIVIDER = 3


class PortfolioManager:
    """
    Управление позициями и капиталом.
    Хранит open_positions в памяти; при рестарте восстанавливается
    через _restore_entry_price() в monitor_positions().
    """

    def __init__(self, total_capital: float = 1000.0,
                 max_positions: int = DEFAULT_MAX_POSITIONS,
                 position_divider: int = DEFAULT_POSITION_DIVIDER):
        self.total_capital    = total_capital
        self.max_positions    = max_positions
        self.position_divider = position_divider
        self.open_positions: Dict[str, dict] = {}
        self.pnl: Dict[str, float]           = {}

    # ── Проверки ──────────────────────────────────────────────────────────────

    def is_open(self, symbol: str) -> bool:
        """Есть ли открытая позиция по символу."""
        return symbol in self.open_positions

    def open_count(self) -> int:
        """Количество открытых позиций."""
        return len(self.open_positions)

    def can_open(self, symbol: str, wallet_coins: list = None) -> tuple:
        """
        Можно ли открыть новую позицию.
        Returns: (bool, reason_str)
        """
        if symbol in self.open_positions:
            return False, f"Уже открыта позиция по {symbol}"
        if self.open_count() >= self.max_positions:
            return False, f"Достигнут лимит позиций ({self.max_positions})"
        return True, "OK"

    # ── Управление позициями ──────────────────────────────────────────────────

    def open_position(self, symbol: str, usdt_amount: float,
                      entry_price: float):
        """Регистрация открытой позиции (вызывается из execute_signal)."""
        self.open_positions[symbol] = {
            'entry_price': entry_price,
            'usdt_amount': usdt_amount,
            'open_time':   time.time(),
            'source':      'bot',       # 'bot' | 'dashboard' | 'external'
        }

    def open_position_from_dashboard(self, symbol: str, usdt_amount: float,
                                      entry_price: float):
        """
        FIX v2: Регистрация позиции открытой ВРУЧНУЮ через дашборд.
        dashboard /api/buy должен вызывать именно этот метод.
        Без этого entry='—', PnL='?' и trail не работает.
        """
        self.open_positions[symbol] = {
            'entry_price': entry_price,
            'usdt_amount': usdt_amount,
            'open_time':   time.time(),
            'source':      'dashboard',
        }
        print(f"   📌 portfolio.open_position_from_dashboard("
              f"{symbol}, {usdt_amount:.2f} USDT, {entry_price:.4f})")

    def close_position(self, symbol: str):
        """Снятие позиции с учёта."""
        self.open_positions.pop(symbol, None)
        self.pnl.pop(symbol, None)

    def update_entry_price(self, symbol: str, new_price: float):
        """Обновление цены входа (после восстановления из execution/list)."""
        if symbol in self.open_positions:
            self.open_positions[symbol]['entry_price'] = new_price

    # ── Капитал ───────────────────────────────────────────────────────────────

    def get_order_amount(self) -> float:
        """Сумма одного ордера = total_capital / position_divider."""
        divider = self.position_divider if self.position_divider > 0 else 1
        return round(self.total_capital / divider, 2)

    def get_available_slots(self) -> int:
        """Сколько ещё позиций можно открыть."""
        return max(0, self.max_positions - self.open_count())

    # ── Сводка ────────────────────────────────────────────────────────────────

    def get_portfolio_summary(self) -> dict:
        """Возвращает полное состояние портфеля для дашборда."""
        return {
            'total_capital':    self.total_capital,
            'open_positions':   self.open_positions,
            'open_count':       self.open_count(),
            'max_positions':    self.max_positions,
            'position_divider': self.position_divider,
            'order_amount':     self.get_order_amount(),
            'available_slots':  self.get_available_slots(),
            'pnl':              self.pnl,
            'total_pnl':        sum(self.pnl.values()),
        }
