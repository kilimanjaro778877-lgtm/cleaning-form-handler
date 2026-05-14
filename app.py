"""
Clean-Clean form handler — приймає заявки з сайту і шле у Telegram.

Деплоїться на Render як Web Service. Токен і chat_id — у env vars Render.
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
import time
import uuid
from datetime import datetime, timezone, timedelta
from typing import Any

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field, field_validator


# ── Config ──────────────────────────────────────────────────────────────
def _required(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise SystemExit(f"❌ Не задана env-var: {name}")
    return value


TELEGRAM_TOKEN = _required("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = _required("TELEGRAM_CHAT_ID")

# ЧистоТак
CHISTOTAK_BOT_TOKEN = os.environ.get("CHISTOTAK_BOT_TOKEN", "8924613073:AAFBmEgr3uWb72VOXmWj8S4rR7rJDc7SBfo")
CHISTOTAK_CHAT_ID   = os.environ.get("CHISTOTAK_CHAT_ID",   "1003760194653")

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
app = FastAPI(title="Clean-Clean form handler")

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


# ── TikTok Events API ───────────────────────────────────────────────────
TIKTOK_PIXEL_ID = "D81IGJRC77UDUGTVEQ00"
TIKTOK_ACCESS_TOKEN = "1dffcc6847eab618ce2e955be753f5dd17ef9264"
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


# ── ЧистоТак Telegram ───────────────────────────────────────────────────
CHISTOTAK_API = f"https://api.telegram.org/bot{CHISTOTAK_BOT_TOKEN}/sendMessage"


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


async def send_to_chistotak_telegram(text: str) -> None:
    payload = {
        "chat_id": CHISTOTAK_CHAT_ID,
        "text": text,
        "parse_mode": "Markdown",
    }
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.post(CHISTOTAK_API, json=payload)
    if resp.status_code != 200:
        log.error("ЧистоТак Telegram error: %s %s", resp.status_code, resp.text)
        raise HTTPException(status_code=502, detail="telegram_unavailable")


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
    await send_to_chistotak_telegram(text)
    return {"ok": True}
