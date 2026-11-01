"""Онлайн-запис: календар, бронювання під заявку, кнопки й команди менеджера в Telegram."""
from datetime import date, datetime

import pytest

import booking
from tests.test_app import app_module, db, lead, client, tg, _isolate  # noqa: F401  (фікстури)

EP = "/api/chistotak-order"
NOW = datetime(2026, 11, 2, 6, 0, tzinfo=booking.TZ)       # пн 02.11, 06:00 за Києвом
D = date(2026, 11, 3)                                        # вт 03.11
HUMAN = dict(t_ms=45000, hi=37)
SECRET = {"X-Telegram-Bot-Api-Secret-Token": "s3cret"}


class FakeBookings:
    """In-memory двійник db-функцій запису з тією ж семантикою, що й SQL у db.py."""

    def __init__(self):
        self.rows: list[dict] = []
        self.fail = False

    def _active(self, **kw):
        return [r for r in self.rows if r["status"] != "cancelled"
                and all(r[k] == v for k, v in kw.items())]

    async def booking_state(self, site, d_from, d_to):
        if self.fail:
            raise ConnectionError("db down")
        taken, blocks = {}, set()
        for r in self._active(site=site):
            if not d_from <= r["slot_date"] <= d_to:
                continue
            key = (r["slot_date"], r["slot_time"])
            if r["kind"] == "block":
                blocks.add(key)
            else:
                taken[key] = taken.get(key, 0) + 1
        return taken, blocks

    async def hold_slot(self, *, site, d, t, capacity, kind="lead", lead_id=None, ip_hash=""):
        if self.fail:
            raise ConnectionError("db down")
        blocked = [r for r in self._active(site=site, slot_date=d, kind="block") if r["slot_time"] in ("", t)]
        taken = [r for r in self._active(site=site, slot_date=d, slot_time=t) if r["kind"] != "block"]
        if blocked or len(taken) >= capacity:
            return None
        return self._add(site, d, t, kind, lead_id, ip_hash)

    def _add(self, site, d, t, kind, lead_id=None, ip_hash=""):
        self.rows.append(dict(id=len(self.rows) + 1, site=site, slot_date=d, slot_time=t, kind=kind,
                              status="active", lead_id=lead_id, ip_hash=ip_hash))
        return len(self.rows)

    async def ip_recent_holds(self, site, ip_hash, hours=24):
        return len([r for r in self.rows if r["site"] == site and r["ip_hash"] == ip_hash and r["kind"] == "lead"])

    async def set_booking_status(self, booking_id, status):
        for r in self.rows:
            if r["id"] == booking_id:
                before, r["status"] = r["status"], status
                return {"slot_date": r["slot_date"], "slot_time": r["slot_time"], "before": before}
        return None

    async def add_block(self, site, d, t):
        self._add(site, d, t, "block")

    async def reopen(self, site, d, t, all_times):
        day_blocked = bool([r for r in self._active(site=site, slot_date=d, kind="block") if r["slot_time"] == ""])
        n = 0
        for r in self._active(site=site, slot_date=d):
            if r["kind"] in ("block", "manual") and (t == "" or r["slot_time"] in ("", t)):
                r["status"] = "cancelled"
                n += 1
        if t and day_blocked:
            for other in all_times:
                if other != t:
                    self._add(site, d, other, "block")
        return n


@pytest.fixture
def bk(monkeypatch):
    fb = FakeBookings()
    for name in ("booking_state", "hold_slot", "ip_recent_holds", "set_booking_status", "add_block", "reopen"):
        monkeypatch.setattr(db, name, getattr(fb, name))
    monkeypatch.setattr(app_module, "BOOKING_ENABLED", True)
    monkeypatch.setattr(app_module, "TELEGRAM_WEBHOOK_SECRET", "s3cret")
    monkeypatch.setattr(booking, "now_kyiv", lambda: NOW)
    return fb


def _sent(tg):
    return [p for u, p in tg.calls if u.endswith("/sendMessage")]


def _free(client, d=D, t="11:00", city="kyiv"):
    days = client.get(f"/api/chistotak-slots?city={city}").json()["days"]
    for day in days:
        if day["date"] == d.isoformat():
            for s in day["slots"]:
                if s["t"] == t:
                    return s["free"]
    return None


def _cmd(client, text, chat="222"):
    upd = {"message": {"message_id": 9, "text": text, "chat": {"id": int(chat)}}}
    return client.post("/telegram-webhook", json=upd, headers=SECRET)


# ── Чиста логіка ─────────────────────────────────────────────────────────
def test_window_hides_slots_too_close_and_counts_capacity():
    days = booking.window(NOW)
    assert len(days) == 14
    av = booking.availability(days, {(D, "11:00"): 1}, {(date(2026, 11, 4), "")}, NOW)
    today = av[0]
    assert today["date"] == "2026-11-02"
    assert [s["t"] for s in today["slots"]] == ["08:00", "11:00", "14:00"]   # 06:00 → 08:00 рівно за 2 год
    late = booking.availability(days, {}, set(), datetime(2026, 11, 2, 6, 30, tzinfo=booking.TZ))
    assert [s["t"] for s in late[0]["slots"]] == ["11:00", "14:00"]
    tue = next(x for x in av if x["date"] == "2026-11-03")
    assert {s["t"]: s["free"] for s in tue["slots"]} == {"08:00": 2, "11:00": 1, "14:00": 2}
    wed = next(x for x in av if x["date"] == "2026-11-04")
    assert all(s["free"] == 0 for s in wed["slots"])


@pytest.mark.parametrize("text,expect", [
    ("/close 05.11 14", ("close", date(2026, 11, 5), "14:00")),
    ("/close@ChistoBot 5.11", ("close", date(2026, 11, 5), "")),
    ("/book 05.11 08:00", ("book", date(2026, 11, 5), "08:00")),
    ("/open 3/11 11", ("open", date(2026, 11, 3), "11:00")),
    ("/slots", ("slots", None, "")),
    ("/close 05.01", ("close", date(2027, 1, 5), "")),
    ("привіт", ("", None, "")),
    ("/unknown 1", ("", None, "")),
])
def test_parse_command(text, expect):
    assert booking.parse_command(text, NOW) == expect


@pytest.mark.parametrize("text", ["/book 05.11", "/close 31.02", "/close 05.11 13", "/book"])
def test_parse_command_errors(text):
    with pytest.raises(booking.CmdError):
        booking.parse_command(text, NOW)


# ── Сайт: календар і заявка з часом ──────────────────────────────────────
def test_slots_endpoint_live_and_lviv_wish_only(client, bk):
    r = client.get("/api/chistotak-slots")
    assert r.headers["cache-control"] == "no-store"
    body = r.json()
    assert body["live"] is True and len(body["days"]) == 14
    assert client.get("/api/chistotak-slots?city=Львів").json()["live"] is False


def test_slots_endpoint_without_db_still_offers_wish_times(client):
    body = client.get("/api/chistotak-slots").json()
    assert body["live"] is False and body["days"]


def test_lead_with_slot_holds_place_and_sends_buttons(client, tg, bk):
    r = client.post(EP, json=lead(slot_date="2026-11-03", slot_time="11:00", **HUMAN))
    assert r.json() == {"ok": True, "q": "ok", "slot": "held"}
    msg = _sent(tg)[0]
    assert "📅 <b>Запис:</b> вт, 03.11 о 11:00" in msg["text"]
    assert msg["reply_markup"]["inline_keyboard"][0][0]["callback_data"] == "bk_ok:1"
    assert _free(client) == 1


def test_capacity_two_then_full_but_lead_never_lost(client, tg, bk):
    for i in range(2):
        r = client.post(EP, json=lead(phone=f"+38067123456{i}", slot_date="2026-11-03", slot_time="11:00", **HUMAN),
                        headers={"X-Forwarded-For": f"10.0.0.{i}"})
        assert r.json()["slot"] == "held"
    r = client.post(EP, json=lead(phone="+380671234569", slot_date="2026-11-03", slot_time="11:00", **HUMAN),
                    headers={"X-Forwarded-For": "10.0.0.9"})
    assert r.json() == {"ok": True, "q": "ok", "slot": "full"}
    last = _sent(tg)[-1]
    assert "слот уже зайнятий" in last["text"] and "reply_markup" not in last
    assert len(_sent(tg)) == 3 and _free(client) == 0


def test_suspect_and_lviv_leads_do_not_hold(client, tg, bk):
    r = client.post(EP, json=lead(name="asdf", phone="+380000000000", t_ms=300, hi=0,
                                  slot_date="2026-11-03", slot_time="11:00"))
    assert r.json()["slot"] == "wish"
    assert "не бронюю — підозріла заявка" in _sent(tg)[-1]["text"]
    r = client.post(EP, json=lead(city="Львів", slot_date="2026-11-03", slot_time="11:00", **HUMAN))
    assert r.json()["slot"] == "wish"
    assert bk.rows == [] and _free(client) == 2


def test_ip_cannot_fill_calendar(client, tg, bk):
    ip = {"X-Forwarded-For": "198.51.100.5"}
    for i, t in enumerate(["08:00", "11:00", "14:00"]):
        assert client.post(EP, json=lead(phone=f"+38067111222{i}", slot_date="2026-11-05", slot_time=t, **HUMAN),
                           headers=ip).json()["slot"] == "held"
    r = client.post(EP, json=lead(phone="+380671112229", slot_date="2026-11-06", slot_time="08:00", **HUMAN), headers=ip)
    assert r.json()["slot"] == "wish"
    assert "забагато записів" in _sent(tg)[-1]["text"]


def test_bad_or_past_slot_never_breaks_lead(client, tg, bk):
    r = client.post(EP, json=lead(slot_date="not-a-date", slot_time="11:00", **HUMAN))
    assert r.json() == {"ok": True, "q": "ok"}
    r = client.post(EP, json=lead(slot_date="2026-11-02", slot_time="07:00", **HUMAN))
    assert r.json() == {"ok": True, "q": "ok"}
    r = client.post(EP, json=lead(slot_date="2026-11-01", slot_time="11:00", **HUMAN))
    assert r.json()["slot"] == "late"


def test_db_failure_falls_back_to_wish(client, tg, bk):
    bk.fail = True
    r = client.post(EP, json=lead(slot_date="2026-11-03", slot_time="11:00", **HUMAN))
    assert r.status_code == 200 and r.json()["slot"] == "wish"
    assert "Бажаний час" in _sent(tg)[0]["text"]
    assert client.get("/api/chistotak-slots").json()["live"] is False


# ── Telegram: кнопки й команди ───────────────────────────────────────────
def test_cancel_button_frees_slot_confirm_keeps_cancel(client, tg, bk):
    client.post(EP, json=lead(slot_date="2026-11-03", slot_time="11:00", **HUMAN))
    cq = {"callback_query": {"id": "c1", "data": "bk_ok:1",
                             "message": {"message_id": 55, "chat": {"id": 222}, "text": "x"}}}
    client.post("/telegram-webhook", json=cq, headers=SECRET)
    kb = [p for u, p in tg.calls if u.endswith("/editMessageReplyMarkup")][-1]["reply_markup"]
    assert [b["callback_data"] for b in kb["inline_keyboard"][0]] == ["bk_no:1"]
    assert _free(client) == 1
    cq["callback_query"]["data"] = "bk_no:1"
    client.post("/telegram-webhook", json=cq, headers=SECRET)
    assert _free(client) == 2
    assert "місце звільнено: вт, 03.11 о 11:00" in _sent(tg)[-1]["text"]


def test_callback_from_foreign_chat_ignored(client, tg, bk):
    client.post(EP, json=lead(slot_date="2026-11-03", slot_time="11:00", **HUMAN))
    cq = {"callback_query": {"id": "c1", "data": "bk_no:1",
                             "message": {"message_id": 55, "chat": {"id": 999}}}}
    client.post("/telegram-webhook", json=cq, headers=SECRET)
    assert _free(client) == 1


def test_commands_close_open_book(client, tg, bk):
    _cmd(client, "/close 03.11")
    assert _free(client, t="08:00") == 0 and _free(client, t="14:00") == 0
    assert "увесь день" in _sent(tg)[-1]["text"]
    _cmd(client, "/open 03.11 14")
    assert _free(client, t="14:00") == 2 and _free(client, t="08:00") == 0
    _cmd(client, "/book 03.11 14:00")
    assert _free(client, t="14:00") == 1
    assert "Вільних місць лишилось: 1" in _sent(tg)[-1]["text"]
    _cmd(client, "/open 03.11")
    assert _free(client, t="08:00") == 2 and _free(client, t="14:00") == 2
    _cmd(client, "/slots")
    assert "вт, 03.11" in _sent(tg)[-1]["text"]
    _cmd(client, "/book 03.11")
    assert "потрібен час" in _sent(tg)[-1]["text"]


def test_commands_from_other_chat_ignored(client, tg, bk):
    _cmd(client, "/close 03.11", chat="12345")
    assert _free(client, t="08:00") == 2 and _sent(tg) == []


def test_webhook_db_failure_replies_not_500(client, tg, bk):
    bk.fail = True
    r = _cmd(client, "/slots")
    assert r.status_code == 200
    assert "помилка бази" in _sent(tg)[-1]["text"]


def test_db_watchdog_alerts_once_and_recovers(tg, monkeypatch):
    import asyncio
    states = iter([False, False, True])          # 1: впала, 2: досі лежить, 3: ожила
    pings, inits = [], []

    async def ping():
        v = next(states); pings.append(v); return v

    async def init_pool():
        inits.append(1); return False

    async def close_pool():
        return None

    calls = {"n": 0}
    real_sleep = asyncio.sleep

    async def fake_sleep(_s):
        calls["n"] += 1
        if calls["n"] > 3:
            raise asyncio.CancelledError
        await real_sleep(0)

    monkeypatch.setattr(db, "ping", ping)
    monkeypatch.setattr(db, "init_pool", init_pool)
    monkeypatch.setattr(db, "close_pool", close_pool)
    monkeypatch.setattr(app_module.asyncio, "sleep", fake_sleep)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(app_module._db_watchdog())
    texts = [p["text"] for p in _sent(tg)]
    assert len(texts) == 2                          # один алерт (не дублюється), одне «ожила»
    assert "база даних не відповідає" in texts[0] and "знову працює" in texts[1]
    assert len(inits) == 2                          # пробував перепідключитись, поки лежала
