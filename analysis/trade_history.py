# analysis/trade_history.py
import os, csv, glob
from datetime import datetime
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from config.app_config import DATA_DIR

DECISIONS_DIR = os.path.join(DATA_DIR, "decisions")


class TradeHistory:
    """Читает посуточные decisions_*.csv и возвращает сводку сделок."""

    def __init__(self):
        self.decisions_dir = DECISIONS_DIR

    def _load_files(self, days: int = 30) -> list:
        pattern = os.path.join(self.decisions_dir, "decisions_*.csv")
        files = sorted(glob.glob(pattern), reverse=True)[:days]
        rows = []
        for fp in files:
            try:
                with open(fp, encoding="utf-8") as f:
                    reader = csv.DictReader(f, delimiter=";")
                    rows.extend(list(reader))
            except Exception:
                pass
        return rows

    def get_closed_trades(self, days: int = 30) -> list:
        """Возвращает только закрытые позиции с PnL."""
        rows = self._load_files(days)
        return [
            r for r in rows
            if r.get("event_type", "").startswith("POSITION_CLOSED")
        ]

    def get_summary(self, days: int = 30) -> dict:
        """Сводная статистика за период."""
        trades = self.get_closed_trades(days)
        if not trades:
            return {"total": 0, "wins": 0, "losses": 0,
                    "win_rate": 0.0, "total_pnl_usdt": 0.0}
        wins = [t for t in trades if float(t.get("pnl_usdt") or 0) > 0]
        losses = [t for t in trades if float(t.get("pnl_usdt") or 0) <= 0]
        total_pnl = sum(float(t.get("pnl_usdt") or 0) for t in trades)
        return {
            "total":         len(trades),
            "wins":          len(wins),
            "losses":        len(losses),
            "win_rate":      round(len(wins) / len(trades) * 100, 1),
            "total_pnl_usdt": round(total_pnl, 2),
        }

    def get_by_pair(self, pair: str, days: int = 30) -> list:
        """Сделки по конкретной паре."""
        return [t for t in self.get_closed_trades(days)
                if t.get("symbol") == pair]
