"""
Онлайн-запис ЧистоТак (Київ): слоти 08:00 / 11:00 / 14:00 щодня на 14 днів уперед,
на кожен слот — BOOKING_CAPACITY бригад. Тут лише чиста логіка (дати, розбір команд
менеджера, форматування) — без мережі й БД, щоб її легко було тестувати.

Збереження — db.py (таблиця bookings). Записи бувають трьох видів:
  lead   — клієнт обрав час на сайті (займає 1 місце);
  manual — менеджер записав клієнта по телефону: /book 05.11 14:00 (займає 1 місце);
  block  — менеджер закрив слот або весь день: /close 05.11 [14:00] (місць 0).
"""
from __future__ import annotations

import os
import re
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

TZ = ZoneInfo("Europe/Kyiv")
TIMES = ("08:00", "11:00", "14:00")
DAYS_AHEAD = 14
LEAD_HOURS = 2          # не пропонуємо слот, до початку якого лишилось менше 2 год
CAPACITY = max(1, int(os.environ.get("BOOKING_CAPACITY", "2")))
IP_HOLDS_PER_DAY = 3    # захист від «забивання» календаря з одного IP

_WD = ("пн", "вт", "ср", "чт", "пт", "сб", "нд")


def now_kyiv() -> datetime:
    return datetime.now(TZ)


def slot_start(d: date, t: str) -> datetime:
    hh, mm = map(int, t.split(":"))
    return datetime(d.year, d.month, d.day, hh, mm, tzinfo=TZ)


def bookable(d: date, t: str, now: datetime) -> bool:
    if t not in TIMES:
        return False
    if d > now.date() + timedelta(days=DAYS_AHEAD - 1):
        return False
    return slot_start(d, t) - now >= timedelta(hours=LEAD_HOURS)


def window(now: datetime) -> list[date]:
    return [now.date() + timedelta(days=i) for i in range(DAYS_AHEAD)]


def parse_form_slot(slot_date: str, slot_time: str) -> tuple[date, str] | None:
    """Значення з форми: '2026-11-03' + '11:00'. Некоректне -> None (заявку не валимо)."""
    try:
        d = date.fromisoformat((slot_date or "").strip())
    except ValueError:
        return None
    t = (slot_time or "").strip()
    return (d, t) if t in TIMES else None


def fmt_slot(d: date, t: str = "") -> str:
    s = f"{_WD[d.weekday()]}, {d:%d.%m}"
    return f"{s} о {t}" if t else s


def is_lviv(city: str) -> bool:
    c = (city or "").lower()
    return "льв" in c or "lviv" in c


# ── Команди менеджера в Telegram ─────────────────────────────────────────
HELP = (
    "📅 Керування записом:\n"
    "/slots — вільні місця на 7 днів\n"
    "/book 05.11 14:00 — записали клієнта по телефону (−1 місце)\n"
    "/close 05.11 14:00 — закрити слот; /close 05.11 — закрити весь день\n"
    "/open 05.11 14:00 — відкрити слот (знімає закриття й записи по телефону); /open 05.11 — весь день\n"
    "Час: 8, 11 або 14 (можна 08:00)."
)


class CmdError(ValueError):
    pass


def _parse_date(tok: str, now: datetime) -> date:
    m = re.fullmatch(r"(\d{1,2})[./](\d{1,2})(?:[./](\d{2,4}))?", tok)
    if not m:
        raise CmdError(f"Не розумію дату «{tok}». Формат: 05.11")
    dd, mm, yy = int(m.group(1)), int(m.group(2)), m.group(3)
    year = (2000 + int(yy) if len(yy) == 2 else int(yy)) if yy else now.year
    try:
        d = date(year, mm, dd)
    except ValueError as exc:
        raise CmdError(f"Немає такої дати: {tok}") from exc
    if not yy and d < now.date() - timedelta(days=31):
        d = date(year + 1, mm, dd)       # «05.01» у грудні — це наступний рік
    return d


def _parse_time(tok: str) -> str:
    m = re.fullmatch(r"(\d{1,2})(?::(\d{2}))?", tok)
    t = f"{int(m.group(1)):02d}:{m.group(2) or '00'}" if m else ""
    if t not in TIMES:
        raise CmdError(f"Немає слоту «{tok}». Доступні: {', '.join(TIMES)}")
    return t


def parse_command(text: str, now: datetime) -> tuple[str, date | None, str]:
    """'/close@bot 05.11 14' -> ('close', date, '14:00'). Невідома команда -> ('', None, '')."""
    parts = (text or "").strip().split()
    if not parts or not parts[0].startswith("/"):
        return "", None, ""
    cmd = parts[0][1:].split("@", 1)[0].lower()
    args = parts[1:]
    if cmd in ("slots", "help", "start"):
        return cmd, None, ""
    if cmd not in ("book", "close", "open"):
        return "", None, ""
    if not args:
        raise CmdError(f"Вкажіть дату: /{cmd} 05.11" + (" 14:00" if cmd == "book" else " [14:00]"))
    d = _parse_date(args[0], now)
    t = _parse_time(args[1]) if len(args) > 1 else ""
    if cmd == "book" and not t:
        raise CmdError("Для /book потрібен час: /book 05.11 14:00")
    return cmd, d, t


def availability(days: list[date], taken: dict[tuple[date, str], int],
                 blocks: set[tuple[date, str]], now: datetime) -> list[dict]:
    """taken[(d,t)] = зайнято місць; blocks — {(d,'')} весь день або {(d,t)} слот."""
    out = []
    for d in days:
        slots = []
        for t in TIMES:
            if not bookable(d, t, now):
                continue
            free = 0 if ((d, "") in blocks or (d, t) in blocks) else max(0, CAPACITY - taken.get((d, t), 0))
            slots.append({"t": t, "free": free})
        if slots:
            out.append({"date": d.isoformat(), "slots": slots})
    return out


def slots_report(days: list[date], taken, blocks, now: datetime) -> str:
    lines = ["📅 Вільні місця (бригад на слот: %d):" % CAPACITY]
    for day in availability(days[:7], taken, blocks, now):
        d = date.fromisoformat(day["date"])
        cells = []
        for s in day["slots"]:
            closed = (d, "") in blocks or (d, s["t"]) in blocks
            cells.append(f"{s['t'][:2]} {'⛔' if closed else ('✅' + str(s['free']) if s['free'] else '❌')}")
        lines.append(f"{fmt_slot(d)}: " + " · ".join(cells))
    return "\n".join(lines)
