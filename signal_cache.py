# signal_cache.py
"""
Кэширование сигналов для быстрого старта
"""
import json
import os
import time
import threading
from datetime import datetime, timedelta

SIGNAL_TTL_MINUTES    = 5
SIGNAL_EXPIRE_MINUTES = 10

# Абсолютный путь к директории проекта — FIX для Windows [Errno 22]
# когда рабочая директория запуска отличается от папки скрипта
_BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# Глобальный лок — защита от одновременной записи из двух потоков (WinError 5)
_FILE_LOCK = threading.Lock()


class SignalCache:
    """Кэш сигналов для быстрого старта"""

    def __init__(self, cache_file: str = "signal_cache.json"):
        # Строим абсолютный путь относительно папки проекта
        if not os.path.isabs(cache_file):
            cache_file = os.path.join(_BASE_DIR, cache_file)
        self.cache_file = cache_file
        self._last_error_time = 0   # подавляем повторные ошибки (не спамим в лог)
        self.cache = self._load_cache()

    def _load_cache(self) -> dict:
        """
        Загрузка кэша из файла.
        При загрузке отфильтровываем записи старше SIGNAL_EXPIRE_MINUTES.
        """
        if not os.path.exists(self.cache_file):
            return {}
        try:
            with open(self.cache_file, 'r', encoding='utf-8') as f:
                raw = json.load(f)
            now      = datetime.now()
            filtered = {}
            for symbol, data in raw.items():
                try:
                    ts = datetime.fromisoformat(data['timestamp'])
                    if now - ts < timedelta(minutes=SIGNAL_EXPIRE_MINUTES):
                        filtered[symbol] = data
                except Exception:
                    pass
            return filtered
        except Exception:
            return {}

    def _save_cache(self):
        """
        Сохранение кэша.
        FIX: threading.Lock предотвращает одновременную запись из двух потоков.
        FIX: retry при WinError 5 (файл временно заблокирован антивирусом/индексатором).
        FIX: подавляем спам ошибок — одна ошибка раз в 60 секунд.
        """
        try:
            os.makedirs(os.path.dirname(self.cache_file), exist_ok=True)
            data = json.dumps(self.cache, indent=2, ensure_ascii=False)

            with _FILE_LOCK:
                # Retry до 3 раз при WinError 5 (файл захвачен другим процессом)
                for attempt in range(3):
                    try:
                        with open(self.cache_file, 'w', encoding='utf-8') as f:
                            f.write(data)
                        return  # успех
                    except PermissionError:
                        if attempt < 2:
                            time.sleep(0.05)   # 50ms пауза перед повтором
                        else:
                            raise

        except Exception as e:
            # Не спамим: одна ошибка раз в 60 секунд
            now = time.time()
            if now - self._last_error_time > 60:
                print(f"⚠️ Не удалось сохранить signal_cache: {e}")
                self._last_error_time = now

    def get_signal(self, symbol: str):
        """Получение актуального сигнала из кэша (None если устарел)"""
        if symbol not in self.cache:
            return None
        signal_data = self.cache[symbol]
        try:
            timestamp = datetime.fromisoformat(signal_data['timestamp'])
            if datetime.now() - timestamp < timedelta(minutes=SIGNAL_TTL_MINUTES):
                return signal_data['signal']
        except Exception:
            pass
        return None

    def save_signal(self, symbol: str, signal_data: dict):
        """Сохранение сигнала в кэш"""
        self.cache[symbol] = {
            'timestamp': datetime.now().isoformat(),
            'signal':    signal_data,
        }
        self._save_cache()

    def get_all_signals(self) -> dict:
        """Получение всех актуальных сигналов (не старше TTL)"""
        signals = {}
        now     = datetime.now()
        for symbol, data in self.cache.items():
            try:
                timestamp = datetime.fromisoformat(data['timestamp'])
                if now - timestamp < timedelta(minutes=SIGNAL_TTL_MINUTES):
                    signals[symbol] = data['signal']
            except Exception:
                pass
        return signals

    def clear_old(self):
        """Очистка устаревших сигналов (старше SIGNAL_EXPIRE_MINUTES)"""
        to_delete = [
            symbol for symbol, data in self.cache.items()
            if datetime.now() - datetime.fromisoformat(data['timestamp'])
            > timedelta(minutes=SIGNAL_EXPIRE_MINUTES)
        ]
        for symbol in to_delete:
            del self.cache[symbol]
        if to_delete:
            self._save_cache()
