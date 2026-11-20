"""Відгуки з Google (профіль компанії) для блоку на сайті ЧистоТак.

Places API (New): рейтинг, кількість відгуків і до 5 відгуків, які Google вважає найрелевантнішими.
Ключ — лише з env (GOOGLE_PLACES_KEY), у відповідь сайту не потрапляє. Кеш 12 год: сайт може
питати скільки завгодно, а до Google ходимо ~2 рази на добу (у межах безкоштовного ліміту).
Якщо Google тимчасово недоступний — віддаємо останні збережені дані.
"""
from __future__ import annotations

import logging
import os
import time
from typing import Any

import httpx

log = logging.getLogger("clean-form")

API_KEY = os.environ.get("GOOGLE_PLACES_KEY", "")
PLACE_ID = os.environ.get("GOOGLE_PLACE_ID", "")
# Якщо PLACE_ID не задано — один раз знаходимо профіль за назвою й адресою
SEARCH_QUERY = os.environ.get("GOOGLE_PLACE_QUERY", "ЧистоТак клінінг Київ вулиця Лятошинського 8")
CACHE_SEC = 12 * 3600
RETRY_SEC = 15 * 60  # після збою не смикаємо Google частіше, ніж раз на 15 хв

_BASE = "https://places.googleapis.com/v1"
_FIELDS = "id,rating,userRatingCount,reviews,googleMapsUri"
_cache: dict[str, Any] = {"data": None, "at": 0.0, "tried": 0.0, "place_id": PLACE_ID}


def enabled() -> bool:
    return bool(API_KEY)


async def _request(method: str, url: str, *, fields: str, json: dict | None = None,
                   params: dict | None = None) -> dict:
    """Єдина точка виходу до Google (тести її підміняють)."""
    headers = {"X-Goog-Api-Key": API_KEY, "X-Goog-FieldMask": fields}
    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.request(method, url, headers=headers, json=json, params=params)
    r.raise_for_status()
    return r.json()


async def _place_id() -> str:
    if _cache["place_id"]:
        return _cache["place_id"]
    j = await _request("POST", f"{_BASE}/places:searchText", fields="places.id,places.displayName,places.formattedAddress",
                       json={"textQuery": SEARCH_QUERY, "languageCode": "uk"})
    places = j.get("places") or []
    if not places:
        raise LookupError("place not found")
    _cache["place_id"] = places[0]["id"]
    log.info("google place found: %s (%s)", places[0].get("displayName", {}).get("text", ""),
             places[0].get("formattedAddress", ""))
    return _cache["place_id"]


def _shape(j: dict, place_id: str) -> dict:
    reviews = []
    for r in j.get("reviews") or []:
        text = ((r.get("text") or r.get("originalText") or {}).get("text") or "").strip()
        if not text:
            continue
        a = r.get("authorAttribution") or {}
        reviews.append({
            "author": a.get("displayName") or "Клієнт Google",
            "photo": a.get("photoUri") or "",
            "profile": a.get("uri") or "",
            "rating": int(r.get("rating") or 0),
            "text": text[:600],
            "when": r.get("relativePublishTimeDescription") or "",
            "time": r.get("publishTime") or "",
        })
    return {
        "ok": True,
        "rating": round(float(j.get("rating") or 0), 1),
        "count": int(j.get("userRatingCount") or 0),
        "url": j.get("googleMapsUri") or f"https://www.google.com/maps/place/?q=place_id:{place_id}",
        "write_url": f"https://search.google.com/local/writereview?placeid={place_id}",
        "reviews": reviews,
    }


async def get(now: float | None = None) -> dict:
    """Дані для сайту. Без ключа або без жодних даних — {"ok": False}, блок на сайті не показується."""
    now = time.time() if now is None else now
    if not enabled():
        return {"ok": False}
    fresh = _cache["data"] is not None and now - _cache["at"] < CACHE_SEC
    if fresh or now - _cache["tried"] < RETRY_SEC:
        return _cache["data"] or {"ok": False}
    _cache["tried"] = now
    try:
        pid = await _place_id()
        j = await _request("GET", f"{_BASE}/places/{pid}", fields=_FIELDS, params={"languageCode": "uk"})
        _cache["data"], _cache["at"] = _shape(j, pid), now
    except Exception as exc:  # noqa: BLE001 — сайт працює й без блоку відгуків
        log.error("google reviews fetch failed: %s", type(exc).__name__)
    return _cache["data"] or {"ok": False}
