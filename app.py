"""
Clean-Clean form handler — приймає заявки з сайту і шле у Telegram.

Деплоїться на Render як Web Service. Токен і chat_id — у env vars Render.
"""

from __future__ import annotations

import asyncio
import hashlib
import html
import logging
import os
import re
import secrets
import time
import uuid
from collections import deque
from contextlib import asynccontextmanager
from datetime import datetime, timezone, timedelta
from typing import Any

import httpx
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field, field_validator, model_validator

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
    "http://www.chisto-tak.com.ua",
).split(",")

# Антиспам заявок: не більше LEAD_RATE_LIMIT заявок за LEAD_RATE_WINDOW_SEC
# з одного IP, окремо для кожного ендпоінта (in-memory, на процес).
LEAD_RATE_LIMIT = int(os.environ.get("LEAD_RATE_LIMIT", "5"))
LEAD_RATE_WINDOW_SEC = int(os.environ.get("LEAD_RATE_WINDOW_SEC", "600"))


# ── Logging ─────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("clean-form")
# httpx на INFO пише кожен URL запиту — а в URL Telegram Bot API сидить токен бота.
logging.getLogger("httpx").setLevel(logging.WARNING)


def _clean_origins(raw: list[str]) -> list[str]:
    """Origin — це лише scheme://host[:port], без шляху. Записи зі шляхом
    браузер ніколи не надішле, тож вони мертві — відкидаємо."""
    out: list[str] = []
    for item in raw:
        o = item.strip().rstrip("/")
        if not o:
            continue
        if re.fullmatch(r"https?://[^/\s]+", o):
            out.append(o)
        else:
            log.warning("CORS: ignoring invalid origin entry %r", o)
    return out


# ── App ─────────────────────────────────────────────────────────────────
# Готовність фічі відгуків перевіряється ОДИН РАЗ при старті (не в обробнику
# запиту — інакше відсутній ключ шифрування здатен вивалити SystemExit
# посеред request і вбити весь процес, включно з формами заявок, які й так
# уже працюють у проді). Якщо щось не готове — ендпоінти відгуків 503,
# решта сервісу (order-форми) продовжує працювати як і раніше.
REVIEWS_ENABLED = False
# Збереження заявок у Postgres: потрібні DATABASE_URL + REVIEWS_ENC_KEY (ПД
# шифруються). Без них — працюємо як раніше: тільки Telegram.
LEADS_DB_ENABLED = False


@asynccontextmanager
async def _lifespan(_app: FastAPI):
    global REVIEWS_ENABLED, LEADS_DB_ENABLED
    db_ok = await db.init_pool()
    REVIEWS_ENABLED = db_ok and crypto.ready() and db.salt_ready()
    LEADS_DB_ENABLED = db_ok and crypto.ready() and db.leads_ready()
    log.info("leads DB storage: %s", "enabled" if LEADS_DB_ENABLED else "disabled")
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
    allow_origins=_clean_origins(ALLOWED_ORIGINS),
    allow_credentials=False,
    allow_methods=["POST", "GET", "OPTIONS"],
    allow_headers=["Content-Type"],
)


# ── Schema ──────────────────────────────────────────────────────────────
# Задовгі текстові поля ОБРІЗАЄМО, а не відхиляємо з 422: інакше, наприклад,
# URL сторінки з рекламними мітками (gclid/gbraid/utm_*) легко перевищує ліміт,
# і заявка з платного трафіку губиться цілком.
_TRUNCATE = {"name": 100, "city": 50, "service": 100, "details": 1000,
             "page": 1000, "address": 300, "source": 50, "website": 200}


class OrderForm(BaseModel):
    name: str = Field(..., min_length=1, max_length=100)
    phone: str = Field(..., min_length=5, max_length=30)
    city: str = Field(..., max_length=50)
    service: str = Field(..., max_length=100)
    details: str = Field(default="", max_length=1000)
    page: str = Field(default="", max_length=1000)
    # ЧистоТак-форми надсилають адресу (input name="address") — раніше вона мовчки губилась
    address: str = Field(default="", max_length=300)
    # callback-popup.js надсилає source="popup-15s"
    source: str = Field(default="", max_length=50)
    # honeypot — спам-боти заповнять, реальні люди ні
    website: str = Field(default="", max_length=200)

    @model_validator(mode="before")
    @classmethod
    def _truncate_long(cls, data: Any) -> Any:
        if isinstance(data, dict):
            data = dict(data)
            for key, limit in _TRUNCATE.items():
                v = data.get(key)
                if isinstance(v, str) and len(v) > limit:
                    data[key] = v[:limit]
        return data

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
    page: str = Field(default="", max_length=1000)
    website: str = Field(default="", max_length=200)  # honeypot

    @model_validator(mode="before")
    @classmethod
    def _truncate_page(cls, data: Any) -> Any:
        # URL з рекламними мітками може бути довгим — обрізаємо, а не 422
        if isinstance(data, dict) and isinstance(data.get("page"), str) and len(data["page"]) > 1000:
            data = {**data, "page": data["page"][:1000]}
        return data


# ── TikTok Events API ───────────────────────────────────────────────────
# УВАГА: TIKTOK_ACCESS_TOKEN раніше був прямо в коді цього публічного репозиторію
# (github.com/kilimanjaro778877-lgtm/cleaning-form-handler, private:false) — тобто
# токен уже засвітився в git-історії. Старий токен треба ротувати в TikTok
# Business Manager — сама заміна коду цього не скасовує.
# Токен — ТІЛЬКИ з env. Немає env-var -> серверна подія просто не надсилається.
TIKTOK_PIXEL_ID = os.environ.get("TIKTOK_PIXEL_ID", "D81IGJRC77UDUGTVEQ00")
TIKTOK_ACCESS_TOKEN = os.environ.get("TIKTOK_ACCESS_TOKEN", "")
TIKTOK_API = "https://business-api.tiktok.com/open_api/v1.3/pixel/track/"


async def send_tiktok_event(form: OrderForm, client_ip: str = "") -> None:
    """Send server-side SubmitForm event to TikTok Events API."""
    if not TIKTOK_ACCESS_TOKEN:
        return
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
        log.warning("TikTok event error (non-critical): %s", type(exc).__name__)


# ── Client IP (за проксі Render) ────────────────────────────────────────
def get_client_ip(request: Request) -> str:
    """Реальний IP відвідувача. Render (як і будь-який reverse proxy) ставить
    X-Forwarded-For: "<client>, <proxy1>, ..." — беремо перший адрес.
    Fallback — request.client.host (працює і з, і без --proxy-headers).
    Застереження: перший адрес XFF клієнт може підставити сам, тож для
    антиспаму це "мʼякий" ключ (захист від масового флуду, не від цільової атаки)."""
    xff = request.headers.get("x-forwarded-for", "")
    if xff:
        first = xff.split(",")[0].strip()
        if first:
            return first[:64]
    return request.client.host if request.client else ""


# ── Rate limit заявок (in-memory, sliding window) ───────────────────────
_rate_buckets: dict[tuple[str, str], deque[float]] = {}


def _rate_limited(scope: str, ip: str) -> bool:
    """True -> ліміт перевищено (такий запит НЕ зараховується).
    Памʼять: тримаємо лише мітки часу за останнє вікно; застарілі ключі чистимо."""
    if LEAD_RATE_LIMIT <= 0 or not ip:
        return False
    now = time.monotonic()
    cutoff = now - LEAD_RATE_WINDOW_SEC
    if len(_rate_buckets) > 10_000:  # захист від роздування памʼяті
        for k in [k for k, q in _rate_buckets.items() if not q or q[-1] < cutoff]:
            del _rate_buckets[k]
    q = _rate_buckets.setdefault((scope, ip), deque())
    while q and q[0] < cutoff:
        q.popleft()
    if len(q) >= LEAD_RATE_LIMIT:
        return True
    q.append(now)
    return False


# ── Telegram ────────────────────────────────────────────────────────────
TELEGRAM_API = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
CHISTOTAK_API = f"https://api.telegram.org/bot{CHISTOTAK_TOKEN}/sendMessage"


def _e(value: str) -> str:
    """Екранування користувацького значення для Telegram parse_mode=HTML."""
    return html.escape(value or "", quote=True)


def _page_line_html(page: str) -> str:
    if re.match(r"^https?://", page, re.IGNORECASE):
        label = page if len(page) <= 120 else page[:117] + "…"
        return f'🔗 <b>Сторінка:</b> <a href="{_e(page)}">{_e(label)}</a>'
    return f"🔗 <b>Сторінка:</b> {_e(page)}"


def format_lead_message(title: str, form: OrderForm, lead_id: int | None = None) -> tuple[str, str]:
    """Повертає (html, plain). plain — запасний варіант без parse_mode,
    якщо Telegram раптом відхилить HTML (400: can't parse entities / bad URL)."""
    now = datetime.now(timezone(timedelta(hours=3))).strftime("%d.%m.%Y %H:%M")
    h = [
        f"🆕 <b>{_e(title)}</b>",
        "",
        f"👤 <b>Імʼя:</b> {_e(form.name)}",
        f"📞 <b>Телефон:</b> <code>{_e(form.phone)}</code>",
        f"🏙 <b>Місто:</b> {_e(form.city)}",
    ]
    p = [f"🆕 {title}", "", f"👤 Імʼя: {form.name}", f"📞 Телефон: {form.phone}",
         f"🏙 Місто: {form.city}"]
    if form.address:
        h.append(f"📍 <b>Адреса:</b> {_e(form.address)}")
        p.append(f"📍 Адреса: {form.address}")
    h.append(f"🧹 <b>Послуга:</b> {_e(form.service)}")
    p.append(f"🧹 Послуга: {form.service}")
    if form.details:
        h.append(f"📝 <b>Деталі:</b> {_e(form.details)}")
        p.append(f"📝 Деталі: {form.details}")
    if form.page:
        h.append(_page_line_html(form.page))
        p.append(f"🔗 Сторінка: {form.page}")
    h.append(f"🕐 <b>Час:</b> {now}")
    p.append(f"🕐 Час: {now}")
    if lead_id is not None:
        h.append(f"🆔 <b>Заявка №</b>{lead_id}")
        p.append(f"🆔 Заявка №{lead_id}")
    return "\n".join(h), "\n".join(p)


def format_message(form: OrderForm, lead_id: int | None = None) -> str:
    return format_lead_message("Нова заявка з сайту", form, lead_id)[0]


def format_chistotak_message(form: OrderForm, lead_id: int | None = None) -> str:
    return format_lead_message("Нова заявка — ЧистоТак", form, lead_id)[0]


async def _post_json(url: str, payload: dict[str, Any], timeout: float = 15) -> httpx.Response:
    """Єдина точка виходу в мережу для Telegram (тести її підміняють)."""
    async with httpx.AsyncClient(timeout=timeout) as client:
        return await client.post(url, json=payload)


def _tg_error_desc(resp: httpx.Response) -> str:
    """Опис помилки Telegram для логу — без тексту повідомлення (там ПД)."""
    try:
        return str(resp.json().get("description", ""))[:200]
    except Exception:  # noqa: BLE001
        return ""


async def send_lead_to_telegram(api_url: str, chat_id: str, html_text: str, plain_text: str) -> bool:
    """True, якщо Telegram прийняв повідомлення. Ніколи не кидає виняток."""
    try:
        resp = await _post_json(api_url, {"chat_id": chat_id, "text": html_text, "parse_mode": "HTML"})
        if resp.status_code == 200:
            return True
        log.error("Telegram error: %s %s", resp.status_code, _tg_error_desc(resp))
        if resp.status_code == 400:
            # HTML не прийняли — пробуємо простим текстом, щоб заявка не загубилась
            resp = await _post_json(api_url, {"chat_id": chat_id, "text": plain_text})
            if resp.status_code == 200:
                return True
            log.error("Telegram plain-text fallback error: %s %s", resp.status_code, _tg_error_desc(resp))
    except Exception as exc:  # noqa: BLE001 — мережеві помилки/таймаути
        log.error("Telegram request failed: %s", type(exc).__name__)
    return False


# ── Обробка заявки (спільна для обох сайтів) ────────────────────────────
async def _process_lead(
    *, site: str, title: str, api_url: str, chat_id: str,
    form: OrderForm, request: Request, tiktok: bool,
) -> dict[str, Any]:
    client_ip = get_client_ip(request)
    if _rate_limited(site, client_ip):
        log.warning("lead rate-limit hit: site=%s", site)
        raise HTTPException(status_code=429, detail="too_many_requests")

    # honeypot — якщо заповнено, тихо повертаємо OK без надсилання
    if form.website:
        log.warning("honeypot triggered: site=%s", site)
        return {"ok": True}

    # 1) Спершу зберігаємо в БД (якщо налаштована). Збій БД заявку не валить.
    lead_id: int | None = None
    if LEADS_DB_ENABLED:
        try:
            lead_id = await asyncio.wait_for(db.insert_lead(
                site=site, name=form.name, phone=form.phone, address=form.address,
                details=form.details, city=form.city, service=form.service,
                page=form.page, source=form.source,
                ip_hash=db.hash_ip_optional(client_ip),
            ), timeout=8)
        except Exception as exc:  # noqa: BLE001
            log.error("lead DB save failed: site=%s err=%s", site, type(exc).__name__)

    # 2) Telegram (+ серверна подія TikTok паралельно — як і раніше, лише для Clean-Clean)
    html_text, plain_text = format_lead_message(title, form, lead_id)
    if tiktok:
        delivered, _ = await asyncio.gather(
            send_lead_to_telegram(api_url, chat_id, html_text, plain_text),
            send_tiktok_event(form, client_ip),
        )
    else:
        delivered = await send_lead_to_telegram(api_url, chat_id, html_text, plain_text)

    if lead_id is not None:
        try:
            await asyncio.wait_for(db.set_lead_delivered(lead_id, delivered), timeout=8)
        except Exception as exc:  # noqa: BLE001
            log.error("lead delivered-flag update failed: id=%s err=%s", lead_id, type(exc).__name__)

    log.info(
        "lead: id=%s site=%s service=%r saved=%s delivered=%s",
        lead_id, site, form.service[:60], lead_id is not None, delivered,
    )
    if not delivered and lead_id is None:
        # ні в БД, ні в Telegram — фронт має побачити помилку (і повторити)
        raise HTTPException(status_code=502, detail="telegram_unavailable")
    if not delivered:
        log.error("lead saved but NOT delivered to Telegram: id=%s site=%s", lead_id, site)
    return {"ok": True}


# ── Endpoints ───────────────────────────────────────────────────────────
@app.get("/")
async def root() -> dict[str, str]:
    return {"service": "clean-clean-form-handler", "status": "ok"}


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "healthy"}


@app.post("/api/order")
async def submit_order(form: OrderForm, request: Request) -> dict[str, Any]:
    return await _process_lead(
        site="cleanclean", title="Нова заявка з сайту",
        api_url=TELEGRAM_API, chat_id=TELEGRAM_CHAT_ID,
        form=form, request=request, tiktok=True,
    )


@app.post("/api/chistotak-order")
async def submit_chistotak_order(form: OrderForm, request: Request) -> dict[str, Any]:
    return await _process_lead(
        site="chistotak", title="Нова заявка — ЧистоТак",
        api_url=CHISTOTAK_API, chat_id=CHISTOTAK_CHAT_ID,
        form=form, request=request, tiktok=False,
    )


# ── Відгуки (ЧистоТак) ────────────────────────────────────────────────────
REVIEWS_SITE = "chistotak"
_CHISTOTAK_API_BASE = f"https://api.telegram.org/bot{CHISTOTAK_TOKEN}"


async def _tg_call(method: str, payload: dict[str, Any]) -> dict[str, Any]:
    """Виклик Bot API ЧистоТак. Ніколи не кидає виняток (збій Telegram не має
    перетворюватись на 500 після того, як відгук уже збережено в БД)."""
    try:
        resp = await _post_json(f"{_CHISTOTAK_API_BASE}/{method}", payload)
    except Exception as exc:  # noqa: BLE001
        log.error("Telegram %s request failed: %s", method, type(exc).__name__)
        return {}
    if resp.status_code != 200:
        log.error("Telegram %s error: %s %s", method, resp.status_code, _tg_error_desc(resp))
    try:
        return resp.json() if resp.content else {}
    except Exception:  # noqa: BLE001
        return {}


def _review_notify_text(review_id: int, form: ReviewForm) -> str:
    now = datetime.now(timezone(timedelta(hours=3))).strftime("%d.%m.%Y %H:%M")
    stars = "★" * form.rating + "☆" * (5 - form.rating)
    lines = [
        f"💬 <b>Новий відгук #{review_id} — ЧистоТак</b>",
        "",
        f"👤 <b>Ім'я:</b> {_e(form.name)}",
        f"{stars}",
        f"📝 {_e(form.text)}",
    ]
    if form.city:
        lines.append(f"🏙 <b>Місто:</b> {_e(form.city)}")
    if form.service:
        lines.append(f"🧹 <b>Послуга:</b> {_e(form.service)}")
    lines.append(f"🕐 <b>Час:</b> {now}")
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

    client_ip = get_client_ip(request)
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
        "parse_mode": "HTML",
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
            # Telegram віддає msg.text без розмітки (plain) — тому екрануємо його цілком
            old_text = msg.get("text", "")
            await _tg_call("editMessageText", {
                "chat_id": CHISTOTAK_CHAT_ID,
                "message_id": msg["message_id"],
                "text": f"{_e(old_text)}\n\n{_e(note)}",
                "parse_mode": "HTML",
            })
        return {"ok": True}

    msg = update.get("message")
    if msg and msg.get("reply_to_message") and msg.get("text"):
        parent_id = msg["reply_to_message"].get("message_id")
        review = await db.get_by_telegram_msg_id(parent_id) if parent_id else None
        if review:
            REJECT_WORDS = {"видалити", "приховати", "скасувати", "delete", "remove"}
            if msg["text"].strip().lower() in REJECT_WORDS:
                await db.set_status(review["id"], "rejected")
                await _tg_call("sendMessage", {
                    "chat_id": CHISTOTAK_CHAT_ID,
                    "text": f"❌ Відгук #{review['id']} знятий з публікації.",
                    "reply_to_message_id": msg["message_id"],
                })
            else:
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
