"""Блок відгуків Google: кеш, пошук профілю, збої Google, без ключа — нічого не показуємо."""
import asyncio

import pytest

import google_reviews as gr
from test_app import app_module, TestClient  # noqa: F401 — те саме оточення, що й основні тести

PLACE = {
    "id": "ChIJtest",
    "rating": 4.86,
    "userRatingCount": 37,
    "googleMapsUri": "https://maps.google.com/?cid=1",
    "reviews": [
        {"rating": 5, "text": {"text": "Дуже чисто, дякую!"}, "relativePublishTimeDescription": "тиждень тому",
         "publishTime": "2026-11-10T10:00:00Z", "authorAttribution": {"displayName": "Олена К.", "photoUri": "https://x/p.jpg"}},
        {"rating": 4, "text": {"text": ""}, "authorAttribution": {"displayName": "Без тексту"}},
    ],
}


@pytest.fixture
def fake(monkeypatch):
    class Calls(list):
        fail = False
    calls = Calls()

    async def _request(method, url, *, fields, json=None, params=None):
        calls.append((method, url))
        if calls.fail:
            raise RuntimeError("google down")
        if url.endswith(":searchText"):
            return {"places": [{"id": "ChIJfound", "displayName": {"text": "ЧистоТак"}, "formattedAddress": "Київ"}]}
        return PLACE

    monkeypatch.setattr(gr, "_request", _request)
    monkeypatch.setattr(gr, "API_KEY", "k")
    monkeypatch.setattr(gr, "_cache", {"data": None, "at": 0.0, "tried": 0.0, "place_id": ""})
    return calls


def run(coro):
    return asyncio.run(coro)


def test_no_key_shows_nothing(monkeypatch):
    monkeypatch.setattr(gr, "API_KEY", "")
    assert run(gr.get()) == {"ok": False}


def test_finds_place_then_caches(fake):
    d = run(gr.get(now=1000))
    assert d["ok"] and d["rating"] == 4.9 and d["count"] == 37
    assert [r["author"] for r in d["reviews"]] == ["Олена К."]  # відгук без тексту не показуємо
    assert d["write_url"].endswith("placeid=ChIJfound")
    n = len(fake)
    run(gr.get(now=1000 + 3600))  # у межах 12 год — без запитів до Google
    assert len(fake) == n
    run(gr.get(now=1000 + gr.CACHE_SEC + 1))
    assert len(fake) == n + 1  # профіль уже знайдено — лише один запит


def test_google_down_serves_stale(fake):
    first = run(gr.get(now=1000))
    fake.fail = True
    later = run(gr.get(now=1000 + gr.CACHE_SEC + 1))
    assert later == first


def test_google_down_without_cache_is_quiet_and_throttled(fake):
    fake.fail = True
    assert run(gr.get(now=1000)) == {"ok": False}
    n = len(fake)
    run(gr.get(now=1000 + 60))  # повтор не раніше ніж за 15 хв
    assert len(fake) == n


def test_endpoint(fake):
    r = TestClient(app_module.app).get("/api/google-reviews")
    assert r.status_code == 200 and r.json()["count"] == 37
    assert "max-age" in r.headers["cache-control"]
