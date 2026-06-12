# dashboard/real.py
"""REAL-дашборд — Flask :5001"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from dashboard.app import app, socketio, current_data, register_bot
from dashboard.chart_api import chart_bp
from config.app_config import DASHBOARD_REAL_PORT, DASHBOARD_HOST

app.register_blueprint(chart_bp)


def start(bot=None):
    if bot:
        register_bot(bot)
    current_data["mode"] = "REAL"
    socketio.run(app, host=DASHBOARD_HOST,
                 port=DASHBOARD_REAL_PORT, debug=False)


if __name__ == "__main__":
    start()
