# dashboard/app.py
"""
Веб-дашборд HTT v2.
DEMO: localhost:5000
REAL: localhost:5001

Многостраничный Flask. REST API совместим с v1 (POST /api/buy, /api/close_position и т.д.)
"""

import os
import sys
import json
import math
import threading

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from flask import Flask, render_template, jsonify, request, redirect
from flask_socketio import SocketIO

from config.app_config import (
    DASHBOARD_DEMO_PORT, DASHBOARD_REAL_PORT,
    DASHBOARD_HOST, BOT_SETTINGS_PATH,
)

# ── Общее состояние (разделяется с bot/core.py) ───────────────────────────────
current_data = {
    'mode':             'DEMO',
    'trading_allowed':  False,
    'prices':           {},
    'signals':          {},
    'portfolio':        {},
    'balances':         {},
    'trades':           [],
    'min_confidence':   0.60,
    'settings_tp':      3.5,
    'settings_sl':      3.0,
    'shorts_enabled':   False,
}

# ── Flask приложение ──────────────────────────────────────────────────────────
templates_dir = os.path.join(ROOT, "templates")
static_dir    = os.path.join(ROOT, "static")

app = Flask(__name__,
            template_folder=templates_dir,
            static_folder=static_dir)
app.config['SECRET_KEY'] = 'htt_v2_secret'
socketio = SocketIO(app, async_mode='threading', cors_allowed_origins='*')


def register_bot(bot_instance):
    """Регистрирует экземпляр бота в app.config."""
    app.config['BOT'] = bot_instance


def _get_bot():
    return app.config.get('BOT')


def _save_bot_settings(data: dict):
    """Атомарная запись bot_settings.json."""
    tmp = BOT_SETTINGS_PATH + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, BOT_SETTINGS_PATH)


# ── Фоновое обновление дашборда ───────────────────────────────────────────────

def _push_updates():
    """Рассылает обновления через WebSocket каждые 5 сек."""
    import time
    while True:
        try:
            bot = _get_bot()
            if bot:
                bot.update_real_balances()
            socketio.emit('update', current_data)
        except Exception:
            pass
        time.sleep(5)


# ── Страницы ──────────────────────────────────────────────────────────────────

@app.route('/')
def index():
    return render_template('index.html', data=current_data)


@app.route('/positions')
def positions():
    return render_template('positions.html', data=current_data)


@app.route('/history')
def history():
    """История сделок из дневных decisions_*.csv."""
    from datetime import datetime, timedelta
    import csv
    import glob

    decisions_dir = os.path.join(ROOT, "data", "decisions")
    trades = []
    if os.path.exists(decisions_dir):
        pattern = os.path.join(decisions_dir, "decisions_*.csv")
        for fpath in sorted(glob.glob(pattern), reverse=True)[:30]:
            try:
                with open(fpath, encoding='utf-8') as f:
                    reader = csv.DictReader(f, delimiter=';')
                    for row in reader:
                        if 'POSITION_CLOSED' in row.get('event_type', ''):
                            trades.append(row)
            except Exception:
                pass
    return render_template('history.html', trades=trades[:200], data=current_data)


@app.route('/balances')
def balances():
    return render_template('balances.html', data=current_data)


@app.route('/settings')
def settings():
    # Читаем pairs_config для таблицы
    pairs_cfg_path = os.path.join(ROOT, 'config', 'pairs_config.json')
    pairs_cfg = {}
    if os.path.exists(pairs_cfg_path):
        with open(pairs_cfg_path, encoding='utf-8') as f:
            pairs_cfg = json.load(f)
    return render_template('settings.html', data=current_data, pairs_config=pairs_cfg)


# ── REST API ──────────────────────────────────────────────────────────────────

@app.route('/api/status')
def api_status():
    return jsonify(current_data)


@app.route('/api/toggle_trading', methods=['POST'])
def toggle_trading():
    current_data['trading_allowed'] = not current_data.get('trading_allowed', False)
    state = current_data['trading_allowed']
    print(f"{'✅' if state else '⏸️'} Торговля {'ВКЛЮЧЕНА' if state else 'ВЫКЛЮЧЕНА'}")
    return jsonify({'success': True, 'trading_allowed': state})


@app.route('/api/toggle_shorts', methods=['POST'])
def toggle_shorts():
    bot = _get_bot()
    if not bot:
        return jsonify({'success': False, 'error': 'Бот не инициализирован'})
    bot.shorts_enabled = not bot.shorts_enabled
    current_data['shorts_enabled'] = bot.shorts_enabled
    s = bot.shorts_enabled
    # Сохраняем в bot_settings
    try:
        settings = {}
        if os.path.exists(BOT_SETTINGS_PATH):
            with open(BOT_SETTINGS_PATH) as f:
                settings = json.load(f)
        settings['shorts_enabled'] = s
        _save_bot_settings(settings)
    except Exception:
        pass
    print(f"📉 Шорты: {'ВКЛЮЧЕНЫ' if s else 'ВЫКЛЮЧЕНЫ'}")
    return jsonify({'success': True, 'shorts_enabled': s})


@app.route('/api/buy', methods=['POST'])
def api_buy():
    """Ручная покупка через дашборд."""
    if not current_data.get('trading_allowed', False):
        return jsonify({'success': False, 'error': 'Торговля выключена'})
    bot = _get_bot()
    if not bot:
        return jsonify({'success': False, 'error': 'Бот не инициализирован'})

    data       = request.json or {}
    symbol     = data.get('symbol', '').strip().upper()
    usdt_amt   = float(data.get('amount', 0))
    tp_pct     = float(data.get('tp_pct', 3.5))
    sl_pct     = float(data.get('sl_pct', 3.0))

    if not symbol or usdt_amt <= 0:
        return jsonify({'success': False, 'error': 'Неверные параметры'})

    can, reason = bot.portfolio.can_open(symbol)
    if not can:
        return jsonify({'success': False, 'error': reason})

    print(f"\n📥 /api/buy: {symbol} | {usdt_amt} USDT | TP={tp_pct}% SL={sl_pct}%")
    success = bot.executor.place_order(symbol, "BUY", usdt_amt, tp_pct, sl_pct)

    if success:
        # ── FIX v2: регистрируем позицию в portfolio ─────────────────────────
        cur_price = current_data['prices'].get(symbol, 0)
        bot.portfolio.open_position_from_dashboard(symbol, usdt_amt, cur_price)
        bot._sw_closing.discard(symbol)
        bot.breakeven_activated.pop(symbol, None)
        bot._trail_best.pop(symbol, None)
        return jsonify({'success': True})
    else:
        return jsonify({'success': False, 'error': 'Ордер отклонён — см. лог бота'})


@app.route('/api/close_position', methods=['POST'])
def api_close_position():
    """Закрыть одну позицию."""
    bot    = _get_bot()
    data   = request.json or {}
    symbol = data.get('symbol', '').strip().upper()
    if not bot or not symbol:
        return jsonify({'ok': False, 'message': 'Нет бота или символа'})
    try:
        bot.executor._cancel_tp_sl(symbol)
        coin    = symbol.replace('USDT', '')
        coin_bal = bot.trader.get_coin_balance(coin)
        sold = False
        if coin_bal > 0:
            instr = bot.instruments.get(symbol, {})
            step  = float(instr.get('qty_step', 0.001))
            qty_dec = len(str(step).rstrip('0').split('.')[-1]) if '.' in str(step) else 0
            qty   = round(math.floor(coin_bal / step) * step, qty_dec)
            if qty > 0:
                r = bot.trader.place_order(
                    category='spot', symbol=symbol,
                    side='Sell', order_type='Market',
                    qty=str(qty), market_unit='baseCoin',
                )
                sold = r.get('retCode') == 0
        bot.portfolio.close_position(symbol)
        bot.breakeven_activated.pop(symbol, None)
        bot._trail_best.pop(symbol, None)
        bot._sw_closing.discard(symbol)
        msg = "позиция закрыта" if sold else "ордера отменены (монет не было)"
        return jsonify({'ok': True, 'message': msg})
    except Exception as e:
        return jsonify({'ok': False, 'message': str(e)})


@app.route('/api/close_all', methods=['POST'])
def api_close_all():
    """Закрыть все позиции."""
    bot = _get_bot()
    if not bot:
        return jsonify({'ok': False, 'message': 'Бот не инициализирован'})
    closed, errors = [], []
    for symbol in list(bot.portfolio.open_positions.keys()):
        try:
            bot.executor._cancel_tp_sl(symbol)
            coin    = symbol.replace('USDT', '')
            coin_bal = bot.trader.get_coin_balance(coin)
            if coin_bal > 0:
                instr = bot.instruments.get(symbol, {})
                step  = float(instr.get('qty_step', 0.001))
                qty_dec = len(str(step).rstrip('0').split('.')[-1]) if '.' in str(step) else 0
                qty   = round(math.floor(coin_bal / step) * step, qty_dec)
                if qty > 0:
                    r = bot.trader.place_order(
                        category='spot', symbol=symbol,
                        side='Sell', order_type='Market',
                        qty=str(qty), market_unit='baseCoin',
                    )
                    if r.get('retCode') == 0:
                        closed.append(symbol)
                    else:
                        errors.append(f"{symbol}: {r.get('retMsg')}")
            bot.portfolio.close_position(symbol)
            bot.breakeven_activated.pop(symbol, None)
            bot._trail_best.pop(symbol, None)
            bot._sw_closing.discard(symbol)
        except Exception as e:
            errors.append(f"{symbol}: {e}")
    msg = f"Закрыты: {closed}" + (f"  Ошибки: {errors}" if errors else "")
    return jsonify({'ok': True, 'message': msg})


@app.route('/api/update_settings', methods=['POST'])
def api_update_settings():
    """Обновляет глобальные параметры стратегии."""
    data = request.json or {}
    bot  = _get_bot()

    changed = []
    for key in ('tp_pct', 'sl_pct', 'min_confidence', 'max_positions',
                'position_divider', 'breakeven_trigger', 'trailing_pct'):
        val = data.get(key)
        if val is None:
            continue
        try:
            fval = float(val)
            if key == 'tp_pct':           current_data['settings_tp'] = fval
            elif key == 'sl_pct':         current_data['settings_sl'] = fval
            elif key == 'min_confidence': current_data['min_confidence'] = fval
            elif key == 'max_positions' and bot:
                bot.portfolio.max_positions = int(fval)
            elif key == 'position_divider' and bot:
                bot.portfolio.position_divider = int(fval)
            elif key == 'breakeven_trigger' and bot:
                bot.BREAKEVEN_TRIGGER = fval
            elif key == 'trailing_pct' and bot:
                bot.TRAILING_PCT = fval
            changed.append(f"{key}={fval}")
        except Exception:
            pass

    if changed:
        try:
            settings = {}
            if os.path.exists(BOT_SETTINGS_PATH):
                with open(BOT_SETTINGS_PATH) as f:
                    settings = json.load(f)
            settings.update({k: data[k] for k in data if k in settings or data.get(k)})
            _save_bot_settings(settings)
        except Exception:
            pass

    return jsonify({'success': True, 'changed': changed})


@app.route('/api/pair_params', methods=['GET', 'POST'])
def api_pair_params():
    """Чтение и запись pairs_config.json."""
    path = os.path.join(ROOT, 'config', 'pairs_config.json')
    if request.method == 'GET':
        try:
            with open(path, encoding='utf-8') as f:
                return jsonify({'success': True, 'params': json.load(f)})
        except Exception as e:
            return jsonify({'success': False, 'error': str(e), 'params': {}})
    # POST
    try:
        body   = request.get_json() or {}
        params = body.get('params', body)
        with open(path, 'w', encoding='utf-8') as f:
            json.dump(params, f, indent=2, ensure_ascii=False)
        bot = _get_bot()
        if bot:
            bot.pairs_config = params
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})


@app.route('/api/credentials', methods=['POST'])
def api_credentials():
    """Сохраняет API-ключи через UI."""
    from security.key_manager import save_credentials
    data     = request.json or {}
    mode     = data.get('mode', 'demo').lower()
    api_key  = data.get('api_key', '').strip()
    api_sec  = data.get('api_secret', '').strip()
    password = data.get('master_password', '').strip()

    if not api_key or not api_sec or not password:
        return jsonify({'success': False, 'error': 'Заполните все поля'})

    ok = save_credentials(mode, api_key, api_sec, password)
    return jsonify({'success': ok, 'message': f'Ключи [{mode.upper()}] сохранены' if ok else 'Ошибка'})


# ── Запуск ────────────────────────────────────────────────────────────────────

def start_dashboard(mode: str = 'DEMO'):
    """Запускает Flask-сервер в фоновом потоке."""
    port = DASHBOARD_DEMO_PORT if mode == 'DEMO' else DASHBOARD_REAL_PORT

    # Фоновый поток обновлений
    t = threading.Thread(target=_push_updates, daemon=True)
    t.start()

    print(f"🌐 Дашборд [{mode}]: http://localhost:{port}")
    socketio.run(app, host=DASHBOARD_HOST, port=port,
                 debug=False, use_reloader=False, log_output=False)
