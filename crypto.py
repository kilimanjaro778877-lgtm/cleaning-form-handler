"""
Шифрування колонок бази даних (AES-256-GCM, authenticated encryption).

Ключ — 32 байти, зберігається ТІЛЬКИ в env var REVIEWS_ENC_KEY (base64),
окремо від самої бази (Render env vars != Render Postgres). Кожне поле
шифрується незалежно з випадковим 96-бітним nonce; результат —
base64(nonce || ciphertext || tag).

Чому це закриває: якщо базу зіллють (витік DATABASE_URL, вкрадений бекап,
скомпрометований Render-акаунт) — атакер отримує лише беззмістовний
ciphertext. Без ключа (який лежить окремо, в env, не в БД і не в git)
розшифрувати неможливо. GCM tag ще й ловить підміну байтів (tamper-evident):
якщо хтось спробує підправити зашифроване значення напряму в базі,
decrypt() впаде з InvalidTag, а не тихо поверне сміття.

Згенерувати ключ:
    python -c "import os,base64;print(base64.b64encode(os.urandom(32)).decode())"
Покласти результат ТІЛЬКИ в Render env vars — ніколи в код, ніколи в чат/лог.
"""
from __future__ import annotations

import base64
import functools
import os

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

_KEY_ENV = "REVIEWS_ENC_KEY"


class DecryptionError(Exception):
    """Дані пошкоджені або підмінені (GCM tag mismatch)."""


@functools.lru_cache(maxsize=1)
def _key() -> bytes:
    raw = os.environ.get(_KEY_ENV)
    if not raw:
        raise SystemExit(
            f"❌ Не задана env-var: {_KEY_ENV}. Згенеруй:\n"
            '  python -c "import os,base64;print(base64.b64encode(os.urandom(32)).decode())"'
        )
    try:
        key = base64.b64decode(raw, validate=True)
    except Exception as exc:  # noqa: BLE001
        raise SystemExit(f"❌ {_KEY_ENV} не валідний base64: {exc}") from exc
    if len(key) != 32:
        raise SystemExit(f"❌ {_KEY_ENV} має бути 32 байти (base64), отримано {len(key)}")
    return key


def encrypt(plaintext: str) -> str:
    """Шифрує UTF-8 рядок. Порожній рядок лишається порожнім (не шифруємо '')."""
    if not plaintext:
        return ""
    aesgcm = AESGCM(_key())
    nonce = os.urandom(12)
    ct = aesgcm.encrypt(nonce, plaintext.encode("utf-8"), None)
    return base64.b64encode(nonce + ct).decode("ascii")


def ready() -> bool:
    """Best-effort перевірка КОНФІГУ (не мереж/БД) — викликати ОДИН РАЗ при старті,
    а не всередині обробника запиту (SystemExit посеред request-хендлера здатен
    вбити весь ASGI-процес, а не тільки цей ендпоінт)."""
    try:
        _key()
        return True
    except SystemExit:
        return False


def decrypt(blob: str) -> str:
    """Обернена до encrypt(). Кидає DecryptionError, якщо дані підмінені/биті."""
    if not blob:
        return ""
    aesgcm = AESGCM(_key())
    try:
        raw = base64.b64decode(blob, validate=True)
        nonce, ct = raw[:12], raw[12:]
        return aesgcm.decrypt(nonce, ct, None).decode("utf-8")
    except (InvalidTag, ValueError) as exc:
        raise DecryptionError(f"не вдалося розшифрувати: {exc}") from exc
