"""
Async Postgres шар для відгуків. Шифровані PII-поля (name, text, reply)
через crypto.py; усе інше — параметризовані запити (asyncpg $1,$2,...),
НІКОЛИ f-string/format у SQL (захист від SQL-ін'єкції).

Підключення — лише DATABASE_URL (Render Postgres, TLS обов'язковий).
Якщо DATABASE_URL не задано — пул лишається None, і решта застосунку
(форми заявок, які вже працюють у проді) продовжує працювати як раніше;
падають лише ендпоінти відгуків (503), а не весь сервіс.
"""
from __future__ import annotations

import hashlib
import os
from typing import Any

import asyncpg

from crypto import encrypt, decrypt

DATABASE_URL = os.environ.get("DATABASE_URL", "")

_pool: asyncpg.Pool | None = None

SCHEMA = """
CREATE TABLE IF NOT EXISTS reviews (
    id BIGSERIAL PRIMARY KEY,
    site TEXT NOT NULL,
    name_enc TEXT NOT NULL,
    rating SMALLINT NOT NULL CHECK (rating BETWEEN 1 AND 5),
    text_enc TEXT NOT NULL,
    city TEXT NOT NULL DEFAULT '',
    service TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','approved','rejected')),
    reply_enc TEXT NOT NULL DEFAULT '',
    reply_at TIMESTAMPTZ,
    page TEXT NOT NULL DEFAULT '',
    ip_hash TEXT NOT NULL DEFAULT '',
    telegram_msg_id BIGINT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_reviews_site_status ON reviews (site, status, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_reviews_msg_id ON reviews (telegram_msg_id);
"""


async def init_pool() -> bool:
    """Best-effort: якщо DATABASE_URL не задано або конект впав — не валимо весь сервіс."""
    global _pool
    if not DATABASE_URL:
        return False
    try:
        _pool = await asyncpg.create_pool(
            DATABASE_URL, min_size=1, max_size=5, ssl="require", command_timeout=10
        )
        async with _pool.acquire() as conn:
            await conn.execute(SCHEMA)
        return True
    except Exception:  # noqa: BLE001 — не валимо весь застосунок через БД
        _pool = None
        return False


async def close_pool() -> None:
    if _pool is not None:
        await _pool.close()


def available() -> bool:
    return _pool is not None


def salt_ready() -> bool:
    """Перевірка конфігу — викликати раз при старті (див. crypto.ready())."""
    return bool(os.environ.get("IP_HASH_SALT", ""))


def hash_ip(ip: str) -> str:
    """Хешуємо IP замість зберігання у відкритому вигляді (мінімізація PII).
    RuntimeError, а не SystemExit: ця функція викликається під час обробки
    запиту, і SystemExit тут здатен вбити весь ASGI-процес."""
    salt = os.environ.get("IP_HASH_SALT", "")
    if not salt:
        raise RuntimeError("IP_HASH_SALT not set")
    return hashlib.sha256((salt + ip).encode()).hexdigest()[:32]


async def recent_submission_count(ip_hash: str, window_minutes: int = 60) -> int:
    assert _pool is not None
    async with _pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT count(*) AS n FROM reviews
            WHERE ip_hash = $1 AND created_at > now() - make_interval(mins => $2)
            """,
            ip_hash, window_minutes,
        )
        return row["n"]


async def insert_review(
    *, site: str, name: str, rating: int, text: str, city: str,
    service: str, page: str, ip_hash: str,
) -> int:
    assert _pool is not None
    async with _pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO reviews (site, name_enc, rating, text_enc, city, service, page, ip_hash)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
            RETURNING id
            """,
            site, encrypt(name), rating, encrypt(text), city, service, page, ip_hash,
        )
        return row["id"]


async def set_telegram_msg_id(review_id: int, msg_id: int) -> None:
    assert _pool is not None
    async with _pool.acquire() as conn:
        await conn.execute(
            "UPDATE reviews SET telegram_msg_id = $1 WHERE id = $2", msg_id, review_id
        )


def _decrypt_row(r: asyncpg.Record) -> dict[str, Any]:
    return {
        "id": r["id"],
        "name": decrypt(r["name_enc"]),
        "rating": r["rating"],
        "text": decrypt(r["text_enc"]),
        "city": r["city"],
        "service": r["service"],
        "reply": decrypt(r["reply_enc"]) if r["reply_enc"] else None,
        "reply_at": r["reply_at"].isoformat() if r["reply_at"] else None,
        "created_at": r["created_at"].isoformat(),
    }


async def list_approved(site: str, limit: int = 30) -> list[dict[str, Any]]:
    assert _pool is not None
    async with _pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT id, name_enc, rating, text_enc, city, service, reply_enc, reply_at, created_at
            FROM reviews WHERE site = $1 AND status = 'approved'
            ORDER BY created_at DESC LIMIT $2
            """,
            site, limit,
        )
    return [_decrypt_row(r) for r in rows]


async def get_by_id(review_id: int) -> dict[str, Any] | None:
    assert _pool is not None
    async with _pool.acquire() as conn:
        r = await conn.fetchrow(
            """
            SELECT id, site, name_enc, rating, text_enc, city, service, status,
                   reply_enc, reply_at, created_at, telegram_msg_id
            FROM reviews WHERE id = $1
            """,
            review_id,
        )
    if r is None:
        return None
    d = _decrypt_row(r)
    d["site"] = r["site"]
    d["status"] = r["status"]
    d["telegram_msg_id"] = r["telegram_msg_id"]
    return d


async def get_by_telegram_msg_id(msg_id: int) -> dict[str, Any] | None:
    assert _pool is not None
    async with _pool.acquire() as conn:
        r = await conn.fetchrow(
            "SELECT id FROM reviews WHERE telegram_msg_id = $1", msg_id
        )
    if r is None:
        return None
    return await get_by_id(r["id"])


async def set_status(review_id: int, status: str) -> bool:
    assert status in ("pending", "approved", "rejected")
    assert _pool is not None
    async with _pool.acquire() as conn:
        result = await conn.execute(
            "UPDATE reviews SET status = $1 WHERE id = $2", status, review_id
        )
    return result == "UPDATE 1"


async def set_reply(review_id: int, reply_text: str) -> bool:
    assert _pool is not None
    async with _pool.acquire() as conn:
        result = await conn.execute(
            "UPDATE reviews SET reply_enc = $1, reply_at = now() WHERE id = $2",
            encrypt(reply_text), review_id,
        )
    return result == "UPDATE 1"
