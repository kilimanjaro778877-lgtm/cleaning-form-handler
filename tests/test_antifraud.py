"""Антифрод: живі люди не страждають, боти й скликування відсіюються."""
import pytest

import antifraud
from tests.test_app import lead, client, tg, _isolate  # noqa: F401  (фікстури)

EP = "/api/chistotak-order"


def _human(**over):
    return lead(t_ms=45000, hi=37, **over)


def _bot(**over):
    base = dict(name="asdf", phone="+380000000000", t_ms=300, hi=0)
    base.update(over)
    return lead(**base)


def _texts(tg):
    return [c[1].get("text", "") for c in tg.calls if "sendMessage" in c[0]]


# ── Модуль оцінки ────────────────────────────────────────────────────────
@pytest.mark.parametrize("raw,kind", [
    ("+380501234567", "ua_mobile"), ("0501234567", "ua_mobile"), ("050 123 45 67", "ua_mobile"),
    ("+38 (073) 131-22-28", "ua_mobile"), ("0442345678", "ua_landline"),
    ("+380000000000", "invalid"), ("050123456", "invalid"), ("12345", "invalid"),
    ("+48 512 345 678", "intl"),
])
def test_phone_kind(raw, kind):
    assert antifraud.phone_kind(raw) == kind


@pytest.mark.parametrize("name,bad", [
    ("Олена", False), ("Іван Петренко", False), ("Alex", False), ("Анна-Марія", False),
    ("Зворотний дзвінок (спливаюче вікно)", False),
    ("asdf", True), ("qwerty", True), ("ааааа", True), ("123", True), ("x", True), ("bcdfgh", True),
])
def test_name_gibberish(name, bad):
    assert antifraud.name_is_gibberish(name) is bad


def test_single_signal_is_not_suspect():
    g = antifraud.Guard()
    # швидко надіслав (автозаповнення), але все інше нормальне — НЕ підозріла
    v = g.assess(name="Олена", phone="0501234567", details="", address="", t_ms=2500, hi=5, ip="1.1.1.1")
    assert v["suspect"] is False and v["score"] == 1
    # іноземний номер сам по собі — теж ні
    v = g.assess(name="John", phone="+48512345678", details="", address="", t_ms=60000, hi=20, ip="1.1.1.2")
    assert v["suspect"] is False


# ── Через API ────────────────────────────────────────────────────────────
def test_real_person_ok_and_no_warning(client, tg):
    r = client.post(EP, json=_human())
    assert r.status_code == 200 and r.json() == {"ok": True, "q": "ok"}
    text = _texts(tg)[0]
    assert "ПІДОЗРІЛА" not in text and "IP:" not in text


def test_clean_clean_without_signals_is_ok(client, tg):
    # фронт Clean-Clean не шле t_ms/hi — за їх відсутність не штрафуємо
    r = client.post("/api/order", json=lead(name="Марина", phone="0671234567"))
    assert r.json()["q"] == "ok"


def test_bot_is_marked_but_delivered_with_ip(client, tg):
    r = client.post(EP, json=_bot(), headers={"X-Forwarded-For": "203.0.113.9"})
    assert r.status_code == 200 and r.json()["q"] == "suspect"
    text = _texts(tg)[0]
    assert "ПІДОЗРІЛА ЗАЯВКА" in text and "203.0.113.9" in text and "неіснуючий номер" in text


def test_typo_phone_from_real_person_still_delivered(client, tg):
    # людина помилилась у номері (фронт підкаже, але якщо все ж дійшло) — заявка не губиться
    r = client.post(EP, json=_human(phone="050123456"))
    assert r.json()["q"] == "suspect"
    assert len(_texts(tg)) == 1


def test_link_spam_is_suspect(client, tg):
    r = client.post(EP, json=_human(details="Дешеві кредити https://spam.example"))
    assert r.json()["q"] == "suspect"


def test_cloak_blocks_repeat_spam_from_one_ip_but_not_humans(client, tg):
    ip = {"X-Forwarded-For": "198.51.100.7"}
    for i in range(3):           # перші 3 сміттєві — доходять з позначкою
        assert client.post(EP, json=_bot(), headers=ip).json()["q"] == "suspect"
    assert len(_texts(tg)) == 3
    r = client.post(EP, json=_bot(), headers=ip)          # 4-та — тихо відкинута
    assert r.status_code == 200 and r.json() == {"ok": True, "q": "suspect"}
    texts = _texts(tg)
    assert len(texts) == 4 and "Спам-заявки з IP" in texts[-1]   # лише одне повідомлення про блок
    client.post(EP, json=_bot(), headers=ip)               # 5-та — тихо, без повідомлень
    assert len(_texts(tg)) == 4
    # жива людина з того ж (спільного мобільного) IP — доходить
    r = client.post(EP, json=_human(name="Ольга", phone="0931112233"), headers=ip)
    assert r.json()["q"] == "ok" and len(_texts(tg)) == 5


def test_other_ip_not_affected_by_block(client, tg):
    for _ in range(5):
        client.post(EP, json=_bot(), headers={"X-Forwarded-For": "198.51.100.8"})
    r = client.post(EP, json=_bot(phone="0661112233", name="Тест"), headers={"X-Forwarded-For": "198.51.100.99"})
    assert r.json()["q"] == "suspect"          # підозріла (швидко + без дій), але НЕ заблокована
    assert "ПІДОЗРІЛА" in _texts(tg)[-1]
