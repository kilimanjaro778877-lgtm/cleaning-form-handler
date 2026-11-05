"""
Async Postgres шар для відгуків і заявок (таблиця leads). Шифровані PII-поля (name, text, reply)
через crypto.py; усе інше — параметризовані запити (asyncpg $1,$2,...),
НІКОЛИ f-string/format у SQL (захист від SQL-ін'єкції).

Підключення — лише DATABASE_URL (Render Postgres, TLS обов'язковий).
Якщо DATABASE_URL не задано — пул лишається None, і решта застосунку
(форми заявок, які вже працюють у проді) продовжує працювати як раніше;
падають лише ендпоінти відгуків (503), а не весь сервіс.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import os
from typing import Any

import asyncpg

from crypto import encrypt, decrypt

DATABASE_URL = os.environ.get("DATABASE_URL", "")

_pool: asyncpg.Pool | None = None
log = logging.getLogger("clean-form")

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

# Заявки (обидва сайти). ПД (ім'я, телефон, адреса, деталі) — лише шифровані
# (той самий crypto.py/REVIEWS_ENC_KEY). delivered=false -> заявка збережена, але
# в Telegram не дійшла: її можна дослати вручну (SELECT ... WHERE NOT delivered).
LEADS_SCHEMA = """
CREATE TABLE IF NOT EXISTS leads (
    id BIGSERIAL PRIMARY KEY,
    site TEXT NOT NULL,
    name_enc TEXT NOT NULL DEFAULT '',
    phone_enc TEXT NOT NULL DEFAULT '',
    address_enc TEXT NOT NULL DEFAULT '',
    details_enc TEXT NOT NULL DEFAULT '',
    city TEXT NOT NULL DEFAULT '',
    service TEXT NOT NULL DEFAULT '',
    page TEXT NOT NULL DEFAULT '',
    source TEXT NOT NULL DEFAULT '',
    ip_hash TEXT NOT NULL DEFAULT '',
    delivered BOOLEAN NOT NULL DEFAULT false,
    delivered_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_leads_undelivered ON leads (created_at) WHERE NOT delivered;
-- антифрод: ok | suspect | blocked (+ причини)
ALTER TABLE leads ADD COLUMN IF NOT EXISTS quality TEXT NOT NULL DEFAULT '';
"""

_leads_ready = False

# Онлайн-запис (див. booking.py). kind: lead | manual | block; block з slot_time='' — увесь день.
BOOKINGS_SCHEMA = """
CREATE TABLE IF NOT EXISTS bookings (
    id BIGSERIAL PRIMARY KEY,
    site TEXT NOT NULL,
    slot_date DATE NOT NULL,
    slot_time TEXT NOT NULL DEFAULT '',
    kind TEXT NOT NULL CHECK (kind IN ('lead','manual','block')),
    status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active','confirmed','cancelled')),
    lead_id BIGINT,
    ip_hash TEXT NOT NULL DEFAULT '',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_bookings_slot ON bookings (site, slot_date) WHERE status <> 'cancelled';
"""

_bookings_ready = False


async def init_pool() -> bool:
    """Best-effort: якщо DATABASE_URL не задано або конект впав — не валимо весь сервіс."""
    global _pool, _leads_ready, _bookings_ready
    if not DATABASE_URL:
        return False
    # Кілька спроб: після рестарту/створення бази вона може відповісти не одразу
    # (так 2026-11-01 сервіс піднявся без БД і мовчки працював без неї)
    for attempt, delay in enumerate((0, 3, 6, 12), start=1):
        if delay:
            await asyncio.sleep(delay)
        try:
            _pool = await asyncpg.create_pool(
                DATABASE_URL, min_size=1, max_size=5, ssl="require", command_timeout=10
            )
            async with _pool.acquire() as conn:
                await conn.execute(SCHEMA)
            break
        except Exception as exc:  # noqa: BLE001 — не валимо весь застосунок через БД
            # лише тип помилки: текст asyncpg може містити хост/користувача
            log.error("DB connect attempt %d failed: %s", attempt, type(exc).__name__)
            if _pool is not None:
                await _pool.close()
            _pool = None
    if _pool is None:
        return False
    # Окремо від відгуків: якщо з таблицею заявок щось не так — відгуки
    # (і відправка заявок у Telegram) працюють як раніше.
    try:
        async with _pool.acquire() as conn:
            await conn.execute(LEADS_SCHEMA)
        _leads_ready = True
    except Exception:  # noqa: BLE001
        _leads_ready = False
    try:
        async with _pool.acquire() as conn:
            await conn.execute(BOOKINGS_SCHEMA)
        _bookings_ready = True
    except Exception:  # noqa: BLE001
        _bookings_ready = False
    return True


def leads_ready() -> bool:
    return _pool is not None and _leads_ready


def bookings_ready() -> bool:
    return _pool is not None and _bookings_ready


# ── Онлайн-запис ─────────────────────────────────────────────────────────
async def booking_state(site: str, d_from, d_to) -> tuple[dict, set]:
    """(taken, blocks): taken[(date, time)] = зайнято місць; blocks = {(date, time|'')}."""
    assert _pool is not None
    async with _pool.acquire(timeout=5) as conn:
        rows = await conn.fetch(
            """
            SELECT slot_date, slot_time, kind, count(*) AS n FROM bookings
            WHERE site = $1 AND status <> 'cancelled' AND slot_date BETWEEN $2 AND $3
            GROUP BY slot_date, slot_time, kind
            """,
            site, d_from, d_to,
        )
    taken: dict = {}
    blocks: set = set()
    for r in rows:
        key = (r["slot_date"], r["slot_time"])
        if r["kind"] == "block":
            blocks.add(key)
        else:
            taken[key] = taken.get(key, 0) + r["n"]
    return taken, blocks


async def hold_slot(*, site: str, d, t: str, capacity: int, kind: str = "lead",
                    lead_id: int | None = None, ip_hash: str = "") -> int | None:
    """Атомарно займає місце (advisory-lock на слот). None — слот повний або закритий."""
    assert _pool is not None and kind in ("lead", "manual")
    async with _pool.acquire(timeout=5) as conn:
        async with conn.transaction():
            await conn.execute("SELECT pg_advisory_xact_lock(hashtext($1))", f"{site}|{d}|{t}")
            blocked = await conn.fetchval(
                """
                SELECT count(*) FROM bookings WHERE site = $1 AND slot_date = $2 AND kind = 'block'
                  AND status <> 'cancelled' AND slot_time IN ('', $3::text)
                """,
                site, d, t,
            )
            taken = await conn.fetchval(
                """
                SELECT count(*) FROM bookings WHERE site = $1 AND slot_date = $2 AND slot_time = $3
                  AND kind IN ('lead','manual') AND status <> 'cancelled'
                """,
                site, d, t,
            )
            if blocked or taken >= capacity:
                return None
            return await conn.fetchval(
                """
                INSERT INTO bookings (site, slot_date, slot_time, kind, lead_id, ip_hash)
                VALUES ($1, $2, $3, $4, $5, $6) RETURNING id
                """,
                site, d, t, kind, lead_id, ip_hash,
            )


async def ip_recent_holds(site: str, ip_hash: str, hours: int = 24) -> int:
    assert _pool is not None
    async with _pool.acquire(timeout=5) as conn:
        return await conn.fetchval(
            """
            SELECT count(*) FROM bookings WHERE site = $1 AND ip_hash = $2 AND kind = 'lead'
              AND created_at > now() - make_interval(hours => $3)
            """,
            site, ip_hash, hours,
        )


async def set_booking_status(booking_id: int, status: str) -> dict | None:
    """Повертає {slot_date, slot_time, status_before} або None, якщо такого запису немає."""
    assert status in ("active", "confirmed", "cancelled") and _pool is not None
    async with _pool.acquire(timeout=5) as conn:
        r = await conn.fetchrow(
            """
            UPDATE bookings b SET status = $2 FROM (SELECT id, status FROM bookings WHERE id = $1) old
            WHERE b.id = old.id RETURNING b.slot_date, b.slot_time, old.status AS before
            """,
            booking_id, status,
        )
    return dict(r) if r else None


ARTICLES_SCHEMA = """
CREATE TABLE IF NOT EXISTS notified_articles (
    url        TEXT PRIMARY KEY,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
"""


async def new_articles(urls: list[str]) -> list[str]:
    """Позначає статті як «надіслані» і повертає ті, яких раніше не було.
    Перший запуск (таблиця порожня) — лише запамʼятовує наявні, нічого не повертає."""
    assert _pool is not None
    async with _pool.acquire(timeout=5) as conn:
        await conn.execute(ARTICLES_SCHEMA)
        first = not await conn.fetchval("SELECT EXISTS (SELECT 1 FROM notified_articles)")
        fresh = await conn.fetch(
            "INSERT INTO notified_articles (url) SELECT unnest($1::text[]) "
            "ON CONFLICT DO NOTHING RETURNING url", urls)
    return [] if first else [r["url"] for r in fresh]


async def add_block(site: str, d, t: str) -> None:
    assert _pool is not None
    async with _pool.acquire(timeout=5) as conn:
        await conn.execute(
            "INSERT INTO bookings (site, slot_date, slot_time, kind) VALUES ($1, $2, $3, 'block')",
            site, d, t,
        )


async def reopen(site: str, d, t: str, all_times: tuple[str, ...]) -> int:
    """Знімає закриття й записи «по телефону» (lead-записи клієнтів не чіпає —
    їх скасовують кнопкою під заявкою). t='' — увесь день. Якщо відкривають один
    слот у закритому дні — день розбиваємо на закриті слоти, крім цього. Повертає к-сть знятих."""
    assert _pool is not None
    async with _pool.acquire(timeout=5) as conn:
        async with conn.transaction():
            day_blocked = await conn.fetchval(
                """
                SELECT count(*) FROM bookings WHERE site = $1 AND slot_date = $2 AND kind = 'block'
                  AND slot_time = '' AND status <> 'cancelled'
                """,
                site, d,
            )
            res = await conn.execute(
                """
                UPDATE bookings SET status = 'cancelled'
                WHERE site = $1 AND slot_date = $2 AND kind IN ('block','manual') AND status <> 'cancelled'
                  AND ($3::text = '' OR slot_time IN ('', $3::text))
                """,
                site, d, t,
            )
            if t and day_blocked:
                for other in all_times:
                    if other != t:
                        await conn.execute(
                            "INSERT INTO bookings (site, slot_date, slot_time, kind) VALUES ($1, $2, $3, 'block')",
                            site, d, other,
                        )
    return int(res.split()[-1]) if res else 0


def hash_ip_optional(ip: str) -> str:
    """Як hash_ip(), але без винятку: без IP_HASH_SALT просто не зберігаємо IP."""
    try:
        return hash_ip(ip) if ip else ""
    except RuntimeError:
        return ""


async def insert_lead(
    *, site: str, name: str, phone: str, address: str, details: str,
    city: str, service: str, page: str, source: str, ip_hash: str, quality: str = '',
) -> int:
    assert _pool is not None
    async with _pool.acquire(timeout=5) as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO leads (site, name_enc, phone_enc, address_enc, details_enc,
                               city, service, page, source, ip_hash, quality)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11)
            RETURNING id
            """,
            site, encrypt(name), encrypt(phone), encrypt(address), encrypt(details),
            city, service, page, source, ip_hash, quality[:300],
        )
        return row["id"]


async def set_lead_delivered(lead_id: int, delivered: bool) -> None:
    assert _pool is not None
    async with _pool.acquire(timeout=5) as conn:
        await conn.execute(
            """
            UPDATE leads SET delivered = $1::boolean,
                   delivered_at = CASE WHEN $1::boolean THEN now() ELSE NULL END
            WHERE id = $2
            """,
            delivered, lead_id,
        )


async def close_pool() -> None:
    global _pool
    if _pool is not None:
        try:
            await _pool.close()
        except Exception:  # noqa: BLE001
            pass
        _pool = None


def available() -> bool:
    return _pool is not None


async def ping() -> bool:
    """Чи відповідає БД (для сторожа в app.py). Ніколи не кидає виняток."""
    if _pool is None:
        return False
    try:
        async with _pool.acquire(timeout=5) as conn:
            return await asyncio.wait_for(conn.fetchval("SELECT 1"), timeout=5) == 1
    except Exception:  # noqa: BLE001
        return False


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
