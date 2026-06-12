#!/usr/bin/env python3
# test_startup.py
# Запускай из D:\Phyton\CryptoAnalyzer_V_2\
#   python test_startup.py

import sys
import os
ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)

OK   = "OK  "
FAIL = "FAIL"
WARN = "WARN"
results = []

def check(name, fn):
    try:
        fn()
        results.append((OK, name))
        print(f"  [OK]  {name}")
    except Exception as e:
        results.append((FAIL, f"{name}: {e}"))
        print(f"  [!!]  {name}: {e}")

def warn(name, fn):
    try:
        fn()
        results.append((OK, name))
        print(f"  [OK]  {name}")
    except Exception as e:
        results.append((WARN, name))
        print(f"  [--]  {name}: {e}  (не критично)")

print("=" * 60)
print("  HTT v2 -- Test Startup")
print("=" * 60)
print()

# 1. Импорты
print("1. Imports:")
check("config (shim)",           lambda: __import__('config'))
check("config.app_config",       lambda: __import__('config.app_config'))
check("pairs_config.json",
      lambda: __import__('json').load(
          open(os.path.join(ROOT, 'config', 'pairs_config.json'))))
check("bybit_client (shim)",     lambda: __import__('bybit_client'))
check("stable_core.bybit_client",
      lambda: __import__('stable_core.bybit_client', fromlist=['BybitTrader']))
check("stable_core.order_executor",
      lambda: __import__('stable_core.order_executor', fromlist=['OrderExecutor']))
check("portfolio_manager",       lambda: __import__('portfolio_manager'))
check("security.key_manager",    lambda: __import__('security.key_manager'))
check("logging_v2",              lambda: __import__('logging_v2'))

for mod in ('strategy_engine', 'data_loader', 'signal_cache'):
    check(f"v1 module: {mod}", lambda m=mod: __import__(m))

warn("ml.ml_strategy_engine",
     lambda: __import__('ml.ml_strategy_engine', fromlist=['MLStrategyEngine']))
print()

# 2. Пары
print("2. Pairs config:")
def _check_pairs():
    import json
    with open(os.path.join(ROOT, 'config', 'pairs_config.json')) as f:
        cfg = json.load(f)
    demo = [s for s, c in cfg.items() if c.get('demo_enabled')]
    real = [s for s, c in cfg.items() if c.get('real_enabled')]
    print(f"  [OK]  Total: {len(cfg)}  DEMO: {len(demo)}  REAL: {len(real)}")
    print(f"        DEMO pairs: {demo[:5]}...")
_check_pairs()
print()

# 3. Security
print("3. Security (AES-256-GCM):")
def _check_security():
    from security.key_manager import save_credentials, load_credentials, delete_credentials
    ok = save_credentials('demo', 'TEST_KEY_123', 'TEST_SECRET_456', 'test_password')
    assert ok
    creds = load_credentials('demo', 'test_password')
    assert creds and creds['key'] == 'TEST_KEY_123'
    delete_credentials('demo')
    print("  [OK]  Encrypt/decrypt OK")
check("key_manager", _check_security)
print()

# 4. API (опционально -- может не работать на D:\ из-за сети)
print("4. Bybit DEMO API (optional -- network may not reach demo API from this machine):")
def _check_api():
    import requests
    r = requests.get(
        "https://api-demo.bybit.com/v5/market/tickers",
        params={"category": "spot", "symbol": "BTCUSDT"},
        timeout=5
    )
    assert r.status_code == 200
    price = float(r.json()['result']['list'][0]['lastPrice'])
    print(f"  [OK]  BTC/USDT @ {price:,.2f}")
warn("api-demo.bybit.com", _check_api)
print()

# 5. Portfolio
print("5. PortfolioManager:")
def _check_pm():
    from portfolio_manager import PortfolioManager
    pm = PortfolioManager(total_capital=1000.0, max_positions=3, position_divider=3)
    assert pm.get_order_amount() == 333.33
    pm.open_position("INJUSDT", 333.33, 5.15)
    assert pm.is_open("INJUSDT")
    pm.close_position("INJUSDT")
    assert not pm.is_open("INJUSDT")

    # FIX v2: open_position_from_dashboard
    pm.open_position_from_dashboard("SUIUSDT", 333.33, 0.75)
    assert pm.is_open("SUIUSDT")
    pos = pm.open_positions["SUIUSDT"]
    assert pos['source'] == 'dashboard'
    print("  [OK]  open/close/can_open/dashboard_fix OK")
check("portfolio_manager", _check_pm)
print()

# 6. Config shim
print("6. Config shim (v1 compatibility):")
def _check_config_shim():
    from config import OPTIMAL_PARAMS, TECH_PARAMS, LOG_FLAGS, PORTFOLIO, PROJECT_INFO
    assert 'entry_threshold' in OPTIMAL_PARAMS
    assert 'sma_windows' in TECH_PARAMS
    assert isinstance(PORTFOLIO, dict) and len(PORTFOLIO) > 0
    assert len(PORTFOLIO) == 15
    assert PROJECT_INFO['version'] == '2.0.0'
    print(f"  [OK]  PORTFOLIO={len(PORTFOLIO)} pairs  VERSION={PROJECT_INFO['version']}")
check("config shim", _check_config_shim)
print()

# Summary
passed  = sum(1 for r in results if r[0] == OK)
failed  = sum(1 for r in results if r[0] == FAIL)
warned  = sum(1 for r in results if r[0] == WARN)

print("=" * 60)
print(f"  Result: {passed} OK  {failed} FAILED  {warned} WARN")

if failed == 0:
    print()
    print("  All checks passed!")
    print()
    print("  Next steps:")
    print("    1. Enter keys:   python security/key_manager.py")
    print("    2. Start DEMO:   python main.py")
    print("    3. Dashboard:    http://localhost:5000")
else:
    print()
    print("  Fix errors above before running the bot.")
    for r in results:
        if r[0] == FAIL:
            print(f"    FAILED: {r[1]}")
print("=" * 60)
