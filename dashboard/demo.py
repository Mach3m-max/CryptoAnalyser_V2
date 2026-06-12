# dashboard/demo.py
"""DEMO-дашборд — Flask :5000"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from dashboard.app import app, socketio, current_data, register_bot
from dashboard.chart_api import chart_bp
from config.app_config import DASHBOARD_DEMO_PORT, DASHBOARD_HOST

app.register_blueprint(chart_bp)


def start(bot=None):
    if bot:
        register_bot(bot)
    current_data["mode"] = "DEMO"
    socketio.run(app, host=DASHBOARD_HOST,
                 port=DASHBOARD_DEMO_PORT, debug=False)


if __name__ == "__main__":
    start()
