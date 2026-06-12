# Harmonic Trading Terminal v2

Автоматический торговый бот для Bybit Spot (DEMO + REAL).

## Быстрый старт

### 1. Установка зависимостей
pip install -r requirements.txt

### 2. Первый запуск
python main.py

При первом запуске введи мастер-пароль и API-ключи через браузер.

### 3. Режимы
python main.py           # DEMO localhost:5000
python main.py --real    # REAL localhost:5001

### 4. Подготовка ML-моделей
python download_history.py
python compute_historical_indicators.py
python ml/train.py
python simulator.py

## Структура проекта

| Папка | Назначение |
|---|---|
| bot/ | Ядро бота TradingBot |
| config/ | Конфигурация |
| security/ | Шифрование API-ключей AES-256-GCM |
| dashboard/ | Веб-дашборд Flask |
| logging_v2/ | Посуточное логирование |
| analysis/ | История сделок, графики |
| ml/ | ML-стратегия, обучение моделей |
| stable_core/ | Bybit API клиент |
| templates/ | HTML шаблоны дашборда |

## Безопасность

API-ключи хранятся зашифрованными AES-256-GCM.
Открытый текст никогда не записывается на диск.
Ввод ключей только через страницу настроек дашборда.
