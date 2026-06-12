from typing import Optional
# security/key_manager.py
"""
Безопасное хранение API-ключей Bybit.
Ключи шифруются AES-256-GCM, хранятся в credentials/{mode}.enc.
Мастер-пароль → KDF (PBKDF2-HMAC-SHA256) → ключ шифрования.
Открытый текст НИКОГДА не записывается на диск.
"""

import os
import json
import base64
import hashlib
import warnings

_CRYPTO_AVAILABLE = False
_CRYPTO_ERROR     = None
try:
    import warnings as _w
    with _w.catch_warnings():
        _w.simplefilter("ignore")   # подавляем DeprecationWarning Python 3.8
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
        from cryptography.hazmat.primitives import hashes
    _CRYPTO_AVAILABLE = True
except Exception as _e:
    _CRYPTO_ERROR = str(_e)
    print(f"⚠️  cryptography: {_e}")
    print("   Ключи без шифрования. pip install cryptography")

import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from config.app_config import CREDENTIALS_DIR


# ── Внутренние вспомогательные функции ────────────────────────────────────────

def _derive_key(password: str, salt: bytes) -> bytes:
    """PBKDF2-HMAC-SHA256: пароль + соль → 32-байтный ключ AES-256."""
    if not _CRYPTO_AVAILABLE:
        return hashlib.sha256(password.encode() + salt).digest()
    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA256(),
        length=32,
        salt=salt,
        iterations=390_000,   # OWASP 2023 рекомендация
    )
    return kdf.derive(password.encode("utf-8"))


def _creds_path(mode: str) -> str:
    """Путь к файлу credentials/{mode}.enc"""
    mode = mode.lower()
    if mode not in ("demo", "real"):
        raise ValueError(f"mode должен быть 'demo' или 'real', получено: {mode}")
    return os.path.join(CREDENTIALS_DIR, f"{mode}.enc")


# ── Публичный API ──────────────────────────────────────────────────────────────

def save_credentials(mode: str, api_key: str, api_secret: str,
                     master_password: str) -> bool:
    """
    Шифрует и сохраняет API-ключи.

    Args:
        mode           — 'demo' или 'real'
        api_key        — API ключ Bybit
        api_secret     — API секрет Bybit
        master_password — мастер-пароль для шифрования

    Returns:
        True при успехе, False при ошибке
    """
    try:
        payload = json.dumps({"key": api_key, "secret": api_secret}).encode("utf-8")
        salt    = os.urandom(16)
        enc_key = _derive_key(master_password, salt)

        if _CRYPTO_AVAILABLE:
            aesgcm    = AESGCM(enc_key)
            nonce     = os.urandom(12)
            ciphertext = aesgcm.encrypt(nonce, payload, None)
            data = {
                "v":          2,
                "salt":       base64.b64encode(salt).decode(),
                "nonce":      base64.b64encode(nonce).decode(),
                "ciphertext": base64.b64encode(ciphertext).decode(),
            }
        else:
            # Фолбек без шифрования (только для разработки)
            data = {
                "v":          1,
                "salt":       base64.b64encode(salt).decode(),
                "payload_b64": base64.b64encode(payload).decode(),
                "_warning":   "NOT ENCRYPTED — install cryptography package",
            }

        path = _creds_path(mode)
        tmp  = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp, path)
        print(f"🔐 Ключи [{mode.upper()}] сохранены: {path}")
        return True

    except Exception as e:
        print(f"❌ save_credentials [{mode}]: {e}")
        return False


def load_credentials(mode: str, master_password: str) -> Optional[dict]:
    """
    Расшифровывает и возвращает API-ключи.

    Returns:
        {"key": "...", "secret": "..."} или None при ошибке
    """
    try:
        path = _creds_path(mode)
        if not os.path.exists(path):
            return None

        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)

        salt = base64.b64decode(data["salt"])
        enc_key = _derive_key(master_password, salt)

        if data.get("v") == 2 and _CRYPTO_AVAILABLE:
            nonce      = base64.b64decode(data["nonce"])
            ciphertext = base64.b64decode(data["ciphertext"])
            aesgcm     = AESGCM(enc_key)
            payload    = aesgcm.decrypt(nonce, ciphertext, None)
        else:
            payload = base64.b64decode(data["payload_b64"])

        return json.loads(payload.decode("utf-8"))

    except Exception as e:
        print(f"❌ load_credentials [{mode}]: {e} (неверный пароль?)")
        return None


def credentials_exist(mode: str) -> bool:
    """Проверяет наличие сохранённых ключей для режима."""
    return os.path.exists(_creds_path(mode))


def delete_credentials(mode: str) -> bool:
    """Удаляет файл ключей (при смене ключей)."""
    path = _creds_path(mode)
    if os.path.exists(path):
        os.remove(path)
        print(f"🗑️  Ключи [{mode.upper()}] удалены")
        return True
    return False


def rotate_credentials(mode: str, new_api_key: str, new_api_secret: str,
                        master_password: str) -> bool:
    """Заменяет ключи (удаляет старые, сохраняет новые)."""
    delete_credentials(mode)
    return save_credentials(mode, new_api_key, new_api_secret, master_password)


def verify_password(mode: str, master_password: str) -> bool:
    """Проверяет правильность мастер-пароля (пробует расшифровать)."""
    result = load_credentials(mode, master_password)
    return result is not None


# ── CLI для первичной настройки ────────────────────────────────────────────────

if __name__ == "__main__":
    print("=" * 60)
    print("  HTT v2 — Настройка API-ключей")
    print("=" * 60)
    print()

    mode = input("Режим (demo/real): ").strip().lower()
    if mode not in ("demo", "real"):
        print("Ошибка: введите 'demo' или 'real'")
        exit(1)

    print(f"\nВведите API-ключи для {mode.upper()}")
    print(f"  DEMO ключи: bybit.com → Demo Trading → API Management")
    print(f"  REAL ключи: bybit.com → Main Account → API Management")
    print()

    api_key    = input("API Key:    ").strip()
    api_secret = input("API Secret: ").strip()

    import getpass
    password   = getpass.getpass("Мастер-пароль (скрыт): ")
    password2  = getpass.getpass("Повторите пароль:       ")

    if password != password2:
        print("❌ Пароли не совпадают")
        exit(1)

    if save_credentials(mode, api_key, api_secret, password):
        print(f"\n✅ Готово! Ключи [{mode.upper()}] зашифрованы и сохранены.")
        print(f"   При запуске бота введите этот мастер-пароль.")
    else:
        print("❌ Не удалось сохранить ключи")
