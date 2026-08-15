"""
Clean-Clean form handler — приймає заявки з сайту і шле у Telegram.

Деплоїться на Render як Web Service. Токен і chat_id — у env vars Render.
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
import secrets
import time
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone, timedelta
from typing import Any

import httpx
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field, field_validator

import crypto
import db

# ── Config ──────────────────────────────────────────────────────────────
def _required(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise SystemExit(f"❌ Не задана env-var: {name}")
    return value


TELEGRAM_TOKEN = _required("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = _required("TELEGRAM_CHAT_ID")

# ЧистоТак — окремий бот і чат
CHISTOTAK_TOKEN = os.environ.get("CHISTOTAK_TELEGRAM_TOKEN", TELEGRAM_TOKEN)
CHISTOTAK_CHAT_ID = os.environ.get("CHISTOTAK_TELEGRAM_CHAT_ID", TELEGRAM_CHAT_ID)

# Відгуки: секрет для валідації вхідних запитів від Telegram (X-Telegram-Bot-Api-Secret-Token).
# Порожній рядок = вебхук вимкнено (ендпоінт відповідає 404, поки секрет не заданий).
TELEGRAM_WEBHOOK_SECRET = os.environ.get("TELEGRAM_WEBHOOK_SECRET", "")

ALLOWED_ORIGINS = os.environ.get(
    "ALLOWED_ORIGINS",
    "https://kilimanjaro778877-lgtm.github.io,"
    "https://clean-clean.com.ua,"
    "https://www.clean-clean.com.ua,"
    "http://clean-clean.com.ua,"
    "http://www.clean-clean.com.ua,"
    "https://chisto-tak.com.ua,"
    "https://www.chisto-tak.com.ua,"
    "http://chisto-tak.com.ua,"
    "http://www.chisto-tak.com.ua,"
    "https://kilimanjaro778877-lgtm.github.io/chistotak-site",
).split(",")


# ── Logging ─────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("clean-form")


# ── App ─────────────────────────────────────────────────────────────────
# Готовність фічі відгуків перевіряється ОДИН РАЗ при старті (не в обробнику
# запиту — інакше відсутній ключ шифрування здатен вивалити SystemExit
# посеред request і вбити весь процес, включно з формами заявок, які й так
# уже працюють у проді). Якщо щось не готове — ендпоінти відгуків 503,
# решта сервісу (order-форми) продовжує працювати як і раніше.
REVIEWS_ENABLED = False


@asynccontextmanager
async def _lifespan(_app: FastAPI):
    global REVIEWS_ENABLED
    db_ok = await db.init_pool()
    REVIEWS_ENABLED = db_ok and crypto.ready() and db.salt_ready()
    if db_ok and not REVIEWS_ENABLED:
        log.error(
            "reviews DB connected, but disabled: REVIEWS_ENC_KEY=%s IP_HASH_SALT=%s",
            "ok" if crypto.ready() else "MISSING",
            "ok" if db.salt_ready() else "MISSING",
        )
    log.info("reviews feature: %s", "enabled" if REVIEWS_ENABLED else "disabled")
    yield
    await db.close_pool()


# /docs та /redoc віддають повну схему всіх ендпоінтів (включно з модерацією) —
# у проді це безкоштовна карта API для будь-кого, хто на неї натрапить.
# Вмикається явно через ENABLE_DOCS=1 (лише для локальної розробки).
_docs_enabled = bool(os.environ.get("ENABLE_DOCS"))

app = FastAPI(
    title="Clean-Clean form handler",
    lifespan=_lifespan,
    docs_url="/docs" if _docs_enabled else None,
    redoc_url="/redoc" if _docs_enabled else None,
    openapi_url="/openapi.json" if _docs_enabled else None,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[origin.strip() for origin in ALLOWED_ORIGINS],
    allow_credentials=False,
    allow_methods=["POST", "GET", "OPTIONS"],
    allow_headers=["Content-Type"],
)


# ── Schema ──────────────────────────────────────────────────────────────
class OrderForm(BaseModel):
    name: str = Field(..., min_length=1, max_length=100)
    phone: str = Field(..., min_length=5, max_length=30)
    city: str = Field(..., max_length=50)
    service: str = Field(..., max_length=100)
    details: str = Field(default="", max_length=1000)
    page: str = Field(default="", max_length=200)
    # honeypot — спам-боти заповнять, реальні люди ні
    website: str = Field(default="", max_length=200)

    @field_validator("phone")
    @classmethod
    def clean_phone(cls, v: str) -> str:
        cleaned = re.sub(r"[^\d+]", "", v)
        if len(cleaned) < 5:
            raise ValueError("телефон занадто короткий")
        return cleaned


class ReviewForm(BaseModel):
    """Відгук — без телефону (свідомо: не питаємо його на цій формі)."""
    name: str = Field(..., min_length=1, max_length=100)
    rating: int = Field(..., ge=1, le=5)
    text: str = Field(..., min_length=1, max_length=1000)
    city: str = Field(default="", max_length=100)
    service: str = Field(default="", max_length=100)
    page: str = Field(default="", max_length=200)
    website: str = Field(default="", max_length=200)  # honeypot


# ── TikTok Events API ───────────────────────────────────────────────────
# УВАГА: TIKTOK_ACCESS_TOKEN раніше був прямо в коді цього публічного репозиторію
# (github.com/kilimanjaro778877-lgtm/cleaning-form-handler, private:false) — тобто
# токен уже засвітився в git-історії. Перенесено в env var, але СТАРИЙ токен
# (1dffcc6847eab618ce2e955be753f5dd17ef9264) треба ротувати в TikTok Business
# Manager — сама заміна коду цього не скасовує, старе значення й далі читається
# у git log будь-ким.
TIKTOK_PIXEL_ID = os.environ.get("TIKTOK_PIXEL_ID", "D81IGJRC77UDUGTVEQ00")
# Fallback на старе значення — щоб трекінг не зламався ДО того, як в Render
# з'явиться env var TIKTOK_ACCESS_TOKEN. Після ротації токена в TikTok Business
# Manager постав новий токен у Render і прибери цей fallback.
TIKTOK_ACCESS_TOKEN = os.environ.get("TIKTOK_ACCESS_TOKEN", "1dffcc6847eab618ce2e955be753f5dd17ef9264")
TIKTOK_API = "https://business-api.tiktok.com/open_api/v1.3/pixel/track/"


async def send_tiktok_event(form: OrderForm, client_ip: str = "") -> None:
    """Send server-side SubmitForm event to TikTok Events API."""
    try:
        # Hash phone for privacy
        phone_hash = hashlib.sha256(
            re.sub(r"[^\d]", "", form.phone).encode()
        ).hexdigest()

        payload = {
            "pixel_code": TIKTOK_PIXEL_ID,
            "event": "CompleteRegistration",
            "event_id": str(uuid.uuid4()),
            "timestamp": str(int(time.time())),
            "context": {
                "user": {"phone": phone_hash},
                "ip": client_ip,
                "page": {"url": form.page or "https://clean-clean.com.ua/"},
            },
            "properties": {
                "content_name": form.service,
                "content_category": "cleaning",
            },
        }

        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(
                TIKTOK_API,
                json=payload,
                headers={"Access-Token": TIKTOK_ACCESS_TOKEN},
            )
        if resp.status_code == 200:
            log.info("TikTok event sent: SubmitForm")
        else:
            log.warning("TikTok event failed: %s %s", resp.status_code, resp.text[:200])
    except Exception as exc:
        log.warning("TikTok event error (non-critical): %s", exc)


# ── Telegram ────────────────────────────────────────────────────────────
TELEGRAM_API = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"


def format_message(form: OrderForm) -> str:
    now = datetime.now(timezone(timedelta(hours=3))).strftime("%d.%m.%Y %H:%M")
    lines = [
        "🆕 *Нова заявка з сайту*",
        "",
        f"👤 *Імʼя:* {form.name}",
        f"📞 *Телефон:* `{form.phone}`",
        f"🏙 *Місто:* {form.city}",
        f"🧹 *Послуга:* {form.service}",
    ]
    if form.details:
        lines.append(f"📝 *Деталі:* {form.details}")
    if form.page:
        lines.append(f"🔗 *Сторінка:* {form.page}")
    lines.append(f"🕐 *Час:* {now}")
    return "\n".join(lines)


async def send_to_telegram(text: str) -> None:
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "parse_mode": "Markdown",
    }
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.post(TELEGRAM_API, json=payload)
    if resp.status_code != 200:
        log.error("Telegram error: %s %s", resp.status_code, resp.text)
        raise HTTPException(status_code=502, detail="telegram_unavailable")


# ── ЧистоТак (шле в той самий Telegram, що й Clean-Clean) ───────────────
def format_chistotak_message(form: OrderForm) -> str:
    now = datetime.now(timezone(timedelta(hours=3))).strftime("%d.%m.%Y %H:%M")
    lines = [
        "🆕 *Нова заявка — ЧистоТак*",
        "",
        f"👤 *Імʼя:* {form.name}",
        f"📞 *Телефон:* `{form.phone}`",
        f"🏙 *Місто:* {form.city}",
        f"🧹 *Послуга:* {form.service}",
    ]
    if form.details:
        lines.append(f"📝 *Деталі:* {form.details}")
    if form.page:
        lines.append(f"🔗 *Сторінка:* {form.page}")
    lines.append(f"🕐 *Час:* {now}")
    return "\n".join(lines)


# ── Endpoints ───────────────────────────────────────────────────────────
@app.get("/")
async def root() -> dict[str, str]:
    return {"service": "clean-clean-form-handler", "status": "ok"}


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "healthy"}


@app.post("/api/order")
async def submit_order(form: OrderForm, request: Request) -> dict[str, Any]:
    # honeypot — якщо заповнено, тихо повертаємо OK без надсилання
    if form.website:
        log.warning("honeypot triggered from %s", request.client.host if request.client else "?")
        return {"ok": True}

    log.info("New order: %s / %s / %s", form.name, form.phone, form.service)

    client_ip = request.client.host if request.client else ""
    text = format_message(form)

    # Send Telegram + TikTok server event in parallel
    import asyncio
    await asyncio.gather(
        send_to_telegram(text),
        send_tiktok_event(form, client_ip),
    )
    return {"ok": True}


@app.post("/api/chistotak-order")
async def submit_chistotak_order(form: OrderForm, request: Request) -> dict[str, Any]:
    if form.website:
        return {"ok": True}

    log.info("ЧистоТак order: %s / %s / %s", form.name, form.phone, form.service)
    text = format_chistotak_message(form)

    # Шлємо в окремий бот ЧистоТак
    chistotak_api = f"https://api.telegram.org/bot{CHISTOTAK_TOKEN}/sendMessage"
    payload = {
        "chat_id": CHISTOTAK_CHAT_ID,
        "text": text,
        "parse_mode": "Markdown",
    }
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.post(chistotak_api, json=payload)
    if resp.status_code != 200:
        log.error("ЧистоТак Telegram error: %s %s", resp.status_code, resp.text)
        raise HTTPException(status_code=502, detail="telegram_unavailable")

    return {"ok": True}


# ── Відгуки (ЧистоТак) ────────────────────────────────────────────────────
REVIEWS_SITE = "chistotak"
_CHISTOTAK_API_BASE = f"https://api.telegram.org/bot{CHISTOTAK_TOKEN}"


async def _tg_call(method: str, payload: dict[str, Any]) -> dict[str, Any]:
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.post(f"{_CHISTOTAK_API_BASE}/{method}", json=payload)
    if resp.status_code != 200:
        log.error("Telegram %s error: %s %s", method, resp.status_code, resp.text[:300])
    return resp.json() if resp.content else {}


def _review_notify_text(review_id: int, form: ReviewForm) -> str:
    now = datetime.now(timezone(timedelta(hours=3))).strftime("%d.%m.%Y %H:%M")
    stars = "★" * form.rating + "☆" * (5 - form.rating)
    lines = [
        f"💬 *Новий відгук #{review_id} — ЧистоТак*",
        "",
        f"👤 *Ім'я:* {form.name}",
        f"{stars}",
        f"📝 {form.text}",
    ]
    if form.city:
        lines.append(f"🏙 *Місто:* {form.city}")
    if form.service:
        lines.append(f"🧹 *Послуга:* {form.service}")
    lines.append(f"🕐 *Час:* {now}")
    lines.append("")
    lines.append("Кнопками нижче — опублікувати/відхилити. Щоб відповісти клієнту, надішли текст-відповідь у Reply на це повідомлення.")
    return "\n".join(lines)


def _review_keyboard(review_id: int) -> dict[str, Any]:
    return {
        "inline_keyboard": [[
            {"text": "✅ Опублікувати", "callback_data": f"rv_ok:{review_id}"},
            {"text": "❌ Відхилити", "callback_data": f"rv_no:{review_id}"},
        ]]
    }


@app.post("/api/chistotak-review")
async def submit_chistotak_review(form: ReviewForm, request: Request) -> dict[str, Any]:
    if form.website:  # honeypot
        return {"ok": True}

    if not REVIEWS_ENABLED:
        raise HTTPException(status_code=503, detail="reviews_unavailable")

    client_ip = request.client.host if request.client else ""
    ip_hash = db.hash_ip(client_ip)

    # Rate-limit: не більше 3 відгуків з одного IP за годину (анти-спам,
    # без цього хтось зможе накрутити pending-чергу тисячами фейків)
    recent = await db.recent_submission_count(ip_hash, window_minutes=60)
    if recent >= 3:
        log.warning("review rate-limit hit: %s", ip_hash)
        raise HTTPException(status_code=429, detail="too_many_reviews")

    review_id = await db.insert_review(
        site=REVIEWS_SITE,
        name=form.name,
        rating=form.rating,
        text=form.text,
        city=form.city,
        service=form.service,
        page=form.page,
        ip_hash=ip_hash,
    )
    log.info("New review #%s pending moderation (%s★)", review_id, form.rating)

    tg_resp = await _tg_call("sendMessage", {
        "chat_id": CHISTOTAK_CHAT_ID,
        "text": _review_notify_text(review_id, form),
        "parse_mode": "Markdown",
        "reply_markup": _review_keyboard(review_id),
    })
    msg_id = (tg_resp.get("result") or {}).get("message_id")
    if msg_id:
        await db.set_telegram_msg_id(review_id, msg_id)

    return {"ok": True}


@app.get("/api/chistotak-reviews")
async def list_chistotak_reviews() -> dict[str, Any]:
    if not REVIEWS_ENABLED:
        return {"reviews": []}
    reviews = await db.list_approved(REVIEWS_SITE, limit=30)
    return {"reviews": reviews}


@app.post("/telegram-webhook")
async def telegram_webhook(
    request: Request,
    x_telegram_bot_api_secret_token: str | None = Header(default=None),
) -> dict[str, Any]:
    # Секрет не заданий -> вебхук вважається вимкненим: 404, а не 403 —
    # не підказуємо стороннім, що ендпоінт взагалі існує.
    if not TELEGRAM_WEBHOOK_SECRET:
        raise HTTPException(status_code=404)
    # constant-time порівняння — захист від timing-атаки на секрет
    if not x_telegram_bot_api_secret_token or not secrets.compare_digest(
        x_telegram_bot_api_secret_token, TELEGRAM_WEBHOOK_SECRET
    ):
        raise HTTPException(status_code=404)

    if not REVIEWS_ENABLED:
        return {"ok": True}

    update = await request.json()

    cq = update.get("callback_query")
    if cq:
        data = cq.get("data", "")
        cq_id = cq.get("id")
        action, _, id_str = data.partition(":")
        try:
            review_id = int(id_str)
        except ValueError:
            await _tg_call("answerCallbackQuery", {"callback_query_id": cq_id, "text": "Помилка id"})
            return {"ok": True}

        if action == "rv_ok":
            await db.set_status(review_id, "approved")
            note = "✅ Опубліковано"
        elif action == "rv_no":
            await db.set_status(review_id, "rejected")
            note = "❌ Відхилено"
        else:
            await _tg_call("answerCallbackQuery", {"callback_query_id": cq_id})
            return {"ok": True}

        await _tg_call("answerCallbackQuery", {"callback_query_id": cq_id, "text": note})
        msg = cq.get("message") or {}
        if msg.get("message_id"):
            # прибираємо кнопки, дописуємо статус — щоб не тиснули двічі
            await _tg_call("editMessageReplyMarkup", {
                "chat_id": CHISTOTAK_CHAT_ID,
                "message_id": msg["message_id"],
                "reply_markup": {"inline_keyboard": []},
            })
            old_text = msg.get("text", "")
            await _tg_call("editMessageText", {
                "chat_id": CHISTOTAK_CHAT_ID,
                "message_id": msg["message_id"],
                "text": f"{old_text}\n\n{note}",
                "parse_mode": "Markdown",
            })
        return {"ok": True}

    msg = update.get("message")
    if msg and msg.get("reply_to_message") and msg.get("text"):
        parent_id = msg["reply_to_message"].get("message_id")
        review = await db.get_by_telegram_msg_id(parent_id) if parent_id else None
        if review:
            await db.set_reply(review["id"], msg["text"])
            if review["status"] == "pending":
                await db.set_status(review["id"], "approved")
            await _tg_call("sendMessage", {
                "chat_id": CHISTOTAK_CHAT_ID,
                "text": f"💬 Відповідь до відгуку #{review['id']} збережена й опублікована на сайті.",
                "reply_to_message_id": msg["message_id"],
            })
        return {"ok": True}

    return {"ok": True}
