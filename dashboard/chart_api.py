# dashboard/chart_api.py
"""REST API для графика — используется из demo.py и real.py."""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from flask import Blueprint, jsonify, request
from analysis.historical_engine import HistoricalEngine

chart_bp = Blueprint("chart", __name__)
_engine  = HistoricalEngine()


@chart_bp.route("/api/chart/<pair>")
def get_chart(pair: str):
    days = int(request.args.get("days", 3))
    data = _engine.get_chart_data(pair, days)
    return jsonify(data)


@chart_bp.route("/api/history/summary")
def get_summary():
    from analysis.trade_history import TradeHistory
    days = int(request.args.get("days", 30))
    th   = TradeHistory()
    return jsonify(th.get_summary(days))


@chart_bp.route("/api/history/trades")
def get_trades():
    from analysis.trade_history import TradeHistory
    days = int(request.args.get("days", 30))
    pair = request.args.get("pair")
    th   = TradeHistory()
    if pair:
        trades = th.get_by_pair(pair, days)
    else:
        trades = th.get_closed_trades(days)
    return jsonify(trades)
