"""
Локальні тести form-handler. НІЯКИХ реальних запитів: Telegram підмінено
(app._post_json), будь-який інший вихід у мережу через httpx блокується,
Postgres підмінено фейковим пулом (перевіряється справжній код db.py).

Запуск:  python -m pytest -q
"""
from __future__ import annotations

import base64
import json
import logging
import os
import sys
import types
from pathlib import Path

import pytest

# ── env ДО імпорту app (app читає env при імпорті) ───────────────────────
os.environ["TELEGRAM_TOKEN"] = "TEST-CC-TOKEN"
os.environ["TELEGRAM_CHAT_ID"] = "111"
os.environ["CHISTOTAK_TELEGRAM_TOKEN"] = "TEST-CT-TOKEN"
os.environ["CHISTOTAK_TELEGRAM_CHAT_ID"] = "222"
os.environ["REVIEWS_ENC_KEY"] = base64.b64encode(b"k" * 32).decode()
os.environ["IP_HASH_SALT"] = "test-salt"
os.environ.pop("DATABASE_URL", None)
os.environ.pop("TIKTOK_ACCESS_TOKEN", None)
os.environ.pop("ALLOWED_ORIGINS", None)

# asyncpg має C-розширення; якщо воно не вантажиться локально (напр. Windows
# App Control блокує DLL) — підставляємо заглушку: справжня БД у тестах не потрібна.
try:
    import asyncpg  # noqa: F401
except ImportError:
    _stub = types.ModuleType("asyncpg")
    _stub.Pool = object
    _stub.Record = dict

    async def _no_pool(*a, **k):
        raise RuntimeError("asyncpg stub: no real DB in tests")

    _stub.create_pool = _no_pool
    sys.modules["asyncpg"] = _stub

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import app as app_module  # noqa: E402
import crypto  # noqa: E402
import db  # noqa: E402

CT_URL = "/api/chistotak-order"
CC_URL = "/api/order"


# ── Фейки ────────────────────────────────────────────────────────────────
class FakeTelegram:
    """Підміна app._post_json. statuses — черга кодів відповіді (або Exception)."""

    def __init__(self):
        self.calls: list[tuple[str, dict]] = []
        self.statuses: list = []
        self.result: dict = {"message_id": 55}

    async def __call__(self, url, payload, timeout=15):
        self.calls.append((url, payload))
        st = self.statuses.pop(0) if self.statuses else 200
        if isinstance(st, Exception):
            raise st
        body = {"ok": st == 200}
        if st == 200:
            body["result"] = self.result
        else:
            body["description"] = "Bad Request: test error"
        return httpx.Response(st, content=json.dumps(body).encode(),
                              request=httpx.Request("POST", url))


class FakeConn:
    def __init__(self, pool):
        self.pool = pool

    async def fetchrow(self, sql, *args):
        if self.pool.fail_insert:
            raise ConnectionError("db down")
        assert "INSERT INTO leads" in sql
        self.pool.next_id += 1
        cols = ["site", "name_enc", "phone_enc", "address_enc", "details_enc",
                "city", "service", "page", "source", "ip_hash"]
        row = dict(zip(cols, args))
        row["id"] = self.pool.next_id
        row["delivered"] = False
        self.pool.rows[row["id"]] = row
        return {"id": row["id"]}

    async def execute(self, sql, *args):
        assert "UPDATE leads" in sql
        delivered, lead_id = args
        self.pool.rows[lead_id]["delivered"] = delivered
        return "UPDATE 1"


class _Acq:
    def __init__(self, pool):
        self.pool = pool

    async def __aenter__(self):
        return FakeConn(self.pool)

    async def __aexit__(self, *exc):
        return False


class FakePool:
    def __init__(self):
        self.rows: dict[int, dict] = {}
        self.next_id = 100
        self.fail_insert = False

    def acquire(self, timeout=None):
        return _Acq(self)


# ── Фікстури ─────────────────────────────────────────────────────────────
@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    # Будь-який справжній HTTP-запит через httpx = провал тесту
    async def _blocked(self, *a, **k):
        raise AssertionError("real network call attempted")

    monkeypatch.setattr(httpx.AsyncClient, "send", _blocked)
    app_module._rate_buckets.clear()
    monkeypatch.setattr(app_module, "LEADS_DB_ENABLED", False)
    monkeypatch.setattr(app_module, "REVIEWS_ENABLED", False)
    monkeypatch.setattr(db, "_pool", None)
    yield
    app_module._rate_buckets.clear()


@pytest.fixture
def tg(monkeypatch):
    fake = FakeTelegram()
    monkeypatch.setattr(app_module, "_post_json", fake)
    return fake


@pytest.fixture
def pool(monkeypatch):
    fp = FakePool()
    monkeypatch.setattr(db, "_pool", fp)
    monkeypatch.setattr(db, "_leads_ready", True)
    monkeypatch.setattr(app_module, "LEADS_DB_ENABLED", True)
    return fp


@pytest.fixture
def client():
    # без `with` — lifespan (підключення до справжньої БД) не запускається
    return TestClient(app_module.app)


def lead(**over):
    base = {
        "name": "Іван",
        "phone": "+38 (067) 123-45-67",
        "city": "Київ",
        "service": "Генеральне прибирання",
        "details": "Площа: 60 м²",
        "address": "вул. Хрещатик, 22",
        "page": "https://chisto-tak.com.ua/",
        "website": "",
    }
    base.update(over)
    return base


# ── Заявки: успіх і контракт фронтендів ──────────────────────────────────
def test_chistotak_order_success_contract(client, tg):
    r = client.post(CT_URL, json=lead())
    assert r.status_code == 200
    assert r.json() == {"ok": True}  # фронти ЧистоТак перевіряють resp.ok
    assert len(tg.calls) == 1
    url, payload = tg.calls[0]
    assert url == "https://api.telegram.org/botTEST-CT-TOKEN/sendMessage"
    assert payload["chat_id"] == "222"
    assert payload["parse_mode"] == "HTML"
    assert "ЧистоТак" in payload["text"]
    assert "вул. Хрещатик, 22" in payload["text"]  # адреса більше не губиться
    assert "<code>+380671234567</code>" in payload["text"]


def test_cleanclean_order_success_contract(client, tg):
    payload = lead(page="https://clean-clean.com.ua/kyiv.html")
    payload.pop("address")
    r = client.post(CC_URL, json=payload)
    assert r.status_code == 200
    data = r.json()
    assert data == {"ok": True}
    assert data.get("ok") or data.get("stub")  # app.js: if (data.ok || data.stub)
    url, sent = tg.calls[0]
    assert url == "https://api.telegram.org/botTEST-CC-TOKEN/sendMessage"
    assert sent["chat_id"] == "111"
    assert sent["parse_mode"] == "HTML"
    assert "Нова заявка з сайту" in sent["text"]


def test_popup_payload_and_unknown_fields_accepted(client, tg):
    """callback-popup.js шле source + немає details/address; формы шлють зайві поля."""
    r = client.post(CT_URL, json={
        "name": "Зворотний дзвінок (спливаюче вікно)", "phone": "0671234567",
        "city": "Не вказано (спливаюче вікно)", "service": "Зворотний дзвінок",
        "page": "https://chisto-tak.com.ua/kyiv.html", "source": "popup-15s",
        "website": "", "furniture": "диван",
    })
    assert r.status_code == 200 and r.json() == {"ok": True}


def test_long_ad_url_is_not_rejected(client, tg):
    page = ("https://chisto-tak.com.ua/services/generalna-prybyrannia.html?gad_source=1"
            "&gad_campaignid=23000000000&gbraid=0AAAAA" + "x" * 80 + "&gclid=" + "C" * 120)
    assert len(page) > 200
    r = client.post(CT_URL, json=lead(page=page))
    assert r.status_code == 200


def test_validation_still_rejects_bad_phone(client, tg):
    r = client.post(CT_URL, json=lead(phone="12"))
    assert r.status_code == 422
    assert tg.calls == []


def test_honeypot(client, tg, pool):
    r = client.post(CT_URL, json=lead(website="http://spam.example"))
    assert r.status_code == 200 and r.json() == {"ok": True}
    r = client.post(CC_URL, json=lead(website="spam"))
    assert r.status_code == 200 and r.json() == {"ok": True}
    assert tg.calls == []
    assert pool.rows == {}


# ── HTML-екранування ─────────────────────────────────────────────────────
@pytest.mark.parametrize("endpoint", [CT_URL, CC_URL])
def test_html_escaping(client, tg, endpoint):
    page = "https://chisto-tak.com.ua/?gad_source=1&utm_source=x_y"
    r = client.post(endpoint, json=lead(name="<b>Ivan_*</b>", page=page,
                                         details="a < b & c > d"))
    assert r.status_code == 200
    _, sent = tg.calls[0]
    text = sent["text"]
    assert sent["parse_mode"] == "HTML"
    assert "&lt;b&gt;Ivan_*&lt;/b&gt;" in text
    assert "<b>Ivan" not in text
    assert ('<a href="https://chisto-tak.com.ua/?gad_source=1&amp;utm_source=x_y">'
            in text)
    assert "a &lt; b &amp; c &gt; d" in text
    # Markdown більше не використовується ніде
    assert "*Імʼя:*" not in text


def test_non_http_page_is_not_a_link(client, tg):
    client.post(CT_URL, json=lead(page='javascript:alert("x")'))
    text = tg.calls[0][1]["text"]
    assert "<a " not in text
    assert "javascript:alert(&quot;x&quot;)" in text


def test_html_rejected_falls_back_to_plain_text(client, tg):
    tg.statuses = [400, 200]
    r = client.post(CT_URL, json=lead(name="<i>Ivan</i>"))
    assert r.status_code == 200
    assert len(tg.calls) == 2
    plain = tg.calls[1][1]
    assert "parse_mode" not in plain
    assert "<i>Ivan</i>" in plain["text"]


# ── Збереження в БД + статус доставки ────────────────────────────────────
def test_lead_saved_encrypted_and_marked_delivered(client, tg, pool):
    r = client.post(CT_URL, json=lead(), headers={"X-Forwarded-For": "5.6.7.8, 10.0.0.1"})
    assert r.status_code == 200 and r.json() == {"ok": True}
    assert len(pool.rows) == 1
    row = next(iter(pool.rows.values()))
    assert row["site"] == "chistotak"
    assert row["delivered"] is True
    # ПД зашифровані
    assert "0671234567" not in row["phone_enc"]
    assert crypto.decrypt(row["phone_enc"]) == "+380671234567"
    assert crypto.decrypt(row["name_enc"]) == "Іван"
    assert crypto.decrypt(row["address_enc"]) == "вул. Хрещатик, 22"
    assert row["ip_hash"] == db.hash_ip("5.6.7.8")
    # id заявки потрапляє в повідомлення
    assert f"№</b>{row['id']}" in tg.calls[0][1]["text"]


@pytest.mark.parametrize("endpoint", [CT_URL, CC_URL])
def test_telegram_down_db_ok_returns_200_undelivered(client, tg, pool, endpoint):
    tg.statuses = [500]
    r = client.post(endpoint, json=lead())
    assert r.status_code == 200
    assert r.json() == {"ok": True}
    row = next(iter(pool.rows.values()))
    assert row["delivered"] is False


def test_telegram_network_error_db_ok_returns_200(client, tg, pool):
    tg.statuses = [httpx.ConnectTimeout("timeout")]
    r = client.post(CT_URL, json=lead())
    assert r.status_code == 200
    assert next(iter(pool.rows.values()))["delivered"] is False


@pytest.mark.parametrize("endpoint", [CT_URL, CC_URL])
def test_both_down_returns_502(client, tg, pool, endpoint):
    pool.fail_insert = True
    tg.statuses = [500]
    r = client.post(endpoint, json=lead())
    assert r.status_code == 502
    assert r.json() == {"detail": "telegram_unavailable"}


def test_no_db_configured_telegram_down_returns_502_as_before(client, tg):
    tg.statuses = [500]
    r = client.post(CT_URL, json=lead())
    assert r.status_code == 502


def test_db_down_telegram_ok_returns_200(client, tg, pool):
    pool.fail_insert = True
    r = client.post(CT_URL, json=lead())
    assert r.status_code == 200
    assert len(tg.calls) == 1


# ── Rate limit + X-Forwarded-For ─────────────────────────────────────────
def test_rate_limit_429_per_endpoint(client, tg):
    for _ in range(5):
        assert client.post(CT_URL, json=lead()).status_code == 200
    r = client.post(CT_URL, json=lead())
    assert r.status_code == 429
    assert len(tg.calls) == 5
    # інший ендпоінт — окремий лічильник
    assert client.post(CC_URL, json=lead()).status_code == 200


def test_rate_limit_uses_x_forwarded_for(client, tg):
    h1 = {"X-Forwarded-For": "1.1.1.1, 10.0.0.1"}
    for _ in range(5):
        assert client.post(CT_URL, json=lead(), headers=h1).status_code == 200
    assert client.post(CT_URL, json=lead(), headers={"X-Forwarded-For": "1.1.1.1"}).status_code == 429
    # інший реальний клієнт за тим самим проксі — не блокується
    assert client.post(CT_URL, json=lead(),
                       headers={"X-Forwarded-For": "2.2.2.2, 10.0.0.1"}).status_code == 200


def test_get_client_ip_fallback():
    class R:
        headers = {}
        client = types.SimpleNamespace(host="9.8.7.6")
    assert app_module.get_client_ip(R()) == "9.8.7.6"


# ── Логи без ПД ──────────────────────────────────────────────────────────
def test_logs_contain_no_pii(client, tg, pool, caplog):
    caplog.set_level(logging.DEBUG)
    tg.statuses = [500]
    client.post(CT_URL, json=lead(name="Секретне Імʼя", phone="0679998877",
                                  address="вул. Таємна 1"))
    logs = "\n".join(r.getMessage() for r in caplog.records)
    assert "lead:" in logs
    for pii in ("Секретне", "0679998877", "+380679998877", "Таємна", "TEST-CT-TOKEN"):
        assert pii not in logs


# ── TikTok: без env-токена нічого не шлемо ───────────────────────────────
def test_tiktok_not_sent_without_env_token(client, tg, monkeypatch):
    assert app_module.TIKTOK_ACCESS_TOKEN == ""
    called = []

    async def spy(self, *a, **k):
        called.append(1)
        raise AssertionError("network")

    monkeypatch.setattr(httpx.AsyncClient, "send", spy)
    assert client.post(CC_URL, json=lead()).status_code == 200
    assert called == []


def test_no_hardcoded_tiktok_token_in_source():
    src = (Path(__file__).resolve().parent.parent / "app.py").read_text(encoding="utf-8")
    assert 'os.environ.get("TIKTOK_ACCESS_TOKEN", "")' in src
    assert "1dffcc68" not in src


# ── CORS ─────────────────────────────────────────────────────────────────
def test_cors_origins(client):
    origins = app_module._clean_origins(app_module.ALLOWED_ORIGINS)
    assert all("/" not in o.split("://", 1)[1] for o in origins)
    for o in ("https://chisto-tak.com.ua", "https://www.chisto-tak.com.ua",
              "https://clean-clean.com.ua", "https://www.clean-clean.com.ua"):
        assert o in origins
        r = client.options(CT_URL, headers={
            "Origin": o, "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "content-type"})
        assert r.status_code == 200
        assert r.headers.get("access-control-allow-origin") == o
    assert app_module._clean_origins(["https://x.github.io/chistotak-site", " https://a.b/ "]) == ["https://a.b"]


# ── Відгуки ──────────────────────────────────────────────────────────────
@pytest.fixture
def reviews(monkeypatch):
    state = {"recent_ip_hash": None, "recent": 0, "status": {}, "msg_id": {}}

    async def recent_submission_count(ip_hash, window_minutes=60):
        state["recent_ip_hash"] = ip_hash
        return state["recent"]

    async def insert_review(**kw):
        state["inserted"] = kw
        return 7

    async def set_telegram_msg_id(review_id, msg_id):
        state["msg_id"][review_id] = msg_id

    async def set_status(review_id, status):
        state["status"][review_id] = status
        return True

    monkeypatch.setattr(db, "recent_submission_count", recent_submission_count)
    monkeypatch.setattr(db, "insert_review", insert_review)
    monkeypatch.setattr(db, "set_telegram_msg_id", set_telegram_msg_id)
    monkeypatch.setattr(db, "set_status", set_status)
    monkeypatch.setattr(app_module, "REVIEWS_ENABLED", True)
    return state


def review(**over):
    base = {"name": "<b>Оля</b>", "rating": 5, "text": "Супер & швидко_*", "city": "Київ",
            "service": "Генеральне", "page": "https://chisto-tak.com.ua/vidguky.html", "website": ""}
    base.update(over)
    return base


def test_review_submit_html_and_real_ip(client, tg, reviews):
    r = client.post("/api/chistotak-review", json=review(),
                    headers={"X-Forwarded-For": "9.9.9.9, 10.0.0.1"})
    assert r.status_code == 200 and r.json() == {"ok": True}
    assert reviews["recent_ip_hash"] == db.hash_ip("9.9.9.9")  # реальний IP, не проксі
    url, sent = tg.calls[0]
    assert url == "https://api.telegram.org/botTEST-CT-TOKEN/sendMessage"
    assert sent["parse_mode"] == "HTML"
    assert "&lt;b&gt;Оля&lt;/b&gt;" in sent["text"]
    assert "Супер &amp; швидко_*" in sent["text"]
    kb = sent["reply_markup"]["inline_keyboard"][0]
    assert [b["callback_data"] for b in kb] == ["rv_ok:7", "rv_no:7"]
    assert reviews["msg_id"] == {7: 55}


def test_review_rate_limit(client, tg, reviews):
    reviews["recent"] = 3
    r = client.post("/api/chistotak-review", json=review())
    assert r.status_code == 429


def test_review_telegram_down_does_not_500(client, tg, reviews):
    tg.statuses = [httpx.ConnectError("down")]
    r = client.post("/api/chistotak-review", json=review())
    assert r.status_code == 200


def test_reviews_disabled_503(client, tg):
    r = client.post("/api/chistotak-review", json=review())
    assert r.status_code == 503


def test_webhook_moderation_callback(client, tg, reviews, monkeypatch):
    monkeypatch.setattr(app_module, "TELEGRAM_WEBHOOK_SECRET", "s3cret")
    update = {"callback_query": {
        "id": "cb1", "data": "rv_ok:7",
        "message": {"message_id": 55, "text": "💬 Новий відгук #7 — ЧистоТак\n👤 Ім'я: <b>Оля</b> & co"},
    }}
    r = client.post("/telegram-webhook", json=update,
                    headers={"X-Telegram-Bot-Api-Secret-Token": "s3cret"})
    assert r.status_code == 200
    assert reviews["status"] == {7: "approved"}
    methods = [u.rsplit("/", 1)[1] for u, _ in tg.calls]
    assert methods == ["answerCallbackQuery", "editMessageReplyMarkup", "editMessageText"]
    edit = tg.calls[2][1]
    assert edit["parse_mode"] == "HTML"
    assert edit["message_id"] == 55
    assert "&lt;b&gt;Оля&lt;/b&gt; &amp; co" in edit["text"]
    assert edit["text"].endswith("✅ Опубліковано")


def test_webhook_reject_and_wrong_secret(client, tg, reviews, monkeypatch):
    monkeypatch.setattr(app_module, "TELEGRAM_WEBHOOK_SECRET", "s3cret")
    r = client.post("/telegram-webhook", json={}, headers={"X-Telegram-Bot-Api-Secret-Token": "nope"})
    assert r.status_code == 404
    update = {"callback_query": {"id": "cb2", "data": "rv_no:7",
                                 "message": {"message_id": 56, "text": "x"}}}
    r = client.post("/telegram-webhook", json=update,
                    headers={"X-Telegram-Bot-Api-Secret-Token": "s3cret"})
    assert r.status_code == 200
    assert reviews["status"] == {7: "rejected"}


def test_health_and_root(client):
    assert client.get("/health").json() == {"status": "healthy"}
    assert client.get("/").json()["status"] == "ok"


def test_review_long_ad_url_is_trimmed_not_422(client):
    """vidguky.html шле page=location.href — з рекламними мітками може бути > 200 символів."""
    long_page = "https://chisto-tak.com.ua/vidguky.html?gad_source=1&gclid=" + "x" * 1500
    r = client.post("/api/chistotak-review", json={"name": "Іра", "rating": 5, "text": "Супер", "page": long_page})
    assert r.status_code != 422
