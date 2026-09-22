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
from decimal import Decimal
from datetime import datetime
import threading
import time

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
    'short_min_confidence': 0.75,
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


def format_duration(seconds):
    """
    FIX: колонка 'Держим' на /positions и / — шаблон вычислял возраст позиции
    в {% set age = ... %}, но никогда его не выводил, печатая только литерал
    '—' независимо от результата. pos['open_time'] — time.time() (Unix-секунды),
    поэтому now тоже нужно передавать как time.time(), не datetime.
    Форматирует секунды в читаемый вид: '2ч 15м', '45м', '3д 2ч'.
    """
    try:
        seconds = int(seconds)
    except (TypeError, ValueError):
        return '—'
    if seconds < 0:
        return '—'
    days, rem   = divmod(seconds, 86400)
    hours, rem  = divmod(rem, 3600)
    minutes, _  = divmod(rem, 60)
    if days > 0:
        return f"{days}д {hours}ч"
    if hours > 0:
        return f"{hours}ч {minutes}м"
    if minutes > 0:
        return f"{minutes}м"
    return "<1м"


app.jinja_env.filters['duration'] = format_duration


def age_from_iso(iso_str):
    """
    FIX: та же колонка 'Держим', но для шортов (self.short_positions в
    bot/core.py) — там open_time хранится как datetime.now().isoformat(),
    а не time.time() как у обычных LONG-позиций. Раньше эта колонка у
    шортов была просто жёстким '—' без единой попытки посчитать.
    """
    if not iso_str:
        return '—'
    try:
        from datetime import datetime
        opened = datetime.fromisoformat(iso_str)
        seconds = (datetime.now() - opened).total_seconds()
        return format_duration(seconds)
    except (TypeError, ValueError):
        return '—'


app.jinja_env.filters['age_from_iso'] = age_from_iso


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
    return render_template('index.html', data=current_data, now=time.time())


@app.route('/positions')
def positions():
    return render_template('positions.html', data=current_data, now=time.time())


@app.route('/history')
def history():
    """История сделок — период/режим/лимит через analytics.core (тот же движок, что /analytics)."""
    from datetime import datetime, timedelta
    from analytics.core import load_decisions, prepare_closed
    import pandas as pd

    days_arg  = request.args.get('days', type=int)
    from_str  = request.args.get('from', '').strip()
    to_str    = request.args.get('to', '').strip()
    mode_arg  = request.args.get('mode', '').strip().upper()
    limit_arg = request.args.get('limit', type=int)

    date_from = date_to = None
    if from_str:
        try:
            date_from = datetime.strptime(from_str, '%Y-%m-%d')
        except ValueError:
            pass
    if to_str:
        try:
            date_to = datetime.strptime(to_str, '%Y-%m-%d') + timedelta(hours=23, minutes=59, seconds=59)
        except ValueError:
            pass

    days = None
    if not date_from and not date_to:
        days = days_arg or 30  # по умолчанию — 30 дней (как раньше, только теперь настраиваемо)

    limit = limit_arg or 200

    # Режим: по умолчанию — режим ЭТОГО дашборда (как было раньше), но можно явно
    # выбрать другой или "Все" — тогда строки будут вперемешку, зато виден режим в колонке
    if mode_arg in ('DEMO', 'REAL'):
        mode_filter = mode_arg
    elif mode_arg == 'ALL':
        mode_filter = None
    else:
        mode_filter = current_data.get('mode', 'DEMO')

    trades, error = [], None
    try:
        raw = load_decisions(days=days, date_from=date_from, date_to=date_to, mode=mode_filter)
        closed = prepare_closed(raw)
        if not closed.empty:
            closed = closed.sort_values('timestamp', ascending=False).head(limit)
            for _, row in closed.iterrows():
                ts = row.get('timestamp')
                trades.append({
                    'timestamp':    ts.strftime('%Y-%m-%d %H:%M') if pd.notna(ts) else '',
                    'symbol':       row.get('symbol', '') or '',
                    'event_type':   row.get('event_type', '') or '',
                    'close_reason': row.get('close_reason', '') or '',
                    'entry_price':  '' if pd.isna(row.get('entry_price')) else str(row.get('entry_price')),
                    'price':        '' if pd.isna(row.get('price')) else str(row.get('price')),
                    'pnl_pct':      '' if pd.isna(row.get('pnl_pct')) else str(row.get('pnl_pct')),
                    'pnl_usdt':     '' if pd.isna(row.get('pnl_usdt')) else str(row.get('pnl_usdt')),
                    'mode':         row.get('mode', '') or '',
                })
    except Exception as e:
        error = str(e)

    return render_template('history.html', trades=trades, data=current_data, error=error,
                           days=days, from_str=from_str, to_str=to_str,
                           mode_filter=mode_arg, limit=limit)


@app.route('/analytics')
def analytics():
    """Аналитика по decisions_*.csv: сводка, разрезы, детектор дублей."""
    from datetime import datetime, timedelta
    from analytics.core import build_report

    days_arg  = request.args.get('days', type=int)
    from_str  = request.args.get('from', '').strip()
    to_str    = request.args.get('to', '').strip()
    mode_arg  = request.args.get('mode', '').strip().upper()
    mode_filter = mode_arg if mode_arg in ('DEMO', 'REAL') else None

    date_from = date_to = None
    if from_str:
        try:
            date_from = datetime.strptime(from_str, '%Y-%m-%d')
        except ValueError:
            pass
    if to_str:
        try:
            date_to = datetime.strptime(to_str, '%Y-%m-%d') + timedelta(hours=23, minutes=59, seconds=59)
        except ValueError:
            pass

    days = None
    if not date_from and not date_to:
        days = days_arg or 3  # по умолчанию — последние 3 дня

    try:
        report = build_report(days=days, date_from=date_from, date_to=date_to, mode=mode_filter)
        error = None
    except Exception as e:
        report = None
        error = str(e)

    return render_template('analytics.html', data=current_data, report=report, error=error,
                           days=days, from_str=from_str, to_str=to_str,
                           mode_filter=mode_arg)


@app.route('/balances')
def balances():
    bot = _get_bot()
    free_usdt = bot.trader.get_coin_balance('USDT') if bot else 0
    loans = []
    discrepancies = None
    if bot:
        try:
            wb = bot.trader.get_wallet_balance()
            if wb.get('retCode') == 0:
                for c in wb['result']['list'][0]['coin']:
                    amt = float(c.get('borrowAmount', 0) or 0)
                    if amt > 0:
                        loans.append({'coin': c.get('coin', ''), 'amount': amt})
        except Exception as e:
            print(f"⚠️ /balances: не удалось получить займы: {e}")
        try:
            discrepancies = bot.check_discrepancies()
        except Exception as e:
            discrepancies = {'error': str(e), 'orphan_assets': [], 'orphan_orders': []}
    return render_template('balances.html', data=current_data,
                           free_usdt=free_usdt, loans=loans,
                           discrepancies=discrepancies)


@app.route('/api/repay_loan', methods=['POST'])
def api_repay_loan():
    """Погасить займ по одной монете (Spot Margin)."""
    bot  = _get_bot()
    if not bot:
        return jsonify({'success': False, 'error': 'Бот не инициализирован'})
    data   = request.json or {}
    coin   = data.get('coin', '').strip().upper()
    amount = str(data.get('amount', '')).strip()
    if not coin:
        return jsonify({'success': False, 'error': 'Не указана монета'})
    try:
        mode = 'REAL' if bot.real_mode else 'DEMO'
        
        print(f"\n[REPAY_DIAGNOSTIC] Вызов Погасить {coin}")
        
        # Диагностика 1: текущий баланс и долг
        wb = bot.trader.get_wallet_balance()
        if wb.get('retCode') != 0:
            return jsonify({
                'success': False,
                'error': f'Ошибка get_wallet_balance: {wb.get("retMsg")}',
                'diagnostic': {'step': 'balance_check_failed'}
            })
        
        coin_info = None
        for ci in wb['result']['list'][0]['coin']:
            if ci.get('coin') == coin:
                coin_info = ci
                break
        
        if not coin_info:
            return jsonify({
                'success': False,
                'error': f'{coin} не найден в балансе',
                'diagnostic': {'step': 'coin_not_in_wallet'}
            })
        
        spot_balance = float(coin_info.get('walletBalance', 0) or 0)
        borrow_amt = float(coin_info.get('borrowAmount', 0) or 0)
        available = float(coin_info.get('availableToWithdraw', 0) or 0)
        
        print(f"  Баланс {coin}:")
        print(f"    walletBalance={spot_balance:.8f}")
        print(f"    borrowAmount={borrow_amt:.8f}")
        print(f"    availableToWithdraw={available:.8f}")
        
        # Попытка погашения — сначала no-convert-repay
        req_data = {"coin": coin, "repaymentType": "FLEXIBLE"}
        print(f"  Попытка 1: no-convert-repay (без конвертации)")
        
        r = bot.trader._request("POST", "/v5/account/no-convert-repay", req_data)
        print(f"  response: retCode={r.get('retCode')}, retMsg={r.get('retMsg')}")
        
        # Fallback: если ошибка 34022044 (нет монеты на споте), пробуем обычный repay (конвертирует)
        if r.get('retCode') == 34022044:
            print(f"  ⚠️ Ошибка 34022044 (нет {coin} на споте)")
            print(f"  Попытка 2: обычный /account/repay (с конвертацией других активов)")
            r = bot.trader._request("POST", "/v5/account/repay",
                                    {"coin": coin})
            print(f"  response: retCode={r.get('retCode')}, retMsg={r.get('retMsg')}")
        
        if r.get('retCode') == 0:
            status = r.get('result', {}).get('resultStatus', '?')
            msg = f"Погашение {coin} отправлено (статус {status})"
            print(f"  ✅ {msg}\n")
            return jsonify({
                'success': True,
                'message': msg,
                'diagnostic': {
                    'coin': coin,
                    'mode': mode,
                    'status': status,
                    'spot_balance_before': spot_balance,
                    'borrow_before': borrow_amt
                }
            })
        
        err = r.get('retMsg', 'Ошибка биржи')
        print(f"  ❌ {err}\n")
        return jsonify({
            'success': False,
            'error': err,
            'diagnostic': {
                'coin': coin,
                'retCode': r.get('retCode'),
                'retMsg': err,
                'spot_balance': spot_balance,
                'borrow_amount': borrow_amt,
                'available': available,
                'reason': 'repay_api_failed_both_attempts'
            }
        })
        
    except Exception as e:
        print(f"  💥 Исключение: {e}\n")
        return jsonify({
            'success': False,
            'error': str(e),
            'diagnostic': {
                'coin': coin,
                'error_type': type(e).__name__,
                'error_msg': str(e)
            }
        })


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
    if symbol in bot.short_positions:
        return jsonify({'success': False, 'error': f'{symbol}: уже есть открытый шорт'})

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


@app.route('/api/sell_short', methods=['POST'])
def api_sell_short():
    """Ручное открытие шорта через дашборд (Spot Margin: занять → продать)."""
    if not current_data.get('trading_allowed', False):
        return jsonify({'success': False, 'error': 'Торговля выключена'})
    bot = _get_bot()
    if not bot:
        return jsonify({'success': False, 'error': 'Бот не инициализирован'})
    if not bot.shorts_enabled:
        return jsonify({'success': False, 'error': 'Шорты выключены — включите в /settings'})

    data     = request.json or {}
    symbol   = data.get('symbol', '').strip().upper()
    usdt_amt = float(data.get('amount', 0))
    tp_pct   = float(data.get('tp_pct', 3.5))
    sl_pct   = float(data.get('sl_pct', 3.0))

    if not symbol or usdt_amt <= 0:
        return jsonify({'success': False, 'error': 'Неверные параметры'})
    if symbol in bot.short_positions:
        return jsonify({'success': False, 'error': f'{symbol}: шорт уже открыт'})
    if bot.portfolio.is_open(symbol):
        return jsonify({'success': False, 'error': f'{symbol}: уже есть открытый лонг'})

    if len(bot.short_positions) >= bot.portfolio.position_divider:
        return jsonify({'success': False,
                        'error': f'Достигнут лимит шортов ({bot.portfolio.position_divider})'})

    print(f"\n📥 /api/sell_short: {symbol} | {usdt_amt} USDT | TP={tp_pct}% SL={sl_pct}%")
    ok = bot.open_short(symbol, {'confidence': 0}, usdt_amount=usdt_amt, tp_pct=tp_pct, sl_pct=sl_pct)

    if ok:
        return jsonify({'success': True})
    return jsonify({'success': False, 'error': 'Не удалось открыть шорт — см. лог бота'})


@app.route('/api/close_short', methods=['POST'])
def api_close_short():
    """Закрыть одну шорт-позицию (выкуп + автопогашение займа биржей)."""
    bot    = _get_bot()
    data   = request.json or {}
    symbol = data.get('symbol', '').strip().upper()
    if not bot or not symbol:
        return jsonify({'ok': False, 'message': 'Нет бота или символа'})
    if symbol not in bot.short_positions:
        return jsonify({'ok': False, 'message': f'Шорт {symbol} не найден'})
    try:
        bot.close_short(symbol, 'MANUAL')
        still_open = symbol in bot.short_positions
        return jsonify({
            'ok': not still_open,
            'message': f'{symbol} закрыт' if not still_open else 'Не удалось закрыть — см. лог бота',
        })
    except Exception as e:
        return jsonify({'ok': False, 'message': str(e)})


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
            qty_dec = abs(Decimal(str(step)).as_tuple().exponent)
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
                qty_dec = abs(Decimal(str(step)).as_tuple().exponent)
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

    for symbol in list(bot.short_positions.keys()):
        try:
            bot.close_short(symbol, 'MANUAL')
            if symbol not in bot.short_positions:
                closed.append(f"{symbol} (шорт)")
            else:
                errors.append(f"{symbol} (шорт): не закрылся — см. лог")
        except Exception as e:
            errors.append(f"{symbol} (шорт): {e}")

    msg = f"Закрыты: {closed}" + (f"  Ошибки: {errors}" if errors else "")
    return jsonify({'ok': True, 'message': msg})


@app.route('/api/update_settings', methods=['POST'])
def api_update_settings():
    """Обновляет глобальные параметры стратегии."""
    data = request.json or {}
    bot  = _get_bot()

    changed = []
    for key in ('tp_pct', 'sl_pct', 'min_confidence', 'short_min_confidence',
                'max_positions', 'position_divider', 'breakeven_trigger', 'trailing_pct'):
        val = data.get(key)
        if val is None:
            continue
        try:
            fval = float(val)
            if key == 'tp_pct':           current_data['settings_tp'] = fval
            elif key == 'sl_pct':         current_data['settings_sl'] = fval
            elif key == 'min_confidence': current_data['min_confidence'] = fval
            elif key == 'short_min_confidence' and bot:
                bot.SHORT_MIN_CONFIDENCE = fval
                current_data['short_min_confidence'] = fval
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


@app.route('/api/auto_fix_discrepancies', methods=['POST'])
def api_auto_fix_discrepancies():
    """Авто-починка несоответствий: навешивает TP/SL на голые LONG-активы, отменяет ордера-сироты."""
    bot = _get_bot()
    if not bot:
        return jsonify({'success': False, 'error': 'Бот не инициализирован'})
    try:
        report = bot.auto_fix_discrepancies()
        return jsonify({'success': True, 'report': report})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})


@app.route('/api/test_repay_clean', methods=['POST'])
def api_test_repay_clean():
    """
    Чистый эксперимент: берёт займ TIA на 15 USDT, ждёт и пытается погасить.
    Полное логирование каждого шага для отладки механизма погашения.
    """
    bot = _get_bot()
    if not bot:
        return jsonify({'success': False, 'error': 'Бот не инициализирован'})
    if not bot.real_mode:
        return jsonify({'success': False, 'error': 'Только на REAL режиме'})
    
    test_coin = 'TIA'
    borrow_amt = 15.0
    log_lines = []
    
    def log(msg):
        """Логируем в консоль и собираем в список."""
        print(f"[TEST_REPAY] {msg}")
        log_lines.append(msg)
    
    try:
        log(f"🧪 СТАРТ: берём займ {test_coin} {borrow_amt} USDT → гасим весь займ")
        
        # ── Шаг 1: получить текущий баланс/долг ──
        log("\n1️⃣ Получаем текущий баланс:")
        wb0 = bot.trader.get_wallet_balance()
        if wb0.get('retCode') != 0:
            log(f"❌ Ошибка get_wallet_balance: {wb0.get('retMsg')}")
            return jsonify({'success': False, 'error': wb0.get('retMsg'), 'log': log_lines})
        
        initial_borrow = 0.0
        for ci in wb0['result']['list'][0]['coin']:
            if ci.get('coin') == test_coin:
                initial_borrow = float(ci.get('borrowAmount') or 0)
                log(f"   {test_coin}: borrowAmount={initial_borrow:.8f}")
                break
        
        # ── Шаг 2: берём займ ──
        log(f"\n2️⃣ Берём займ через POST /v5/account/borrow:")
        borrow_resp = bot.trader._request("POST", "/v5/account/borrow",
                                          {"coin": test_coin, "amount": str(borrow_amt)})
        log(f"   retCode: {borrow_resp.get('retCode')}")
        log(f"   retMsg: {borrow_resp.get('retMsg')}")
        if borrow_resp.get('retCode') != 0:
            log(f"❌ Ошибка заёма: {borrow_resp.get('retMsg')}")
            return jsonify({'success': False, 'error': borrow_resp.get('retMsg'), 'log': log_lines})
        
        # ── Шаг 3: проверяем новый баланс ──
        log(f"\n3️⃣ Проверяем баланс после заёма:")
        wb1 = bot.trader.get_wallet_balance()
        if wb1.get('retCode') != 0:
            log(f"❌ Ошибка get_wallet_balance: {wb1.get('retMsg')}")
            return jsonify({'success': False, 'error': wb1.get('retMsg'), 'log': log_lines})
        
        after_borrow = 0.0
        for ci in wb1['result']['list'][0]['coin']:
            if ci.get('coin') == test_coin:
                after_borrow = float(ci.get('borrowAmount') or 0)
                wallet_bal = float(ci.get('walletBalance') or 0)
                available = float(ci.get('availableToWithdraw') or 0)
                log(f"   {test_coin}: borrowAmount={after_borrow:.8f}, walletBalance={wallet_bal:.8f}, available={available:.8f}")
                break
        
        # ── Шаг 4: ждём settlement ──
        log(f"\n4️⃣ Пауза 1.5 сек (settlement lag):")
        time.sleep(1.5)
        log(f"   ⏳ готово")
        
        # ── Шаг 5: пытаемся погасить ──
        log(f"\n5️⃣ Вызываем POST /v5/account/no-convert-repay (repaymentType=FLEXIBLE):")
        repay_req = {"coin": test_coin, "repaymentType": "FLEXIBLE"}
        log(f"   request: {repay_req}")
        
        repay_resp = bot.trader._request("POST", "/v5/account/no-convert-repay", repay_req)
        log(f"   retCode: {repay_resp.get('retCode')}")
        log(f"   retMsg: {repay_resp.get('retMsg')}")
        log(f"   result: {repay_resp.get('result', {})}")
        
        if repay_resp.get('retCode') != 0:
            log(f"❌ Ошибка погашения: {repay_resp.get('retMsg')}")
        else:
            status = repay_resp.get('result', {}).get('resultStatus', '?')
            log(f"✓ Погашение отправлено (статус={status})")
        
        # ── Шаг 6: финальная проверка баланса ──
        log(f"\n6️⃣ Финальная проверка баланса:")
        time.sleep(0.5)
        wb2 = bot.trader.get_wallet_balance()
        if wb2.get('retCode') == 0:
            for ci in wb2['result']['list'][0]['coin']:
                if ci.get('coin') == test_coin:
                    final_borrow = float(ci.get('borrowAmount') or 0)
                    wallet_bal = float(ci.get('walletBalance') or 0)
                    available = float(ci.get('availableToWithdraw') or 0)
                    log(f"   {test_coin}: borrowAmount={final_borrow:.8f}, walletBalance={wallet_bal:.8f}, available={available:.8f}")
                    break
        
        # ── Итоги ──
        log(f"\n✅ ИТОГ:")
        log(f"   Изначальный долг: {initial_borrow:.8f}")
        log(f"   После заёма: {after_borrow:.8f}")
        log(f"   После погашения: {final_borrow:.8f if wb2.get('retCode') == 0 else '?'}")
        log(f"   Ожидаемо долг {test_coin} вырос на ~{borrow_amt:.2f}")
        
        return jsonify({'success': True, 'log': log_lines})
        
    except Exception as e:
        log(f"💥 Исключение: {e}")
        import traceback
        log(traceback.format_exc())
        return jsonify({'success': False, 'error': str(e), 'log': log_lines})


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
