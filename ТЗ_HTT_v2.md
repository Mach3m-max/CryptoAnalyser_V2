# Техническое задание  
# Harmonic Trading Terminal — Версия 2  
### На базе HTT v3.2.0 (BotByBit)

---

**Репозиторий:** https://github.com/Mach3m-max/BotByBit  
**Платформа:** Bybit Spot (DEMO + REAL)  
**Python:** 3.8+  
**Версия документа:** 1.0  
**Дата:** 2026-06  

---

## Содержание

1. [Общее описание](#1-общее-описание)  
2. [Принципы перехода от v1 к v2](#2-принципы-перехода-от-v1-к-v2)  
3. [Блок API-ключей и безопасности](#3-блок-api-ключей-и-безопасности)  
4. [Блок логирования](#4-блок-логирования)  
5. [Блок анализа](#5-блок-анализа)  
6. [Торговые инструменты (пары)](#6-торговые-инструменты-пары)  
7. [Дашборд](#7-дашборд)  
8. [Модульный состав v2](#8-модульный-состав-v2)  
9. [Структура файлов и директорий](#9-структура-файлов-и-директорий)  
10. [Параметры стратегии](#10-параметры-стратегии)  
11. [README и документация](#11-readme-и-документация)  
12. [Зависимости](#12-зависимости)  

---

## 1. Общее описание

Harmonic Trading Terminal v2 (HTT v2) — автоматический торговый бот для платформы Bybit Spot. Система работает одновременно в двух режимах: DEMO (виртуальный баланс, testnet API) и REAL (реальный счёт), используя единый блок стратегии и анализа.

### Ключевые характеристики

| Характеристика | Значение |
|---|---|
| Биржа | Bybit Spot (UTA) |
| Режимы | DEMO (testnet) + REAL (mainnet) |
| Стратегия | ML: XGBoost / RandomForest + SMA-голосование |
| Направления | Лонг (Spot Buy) + Шорт (Spot Margin) |
| Инструменты | Произвольный набор пар, настраивается в конфиге |
| Интерфейс | Веб-дашборд (Flask), DEMO :5000 / REAL :5001 |
| Анализ | Визуализация графиков, ручная разметка, бэктест |

### Что добавляет v2 по сравнению с v1

- Безопасное хранение API-ключей (шифрование, ввод через UI)
- Посуточное логирование с разделением по типам данных
- Многостраничный дашборд с раздельными DEMO/REAL серверами
- Блок визуализации с графиком в стиле биржи, тайм-фреймами, индикаторами и ручными метками
- Произвольное подключение пар без изменения кода
- Исправление всех накопленных багов v1 (Ghost, detect_close_reason, trail, dashboard place_order)

---

## 2. Принципы перехода от v1 к v2

**Главное правило:** модули, отвечающие за физическое исполнение ордеров на бирже (`bybit_client.py`, `place_order()`, OCO-логика), переносятся в v2 **без изменений** — они отлажены и безопасны. Переработке подвергаются только обвязка, интерфейс и аналитика.

| Категория | Что происходит |
|---|---|
| Ядро исполнения ордеров | Перенос без изменений |
| Логика стратегии и ML | Перенос с минимальными правками |
| Мониторинг позиций | Перенос + исправление известных багов |
| Логирование | Полная переработка (посуточное, типизированное) |
| Дашборд | Полная переработка (многостраничный, 2 сервера) |
| Конфигурация пар | Расширение (DEMO/REAL флаги, произвольное подключение) |
| API-ключи | Новый модуль (шифрование, UI-ввод) |
| Визуализация / анализ | Новый модуль |

---

## 3. Блок API-ключей и безопасности

### 3.1 Требования

- API-ключи (key + secret) для DEMO и REAL хранятся **зашифрованными** — открытый текст не записывается на диск.
- Ввод ключей только через веб-интерфейс (страница настроек дашборда); прямое редактирование JSON-файлов недопустимо для prod-режима.
- Мастер-пароль (passphrase) задаётся при первом запуске и используется как ключ шифрования.
- При смене ключей старые затираются (overwrite, не append).

### 3.2 Схема хранения

```
credentials/
├── demo.enc    — зашифрованный DEMO API key/secret
└── real.enc    — зашифрованный REAL API key/secret
```

- Формат файла: AES-256-GCM (через библиотеку `cryptography`).
- Нив в коем случае не хранить открытые ключи в config.json, env-файлах или git-репозитории.
- `.gitignore` обязан включать `credentials/`.

### 3.3 Модуль `key_manager.py` (новый)

| Функция | Описание |
|---|---|
| `set_master_password(pwd)` | Установка/смена мастер-пароля (KDF → ключ шифрования) |
| `save_credentials(mode, key, secret)` | Шифрование и запись на диск |
| `load_credentials(mode)` | Расшифровка и возврат в память (не на диск) |
| `credentials_exist(mode)` | Проверка наличия файла |
| `rotate_credentials(mode, key, secret)` | Замена ключей (старый файл перезаписывается) |

### 3.4 UI-поток ввода ключей

1. При первом запуске дашборд показывает форму «Введите мастер-пароль».
2. После ввода пароля — форма API-ключей (отдельно для DEMO и REAL).
3. Ключи шифруются и сохраняются; форма закрывается, бот стартует.
4. Повторный ввод — только при смене ключей через страницу «Настройки».

---

## 4. Блок логирования

### 4.1 Принципы

- Все лог-файлы посуточные: `YYYY-MM-DD` в имени файла.
- Три независимых потока данных: **рыночные данные**, **сигналы/индикаторы**, **действия бота**.
- Исторические данные (свечи для обучения) хранятся отдельно от оперативных логов.
- Запись через `threading.Lock()` (как в v1) для защиты от race condition.

### 4.2 Структура логов

```
data/
├── candles/
│   └── {PAIR}/
│       └── {PAIR}_2026-06-11.csv     ← минутные свечи (OHLCV), посуточно
│
├── indicators/
│   └── {PAIR}/
│       └── {PAIR}_ind_2026-06-11.csv ← RSI, BB, ATR, SMA-отклонения, посуточно
│
├── signals/
│   └── {PAIR}/
│       └── {PAIR}_sig_2026-06-11.csv ← сигналы ML + SMA-голосование, посуточно
│
├── decisions/
│   └── decisions_2026-06-11.csv      ← все торговые решения, посуточно
│
└── historical/
    └── {PAIR}/
        └── {PAIR}_Xd.csv             ← данные для обучения ML (не ротируются)

logs/
└── bot_2026-06-11_HH-MM.log          ← действия бота (текстовый лог), посуточно
```

### 4.3 Поля файлов

**candles / `{PAIR}_YYYY-MM-DD.csv`**

| Поле | Тип | Описание |
|---|---|---|
| timestamp | datetime | Время свечи (UTC) |
| open, high, low, close | float | OHLC |
| volume | float | Объём |
| pair | str | Символ пары |

**indicators / `{PAIR}_ind_YYYY-MM-DD.csv`**

| Поле | Тип | Описание |
|---|---|---|
| timestamp | datetime | |
| rsi_7, rsi_14, rsi_21 | float | RSI |
| bb_upper, bb_mid, bb_lower | float | Bollinger Bands |
| atr_14 | float | ATR |
| macd, macd_signal | float | MACD |
| sma_50..200 | float | Скользящие средние |
| dev_50..200 | float | Отклонения от SMA |
| obv | float | OBV |

**signals / `{PAIR}_sig_YYYY-MM-DD.csv`**

| Поле | Тип | Описание |
|---|---|---|
| timestamp | datetime | |
| signal | BUY/SELL/HOLD | Итоговый сигнал ML |
| confidence | 0.0–1.0 | Уверенность модели |
| buy_votes, sell_votes | int | Голоса SMA |
| avg_deviation | float | Среднее отклонение |
| model_used | str | xgboost / rf |

**decisions / `decisions_YYYY-MM-DD.csv`**

Поля аналогичны v1 (`decisions.csv`), но добавлены:

| Новое поле | Описание |
|---|---|
| mode | DEMO / REAL |
| close_reason | TP / SL / TRAIL / MANUAL / UNKNOWN |
| pnl_pct | PnL в % от входа |
| pnl_usdt | PnL в USDT с учётом комиссий |

**Текстовый лог бота `logs/bot_YYYY-MM-DD_HH-MM.log`**

Аналогичен v1 — все `print()` и критические события, с уровнями INFO / WARNING / ERROR.

### 4.4 Ротация и очистка

- Оперативные логи (candles, indicators, signals): хранить последние 90 дней, старше удалять автоматически при старте бота.
- `historical/`: не ротируются, управляются вручную.
- `decisions/`: хранить бессрочно (малый объём).

---

## 5. Блок анализа

### 5.1 Набор торговых инструментов (5.1 ТЗ пользователя)

- Пары подключаются через конфигурационный файл `pairs_config.json` (не хардкодятся в коде).
- Для каждой пары указывается: включена ли в DEMO, в REAL, параметры TP/SL/min_conf.
- Добавление новой пары = запись в `pairs_config.json` + запуск `download_history.py` + обучение модели.
- Бот при старте читает `pairs_config.json` и формирует активный список пар динамически.

### 5.2 Моделирование исторических данных (5.2 ТЗ пользователя)

Модуль `analysis/historical_engine.py` (новый, на базе `simulator.py` v1):

| Функция | Описание |
|---|---|
| `load_history(pair, date_from, date_to)` | Загрузка свечей из `data/historical/` или `data/candles/` |
| `compute_indicators(df)` | Расчёт всех 51 индикатора (перенос из `compute_historical_indicators.py`) |
| `run_signals(df, model)` | Прогон ML-модели по историческим данным |
| `simulate(df, params)` | Симуляция сделок с TP/SL/trail (перенос из `simulator.py`) |
| `export_labeled(pair)` | Экспорт DataFrame с метками для переобучения |

### 5.3 Создание и управление моделями (5.3 ТЗ пользователя)

Модуль `ml/trainer.py` (расширение `train.py` v1):

| Функция | Описание |
|---|---|
| `train_pair(pair, days, tp, sl, lookahead)` | Обучение модели для одной пары |
| `train_all(config)` | Обучение всех активных пар из конфига |
| `evaluate(pair)` | F1-метрика, confusion matrix, feature importance |
| `promote_to_production(pair)` | Копирование модели из `staging/` в `production/` |
| `rollback(pair)` | Откат к предыдущей версии модели |

**Директории моделей:**

```
ml/
├── staging/         ← только что обученные (не в работе)
│   └── {PAIR}_model.pkl
├── production/      ← модели, используемые ботом
│   └── {PAIR}_model.pkl
└── archive/         ← предыдущие версии (для rollback)
    └── {PAIR}_{timestamp}_model.pkl
```

### 5.4 Блок визуализации (5.4 ТЗ пользователя)

Отдельная страница дашборда `/chart` — интерактивный график в стиле биржи.

**Возможности:**

| Функция | Описание |
|---|---|
| Свечной график | Японские свечи по историческим данным пары |
| Тайм-фреймы | 1m, 5m, 15m, 1h, 4h, 1d (агрегация из минутных свечей) |
| Индикаторы | SMA 50/100/200, BB, RSI, MACD, ATR, Volume — включаются/выключаются |
| Сигналы модели | Маркеры BUY/SELL/HOLD на графике (расчётные) |
| Реальные ордера | Реальные входы/выходы из `decisions.csv` на графике |
| Ручные метки | Инструмент: клик на свечу → выбор метки (BUY/SELL/HOLD/REMOVE) |
| Экспорт меток | Сохранение массива ручных меток в `labels/{PAIR}_manual.json` |
| Дообучение | Кнопка «Переобучить с ручными метками» → запуск `trainer.py` |

**Технологический стек визуализации:**

- Backend: существующий Flask (дашборд)
- Chart library: **Lightweight Charts** (TradingView, MIT, JS) — наиболее близкий к биржевому виду, не требует сервера данных
- Данные передаются через REST API `/api/chart?pair=INJUSDT&tf=5m&from=...&to=...`

---

## 6. Торговые инструменты (пары)

### 6.1 Конфигурация пар `pairs_config.json`

```json
{
  "INJUSDT": {
    "demo_enabled": true,
    "real_enabled": false,
    "group": "growth",
    "weight": 0.07,
    "tp_pct": 3.5,
    "sl_pct": 3.0,
    "min_conf": 0.60,
    "breakeven_trigger": 2.5,
    "trailing_pct": 1.5,
    "sell_close_conf": 0.70,
    "note": "F1=0.559, xgb, 432d"
  },
  "BTCUSDT": {
    "demo_enabled": true,
    "real_enabled": true,
    "group": "core",
    "weight": 0.20,
    "tp_pct": 2.5,
    "sl_pct": 2.0,
    "min_conf": 0.70,
    "breakeven_trigger": 1.5,
    "trailing_pct": 1.0,
    "sell_close_conf": 0.75,
    "note": "F1=0.617, rf, 180d"
  }
}
```

### 6.2 Поля конфигурации пар

| Поле | Тип | По умолчанию | Описание |
|---|---|---|---|
| `demo_enabled` | bool | true | Торговать в DEMO |
| `real_enabled` | bool | false | Торговать в REAL |
| `group` | str | — | core / growth / hedge |
| `weight` | float | — | Вес в портфеле (сумма = 1.0) |
| `tp_pct` | float | 3.5 | Take Profit % |
| `sl_pct` | float | 3.0 | Stop Loss % |
| `min_conf` | float | 0.60 | Минимальная уверенность ML |
| `breakeven_trigger` | float | 2.5 | Порог активации безубытка % |
| `trailing_pct` | float | 1.5 | Отступ трейлинг SL от максимума % |
| `sell_close_conf` | float | 0.70 | Порог досрочного выхода по SELL |
| `note` | str | — | Произвольная заметка (результат обучения) |

### 6.3 Добавление новой пары (процедура)

1. Добавить запись в `pairs_config.json` с `demo_enabled: true, real_enabled: false`.
2. Запустить `python download_history.py --pair NEWUSDT --days 365`.
3. Запустить `python compute_historical_indicators.py --pairs NEWUSDT`.
4. Запустить `python ml/trainer.py --pair NEWUSDT` → модель в `ml/staging/`.
5. Проверить F1 в отчёте. Если F1 > 0.51 — `python ml/trainer.py --promote NEWUSDT`.
6. Перезапустить бота.

---

## 7. Дашборд

### 7.1 Архитектура

- **Два независимых сервера Flask**: DEMO на порту 5000, REAL на порту 5001.
- Единый Python-процесс для обоих серверов (threading) или два отдельных процесса.
- Общий блок стратегии, ML и расчётов — один инстанс `TradingBot`, разделяемый обоими серверами.
- Транспорт: WebSocket (Flask-SocketIO) для real-time обновлений, REST для команд.

### 7.2 Страницы

#### Страница 1: Оперативный анализ (`/`)

| Блок | Содержимое |
|---|---|
| Заголовок | Режим (DEMO/REAL), баланс USDT, время последнего обновления |
| Таблица пар | По каждой активной паре: название (ссылка на биржу), текущая цена, сигнал ML, уверенность, отклонение от SMA, статус (в позиции / нет) |
| Кнопки действий | BUY (открыть позицию вручную), SELL (досрочный выход), CLOSE ALL |
| Тумблеры | Торговля вкл/выкл, Шорты вкл/выкл |
| Индикатор Cross Margin | Статус включения Spot Margin (предупреждение если выключен) |

Имя инструмента в таблице — гиперссылка вида:  
`https://www.bybit.com/ru-RU/trade/spot/INJ/USDT`

#### Страница 2: Открытые позиции (`/positions`)

| Колонка | Описание |
|---|---|
| Пара | Символ, ссылка на биржу |
| Направление | LONG / SHORT |
| Количество | qty монет |
| Цена входа | entry_price |
| Текущая цена | last_price |
| PnL % | Цвет: зелёный (>0) / красный (<0) / серый (=0) |
| PnL USDT | С учётом комиссий |
| Держим | Время с момента открытия |
| Действия | Кнопки: CLOSE (закрыть позицию), DETAILS |

#### Страница 3: История сделок (`/history`)

| Колонка | Описание |
|---|---|
| Дата/время | Время закрытия |
| Пара | Символ |
| Направление | LONG / SHORT |
| Причина закрытия | TP / SL / TRAIL / MANUAL — с цветовым кодом |
| Цена входа | entry_price |
| Цена выхода | close_price |
| Длительность | Время удержания |
| PnL % | Зелёный (прибыль) / Красный (убыток) |
| PnL USDT | С учётом комиссий |

Цветовая схема:
- TP → зелёный фон строки
- SL → красный фон
- TRAIL (прибыль) → светло-зелёный
- TRAIL (убыток) → светло-красный
- MANUAL → жёлтый

Фильтры: по паре, по периоду, по типу закрытия.  
Экспорт: кнопка «Скачать CSV».

#### Страница 4: Балансы (`/balances`)

| Блок | Содержимое |
|---|---|
| Итого | Общий баланс в USDT (свободный + в позициях) |
| По монетам | Список открытых инструментов: монета, кол-во, стоимость в USDT, доля портфеля |
| История баланса | Мини-график изменения баланса за последние 7 дней |

#### Страница 5: График (`/chart`)

Описание — в разделе 5.4.

#### Страница 6: Настройки (`/settings`)

| Блок | Содержимое |
|---|---|
| API-ключи | Форма ввода key/secret для DEMO и REAL (поля типа password), кнопка «Сохранить» |
| Глобальные параметры | max_positions, position_divider, ABS_RESERVE |
| Параметры по парам | Таблица: пара → TP / SL / min_conf / breakeven / trail / demo_enabled / real_enabled (inline-редактирование) |
| Управление моделями | Таблица пар с F1-метриками, кнопки «Переобучить», «Откатить», «Продвинуть» |

### 7.3 Кнопка «Закрыть все» (`CLOSE ALL`)

Присутствует на страницах 1 и 2. При нажатии:
1. Диалог подтверждения: «Вы уверены? Это закроет все открытые позиции по рынку.»
2. Отмена всех активных TP/SL/OCO ордеров по всем парам.
3. Market Sell всех монет с ненулевым балансом.
4. Очистка `portfolio_manager.open_positions`.
5. Запись в `decisions_*.csv` с `close_reason=MANUAL`.

### 7.4 REST API эндпоинты v2

| Метод | Путь | Описание |
|---|---|---|
| GET | `/api/status` | Полное состояние бота (JSON) |
| GET | `/api/positions` | Открытые позиции |
| GET | `/api/history` | История сделок с фильтрами |
| GET | `/api/balances` | Балансы |
| GET | `/api/signals` | Текущие сигналы по всем парам |
| GET | `/api/chart` | Данные для графика (OHLCV + индикаторы) |
| POST | `/api/buy` | Открыть позицию вручную |
| POST | `/api/sell` | Закрыть позицию досрочно |
| POST | `/api/close_all` | Закрыть все позиции |
| POST | `/api/settings` | Сохранить настройки |
| POST | `/api/credentials` | Сохранить API-ключи (шифрование) |
| POST | `/api/toggle_trading` | Вкл/выкл торговля |
| POST | `/api/toggle_shorts` | Вкл/выкл шорты |
| POST | `/api/labels` | Сохранить ручные метки |
| POST | `/api/train` | Запустить переобучение модели |

---

## 8. Модульный состав v2

### 8.1 Модули — перенос без изменений

| Модуль | Назначение |
|---|---|
| `bybit_client.py` | HTTP клиент Bybit API v5, HMAC-SHA256, ретраи |
| `strategy_engine.py` | SMA-голосование, feature generation |
| `ml_strategy_engine.py` | ML-стратегия, XGBoost/RF, predict_proba |
| `signal_cache.py` | Кэш сигналов (TTL 5 мин) |
| `data_loader.py` | Загрузка и инкрементальный кэш свечей |
| `download_history.py` | Скачивание исторических свечей |
| `compute_historical_indicators.py` | Вычисление 51 индикатора + авторазметка |
| `backtester.py` | Grid search параметров, HTML-отчёт |
| `simulator.py` | Быстрая симуляция стратегии |
| `retrain_best_period.py` | Подбор оптимального периода обучения |

### 8.2 Модули — переработка

| Модуль v1 | Модуль v2 | Что меняется |
|---|---|---|
| `main.py` | `bot/core.py` | Исправление багов (Ghost v1, detect_close_reason, trail, dosrochny exit), разделение на методы, поддержка двух режимов |
| `dashboard.py` | `dashboard/demo.py` + `dashboard/real.py` | Разделение на два сервера, многостраничная структура, REST API v2 |
| `strategy_logger.py` | `logging/decision_logger.py` | Посуточные файлы, поле close_reason, pnl_pct, pnl_usdt |
| `config.py` | `config/pairs_config.json` + `config/app_config.py` | Пары вынесены в JSON-файл, конфиг читается динамически |
| `portfolio_manager.py` | `bot/portfolio_manager.py` | Добавлен метод `open_position_from_dashboard()` (fix для ручного открытия), восстановление entry_price при рестарте |
| `indicator_logger.py` | `logging/indicator_logger.py` | Посуточные файлы по парам |
| `trade_history.py` | `analysis/trade_history.py` | Подтягивает decisions/ вместо одного файла |

### 8.3 Новые модули

| Модуль | Назначение |
|---|---|
| `security/key_manager.py` | Шифрование/расшифровка API-ключей (AES-256-GCM) |
| `logging/candle_logger.py` | Посуточная запись свечей в `data/candles/` |
| `dashboard/chart_api.py` | REST API для блока визуализации |
| `analysis/historical_engine.py` | Загрузка истории, расчёт индикаторов, симуляция |
| `ml/trainer.py` | Управление жизненным циклом моделей (staging → production → archive) |
| `templates/chart.html` | Страница графика с Lightweight Charts |
| `templates/history.html` | Страница истории сделок |
| `templates/balances.html` | Страница балансов |
| `templates/positions.html` | Страница открытых позиций |
| `templates/settings.html` | Страница настроек |

### 8.4 Удаляемые модули

| Модуль | Причина |
|---|---|
| `position_manager.py` | Устаревший legacy, не используется с v3.1 |
| `main110526.py` | Временный файл экспериментов, не нужен в v2 |
| `test_tpsl.py`, `test_tpsl2.py` | Одноразовые тесты, перенесены в `tests/` |

---

## 9. Структура файлов и директорий

```
CryptoAnalyzer_v2/
│
├── bot/
│   ├── core.py                    ← Основной цикл TradingBot
│   ├── portfolio_manager.py       ← Управление позициями
│   └── order_executor.py          ← Обёртка над bybit_client для ордеров
│
├── config/
│   ├── app_config.py              ← Глобальные константы (ABS_RESERVE, тайм-ауты)
│   └── pairs_config.json          ← Конфигурация пар (DEMO/REAL флаги, TP/SL)
│
├── security/
│   └── key_manager.py             ← Шифрование API-ключей
│
├── credentials/                   ← НЕ В GIT
│   ├── demo.enc
│   └── real.enc
│
├── logging/
│   ├── candle_logger.py
│   ├── indicator_logger.py
│   └── decision_logger.py
│
├── analysis/
│   ├── historical_engine.py
│   └── trade_history.py
│
├── ml/
│   ├── trainer.py
│   ├── ml_strategy_engine.py      ← без изменений
│   ├── staging/
│   ├── production/
│   └── archive/
│
├── dashboard/
│   ├── demo.py                    ← Flask :5000
│   ├── real.py                    ← Flask :5001
│   └── chart_api.py               ← REST для графика
│
├── templates/
│   ├── base.html
│   ├── index.html                 ← Оперативный анализ
│   ├── positions.html
│   ├── history.html
│   ├── balances.html
│   ├── chart.html
│   └── settings.html
│
├── static/
│   ├── css/
│   └── js/
│       └── lightweight-charts.js  ← TradingView Lightweight Charts
│
├── data/                          ← НЕ В GIT
│   ├── candles/
│   ├── indicators/
│   ├── signals/
│   ├── decisions/
│   └── historical/
│
├── labels/                        ← ручные метки
│   └── {PAIR}_manual.json
│
├── logs/                          ← НЕ В GIT
│
├── backtest_results/              ← НЕ В GIT
│
├── bybit_client.py                ← без изменений
├── strategy_engine.py             ← без изменений
├── data_loader.py                 ← без изменений
├── signal_cache.py                ← без изменений
├── compute_historical_indicators.py ← без изменений
├── download_history.py            ← без изменений
├── backtester.py                  ← без изменений
├── simulator.py                   ← без изменений
│
├── main.py                        ← точка входа (запуск обоих серверов)
├── requirements.txt
├── .gitignore
├── push.bat / pull.bat
└── README.md
```

---

## 10. Параметры стратегии

### 10.1 Глобальные константы (app_config.py)

| Параметр | Значение v1 | Рекомендация v2 | Описание |
|---|---|---|---|
| `ABS_RESERVE` | 5.50 USDT | 5.50 USDT | Минимальный свободный баланс |
| `BREAKEVEN_TRIGGER` | 1.0–1.5% | 2.5% | Порог активации безубытка |
| `TRAILING_PCT` | 1.0% | 1.5% | Отступ SL от максимума |
| `TRAILING_MIN_MOVE` | 0.3% | 0.3% | Минимальное движение для обновления trail |
| `SELL_CLOSE_CONF` | 0.70 | 0.70 | Порог досрочного выхода |
| `MAX_HOLD_BARS` | 1440 | 1440 | Принудительное закрытие через 24ч |
| `DEBOUNCE_SEC` | 240 сек | 240 сек | Антиспам по паре |

### 10.2 Исправления багов, обязательных для v2

| Баг | Где | Фикс |
|---|---|---|
| Ghost v1 | `monitor_positions()`, sync loop | `if sym in _sw_closing: continue` |
| Bug #3 | `analyze_markets()`, досрочный выход | Очищать `breakeven_activated` и `_trail_best` после досрочного закрытия |
| detect_close_reason | `_detect_close_reason()` | Проверять `orderStatus=Filled` по сохранённым ID TP/SL |
| dashboard place_order | `dashboard.py /api/buy` | Вызывать `portfolio.open_position()` после успешного BUY |
| Trail в минус | `_update_trailing_sl()` | Floor трейлинга = `entry × (1 + floor_pct)`, не ниже безубытка |

---

## 11. README и документация

README.md должен включать:

### Разделы README

1. **О проекте** — краткое описание, скриншоты дашборда, список поддерживаемых пар
2. **Быстрый старт** — шаги от клонирования до первой торговли (DEMO)
3. **Установка зависимостей** — `pip install -r requirements.txt`
4. **Настройка API** — инструкция по вводу ключей через UI (не редактировать файлы!)
5. **Запуск**
   ```bash
   python main.py          # DEMO + REAL (оба сервера)
   python main.py --demo   # только DEMO
   python main.py --real   # только REAL
   ```
6. **Добавление новой пары** — пошаговая инструкция (раздел 6.3 ТЗ)
7. **Переобучение моделей** — когда и как запускать
8. **ML-логика** — описание признаков, разметки, метрик
9. **Параметры стратегии** — таблица с описанием каждого параметра
10. **Дашборд** — описание каждой страницы, скриншоты
11. **Структура данных** — что где хранится, что исключено из git
12. **Известные ограничения** — Cross Margin требует ручного включения на Bybit

---

## 12. Зависимости

### requirements.txt

```
# Торговля
requests>=2.28

# Безопасность
cryptography>=41.0

# Веб-дашборд
flask>=2.3
flask-socketio>=5.3
eventlet>=0.33

# ML / Анализ
scikit-learn>=1.3
xgboost>=1.7
pandas>=2.0
numpy>=1.24

# Системные
python-dateutil>=2.8
pytz>=2023.3
```

### npm (для блока визуализации, опционально)

```
lightweight-charts    # TradingView chart library (встраивается через CDN или локально)
```

Если Lightweight Charts используется через CDN — npm не нужен:
```html
<script src="https://unpkg.com/lightweight-charts/dist/lightweight-charts.standalone.production.js"></script>
```

---

## Приложение A: Таблица багов v1, обязательных к исправлению в v2

| # | Баг | Симптом | Приоритет |
|---|---|---|---|
| 1 | Ghost v1 | Позиция не открывается после trail/SL — бот видит "ghost" | 🔴 Критический |
| 2 | Bug #3 | Досрочный выход не очищает breakeven → следующий вход мгновенно закрывается trail | 🔴 Критический |
| 3 | detect_close_reason | TP детектируется как "причина неизвестна" → decisions.csv неточен | 🟡 Высокий |
| 4 | dashboard /api/buy | Ручная покупка не регистрируется в portfolio → entry="—", PnL="?" | 🟡 Высокий |
| 5 | Trail ниже входа | OPUSDT trail -0.32% при активном breakeven | 🟡 Высокий |
| 6 | Cross Margin | Все шорты падают с "haven't enabled Cross Margin" | 🟡 Высокий (организационный) |
| 7 | Сетевой таймаут | DNS failure не обрабатывается, бот продолжает работу без перезапуска соединения | 🟢 Средний |

## Приложение B: Метрики успеха v2

| Метрика | Целевое значение |
|---|---|
| Win Rate (DEMO, 30 дней) | > 60% |
| Средний PnL на сделку | > +0.8% |
| Trail exits (% от всех закрытий) | < 30% (сейчас 42%) |
| Ср. PnL trail-закрытий | > +1.0% (сейчас +0.3%) |
| Uptime бота | > 99% (автоперезапуск при сбое) |
| Шорты | > 0 успешных сделок (сейчас 0 из-за Cross Margin) |
