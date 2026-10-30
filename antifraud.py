"""Антифрод заявок: оцінка «підозрілості» без зайвих відмов живим людям.

Ідея: жоден окремий сигнал не робить заявку підозрілою (люди помиляються, швидко друкують,
користуються автозаповненням). Підозріла — коли набирається SUSPECT_SCORE балів.
Підозрілі заявки все одно доходять у Telegram (з позначкою ⚠️), але фронт не зараховує
їх як конверсію в рекламу. «Клоака»: якщо з одного IP за добу вже було BLOCK_AFTER
підозрілих заявок, наступні підозрілі з нього тихо відкидаються (бот бачить «успіх»).
Нормальні (не підозрілі) заявки з такого IP все одно доходять — на випадок спільного
мобільного IP.
"""
from __future__ import annotations

import re
import time
from collections import defaultdict, deque

SUSPECT_SCORE = 2
BLOCK_AFTER = 3            # підозрілих заявок з одного IP за вікно — далі тихо відкидаємо
WINDOW_SEC = 24 * 3600

# Коди мобільних операторів України (після +380)
UA_MOBILE = {'39', '50', '63', '66', '67', '68', '73', '77', '89', '91', '92', '93', '94', '95', '96', '97', '98', '99'}


def normalize_phone(raw: str) -> str:
    """До вигляду +380XXXXXXXXX для українських номерів; інакше +цифри / цифри."""
    plus = raw.strip().startswith('+')
    d = re.sub(r'\D', '', raw or '')
    if len(d) == 10 and d.startswith('0'):
        d = '38' + d
    elif len(d) == 9:
        d = '380' + d
    elif len(d) == 11 and d.startswith('80'):
        d = '3' + d
    return ('+' + d) if (d.startswith('380') or plus) else d


def phone_kind(raw: str) -> str:
    """ua_mobile | ua_landline | intl | invalid"""
    p = normalize_phone(raw)
    d = p.lstrip('+')
    if d.startswith('380'):
        if len(d) != 12:
            return 'invalid'
        rest = d[3:]
        if len(set(rest)) <= 2:            # 0000000000, 1111111 тощо
            return 'invalid'
        if rest[:2] in UA_MOBILE:
            return 'ua_mobile'
        if rest[0] in '3456':              # міські коди (44 — Київ, 32 — Львів, …)
            return 'ua_landline'
        return 'invalid'
    if p.startswith('+') and 10 <= len(d) <= 15 and len(set(d)) > 2:
        return 'intl'
    return 'invalid'


_KEYBOARD = ('qwer', 'asdf', 'zxcv', 'йцук', 'фыва', 'фіва', 'ячсм', 'qwerty', 'asdfg')


def name_is_gibberish(name: str) -> bool:
    n = (name or '').strip().lower()
    if not n:
        return True
    if 'зворотний дзвінок' in n:             # службове імʼя з попапу
        return False
    letters = re.sub(r'[^a-zа-яіїєґʼ\']', '', n)
    if len(letters) < 2:
        return True
    if re.search(r'\d{2,}', n):
        return True
    if re.search(r'(.)\1\1', letters):       # ааа, fff
        return True
    if any(k in letters for k in _KEYBOARD):
        return True
    if re.fullmatch(r'[a-z]+', letters) and len(letters) >= 5 and not re.search(r'[aeiouy]', letters):
        return True
    return False


_URL = re.compile(r'(https?://|www\.|\.ru\b|\.com\b|t\.me/|<a\s)', re.I)


class Guard:
    """Стан у памʼяті процесу (скидається при рестарті — це нормально)."""

    def __init__(self) -> None:
        self.phones: dict[str, float] = {}
        self.ip_suspects: dict[str, deque] = defaultdict(deque)
        self.ip_block_notified: dict[str, float] = {}

    def _gc(self, now: float) -> None:
        cutoff = now - WINDOW_SEC
        for k in [k for k, t in self.phones.items() if t < cutoff]:
            del self.phones[k]
        for ip in list(self.ip_suspects):
            q = self.ip_suspects[ip]
            while q and q[0] < cutoff:
                q.popleft()
            if not q:
                del self.ip_suspects[ip]

    def assess(self, *, name: str, phone: str, details: str, address: str,
               t_ms: int | None, hi: int | None, ip: str, now: float | None = None) -> dict:
        now = time.time() if now is None else now
        self._gc(now)
        score, reasons = 0, []

        if t_ms is not None:
            if t_ms < 1500:
                score += 2; reasons.append(f'відправлено за {t_ms / 1000:.1f} с після відкриття сторінки')
            elif t_ms < 4000:
                score += 1; reasons.append(f'відправлено дуже швидко ({t_ms / 1000:.1f} с)')
        if hi is not None and hi == 0:
            score += 1; reasons.append('жодного натискання / дотику на сторінці')

        kind = phone_kind(phone)
        if kind == 'invalid':
            score += 2; reasons.append('неіснуючий номер')
        elif kind == 'intl':
            score += 1; reasons.append('іноземний номер')

        if name_is_gibberish(name):
            score += 1; reasons.append('імʼя схоже на набір символів')
        if _URL.search(f'{name} {details} {address}'):
            score += 2; reasons.append('посилання в тексті заявки')

        norm = normalize_phone(phone)
        repeat = norm in self.phones
        if repeat:
            score += 1; reasons.append('цей номер уже залишав заявку за добу')
        self.phones[norm] = now

        suspect = score >= SUSPECT_SCORE
        prior = len(self.ip_suspects.get(ip, ())) if ip else 0
        blocked = bool(suspect and ip and prior >= BLOCK_AFTER)
        notify_block = False
        if suspect and ip:
            self.ip_suspects[ip].append(now)
            if blocked and now - self.ip_block_notified.get(ip, 0) > WINDOW_SEC:
                self.ip_block_notified[ip] = now
                notify_block = True
        return {
            'score': score, 'reasons': reasons, 'suspect': suspect, 'blocked': blocked,
            'notify_block': notify_block, 'phone_kind': kind, 'repeat': repeat,
            'ip_suspects': prior + (1 if suspect and ip else 0),
        }
