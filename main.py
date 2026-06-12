#!/usr/bin/env python3
# main.py — HTT v2 точка входа
"""
Запуск:
  python main.py            # DEMO (порт 5000)
  python main.py --real     # REAL (порт 5001)
  python main.py --setup    # Настройка API-ключей (CLI)

При первом запуске:
  python security/key_manager.py    # введи ключи и мастер-пароль
  python main.py                     # затем запуск
"""

import os
import sys
import signal
import argparse
import getpass
import threading
import logging

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)

from config.app_config import (
    PROJECT_NAME, PROJECT_VERSION,
    DASHBOARD_DEMO_PORT, DASHBOARD_REAL_PORT,
)


# ── Логирование в файл ────────────────────────────────────────────────────────

def setup_file_logging(real_mode: bool):
    """Пишет stdout в logs/bot_YYYY-MM-DD_HH-MM.log посуточно."""
    from datetime import datetime
    logs_dir = os.path.join(ROOT, "logs")
    os.makedirs(logs_dir, exist_ok=True)
    log_path = os.path.join(
        logs_dir, f"bot_{datetime.now().strftime('%Y-%m-%d_%H-%M')}.log"
    )

    fmt     = logging.Formatter("%(asctime)s | %(message)s", datefmt="%H:%M:%S")
    handler = logging.handlers.TimedRotatingFileHandler(
        log_path, when='midnight', backupCount=90, encoding='utf-8'
    )
    handler.setFormatter(fmt)
    root = logging.getLogger('bot_file')
    root.setLevel(logging.DEBUG)
    root.propagate = False
    root.addHandler(handler)

    class _Tee:
        def __init__(self, stream, logger):
            self._stream = stream
            self._logger = logger
            self._buf    = ''
        def write(self, text):
            self._stream.write(text)
            self._buf += text
            if '\n' in self._buf:
                lines = self._buf.split('\n')
                for line in lines[:-1]:
                    if line.strip():
                        self._logger.info(line.rstrip())
                self._buf = lines[-1]
        def flush(self):
            self._stream.flush()
        def isatty(self):
            return False

    sys.stdout = _Tee(sys.__stdout__, root)
    sys.__stdout__.write(f"📁 Лог: {log_path}\n")
    return log_path


# ── Получение API-ключей ──────────────────────────────────────────────────────

def get_api_credentials(mode: str, master_password: str = None):
    """
    Загружает API-ключи из зашифрованного хранилища.
    Если ключей нет — предлагает ввести через CLI.
    """
    from security.key_manager import credentials_exist, load_credentials, save_credentials

    mode_str = mode.lower()  # 'demo' или 'real'

    if not credentials_exist(mode_str):
        print(f"\n⚠️  Ключи [{mode_str.upper()}] не найдены.")
        print(f"  Запусти: python security/key_manager.py")
        print(f"  Или введи сейчас:\n")

        api_key    = input(f"API Key [{mode_str.upper()}]:    ").strip()
        api_secret = input(f"API Secret [{mode_str.upper()}]: ").strip()
        if not master_password:
            master_password = getpass.getpass("Мастер-пароль (скрыт): ")

        if not save_credentials(mode_str, api_key, api_secret, master_password):
            print("❌ Не удалось сохранить ключи")
            sys.exit(1)

    if not master_password:
        master_password = getpass.getpass(f"Мастер-пароль для [{mode_str.upper()}]: ")

    creds = load_credentials(mode_str, master_password)
    if not creds:
        print("❌ Неверный мастер-пароль или повреждены ключи")
        sys.exit(1)

    return creds['key'], creds['secret']


# ── Основной запуск ───────────────────────────────────────────────────────────

def main():
    import logging.handlers  # noqa — нужен для setup_file_logging

    parser = argparse.ArgumentParser(description=f"{PROJECT_NAME} v{PROJECT_VERSION}")
    parser.add_argument('--real',  action='store_true', help='Запуск в REAL режиме')
    parser.add_argument('--setup', action='store_true', help='Настройка API-ключей')
    parser.add_argument('--password', type=str, default=None,
                        help='Мастер-пароль (не рекомендуется в CLI, используй интерактивный ввод)')
    args = parser.parse_args()

    # ── Режим настройки ───────────────────────────────────────────────────────
    if args.setup:
        from security.key_manager import save_credentials
        mode       = input("Режим (demo/real): ").strip().lower()
        api_key    = input("API Key:    ").strip()
        api_secret = input("API Secret: ").strip()
        pwd        = getpass.getpass("Мастер-пароль: ")
        pwd2       = getpass.getpass("Повторите:     ")
        if pwd != pwd2:
            print("❌ Пароли не совпадают")
            sys.exit(1)
        save_credentials(mode, api_key, api_secret, pwd)
        print("✅ Готово. Теперь запусти: python main.py")
        sys.exit(0)

    real_mode = args.real
    mode_str  = "REAL" if real_mode else "DEMO"

    # ── Логирование ───────────────────────────────────────────────────────────
    setup_file_logging(real_mode)

    print("=" * 70)
    print(f"  {PROJECT_NAME} v{PROJECT_VERSION}")
    print(f"  Режим: {mode_str}")
    print("=" * 70)

    # ── Ключи ─────────────────────────────────────────────────────────────────
    # Диагностика cryptography при старте
    try:
        from security.key_manager import _CRYPTO_AVAILABLE, _CRYPTO_ERROR
        if _CRYPTO_AVAILABLE:
            print("🔐 cryptography: AES-256-GCM активен")
        elif _CRYPTO_ERROR:
            print(f"⚠️  cryptography ошибка: {_CRYPTO_ERROR}")
        else:
            print("⚠️  cryptography: недоступна (fallback без шифрования)")
    except Exception as _de:
        print(f"⚠️  cryptography диагностика: {_de}")

    api_key, api_secret = get_api_credentials(mode_str, args.password)

    # ── Бот ───────────────────────────────────────────────────────────────────
    from bot.core import TradingBot
    from dashboard.app import register_bot, start_dashboard, current_data

    bot = TradingBot(real_mode=real_mode, api_key=api_key, api_secret=api_secret)
    register_bot(bot)
    print(f"✅ Бот зарегистрирован в дашборде")

    # ── Анализ рынков в фоне ──────────────────────────────────────────────────
    t_analyze = threading.Thread(target=bot.analyze_markets, daemon=True)
    t_analyze.start()
    print("✅ Поток analyze_markets запущен")

    # ── Обработка Ctrl+C ──────────────────────────────────────────────────────
    def _sigint(sig, frame):
        bot.stop()
        sys.exit(0)
    signal.signal(signal.SIGINT, _sigint)

    # ── Дашборд (блокирующий вызов) ───────────────────────────────────────────
    port = DASHBOARD_REAL_PORT if real_mode else DASHBOARD_DEMO_PORT
    print(f"\n🌐 Дашборд: http://localhost:{port}")
    print("   Нажмите Ctrl+C для остановки\n")

    try:
        start_dashboard(mode_str)
    except KeyboardInterrupt:
        bot.stop()


if __name__ == "__main__":
    import logging.handlers
    main()
