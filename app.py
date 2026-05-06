"""
Clean-Clean form handler — приймає заявки з сайту і шле у Telegram.

Деплоїться на Render як Web Service. Токен і chat_id — у env vars Render.
"""

from __future__ import annotations

import logging
import os
import re
from datetime import datetime
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
ALLOWED_ORIGINS = os.environ.get(
    "ALLOWED_ORIGINS",
    "https://kilimanjaro778877-lgtm.github.io",
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


# ── Telegram ────────────────────────────────────────────────────────────
TELEGRAM_API = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"


def format_message(form: OrderForm) -> str:
    now = datetime.now().strftime("%d.%m.%Y %H:%M")
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

    text = format_message(form)
    await send_to_telegram(text)
    return {"ok": True}
