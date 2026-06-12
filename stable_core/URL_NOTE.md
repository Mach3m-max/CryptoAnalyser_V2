# ВАЖНО: URL для DEMO-режима Bybit

## Правильный URL

| Режим | URL | Параметр в коде |
|-------|-----|-----------------|
| **DEMO** | `https://api-demo.bybit.com` | `testnet=True` |
| **REAL** | `https://api.bybit.com` | `testnet=False` |

## Что такое api-demo.bybit.com

Bybit в 2024 году ввёл **Demo Trading** — виртуальный аккаунт на основном домене (`bybit.com`),
отдельный от старого testnet. Ключевые отличия:

| | Demo Trading | Testnet (устаревший) |
|---|---|---|
| URL API | `api-demo.bybit.com` | `api-testnet.bybit.com` |
| Регистрация | В основном аккаунте bybit.com | Отдельный аккаунт |
| Маркеты | Реальные котировки | Тестовые данные |
| Статус | ✅ Актуальный | ⚠️ Устаревший |

## Почему параметр называется `testnet`

В `bybit_client.py` параметр `testnet: bool` — это **legacy название** из ранней версии бота,
когда использовался реальный testnet. Сейчас параметр управляет выбором между
`api-demo.bybit.com` и `api.bybit.com`, но название оставлено для обратной совместимости.

```python
# bybit_client.py (stable_core)
self.base_url = "https://api-demo.bybit.com" if testnet else "https://api.bybit.com"
```

## Использование в v2

```python
from stable_core.bybit_client import BybitTrader

# DEMO режим
trader_demo = BybitTrader(demo_api_key, demo_api_secret, testnet=True)
# → base_url = https://api-demo.bybit.com  ✅

# REAL режим  
trader_real = BybitTrader(real_api_key, real_api_secret, testnet=False)
# → base_url = https://api.bybit.com  ✅
```

## Что проверить при смене ключей

При вводе новых API-ключей через UI дашборда — проверить что ключи созданы
в правильном разделе аккаунта Bybit:

- **DEMO ключи**: Bybit → Demo Trading → API Management
- **REAL ключи**: Bybit → Main Account → API Management

Ключи от Demo Trading **не работают** с `api.bybit.com` и наоборот.
