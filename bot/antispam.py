"""Антиспам-движок: балльная оценка сообщения + правила из БД."""
from __future__ import annotations

import logging
import re
import time
from collections import defaultdict, deque
from dataclasses import dataclass, field

from aiogram.types import Message

from .config import Config
from .db import Database

log = logging.getLogger(__name__)

RULES_TTL_SECONDS = 60

# --- статические сигнатуры ---------------------------------------------------
RE_URL = re.compile(
    r"(https?://|www\.)\S+|\b[a-z0-9-]+\.(ru|com|net|org|io|me|cn|biz|shop|store|site|online|info|xyz|top)\b",
    re.IGNORECASE,
)
RE_TG_LINK = re.compile(r"(t\.me/|telegram\.me/|tg://)", re.IGNORECASE)
RE_PHONE = re.compile(r"(?:\+?\d[\s\-()]?){10,15}")
RE_MANY_CAPS = re.compile(r"[А-ЯA-Z]{8,}")
# Нулевой ширины и прочие невидимые символы — ими часто «разрезают» стоп-слова
_INVISIBLE_CHARS = "".join(
    chr(code)
    for code in (
        0x00AD, 0x200B, 0x200C, 0x200D, 0x200E, 0x200F,
        0x202A, 0x202B, 0x202C, 0x202D, 0x202E,
        0x2060, 0x2061, 0x2062, 0x2063, 0x2064, 0xFEFF,
    )
)
RE_INVISIBLE = re.compile("[" + _INVISIBLE_CHARS + "]")

# Символы-«обходы» фильтров: а→a, о→o и т.п.
HOMOGLYPHS = str.maketrans(
    {
        "a": "а", "e": "е", "o": "о", "p": "р", "c": "с", "y": "у", "x": "х",
        "k": "к", "m": "м", "t": "т", "b": "в", "h": "н", "3": "з", "0": "о",
    }
)


@dataclass
class Verdict:
    score: int = 0
    reasons: list[str] = field(default_factory=list)

    def add(self, points: int, reason: str) -> None:
        self.score += points
        self.reasons.append(reason)

    @property
    def reason_text(self) -> str:
        return ", ".join(self.reasons) if self.reasons else "нет"


def normalize(text: str) -> str:
    """Приводим текст к виду, устойчивому к обходам фильтра."""
    text = RE_INVISIBLE.sub("", text or "")
    text = text.lower().translate(HOMOGLYPHS)
    text = re.sub(r"[^\w\s@./:+-]", " ", text, flags=re.UNICODE)
    return re.sub(r"\s+", " ", text).strip()


class AntiSpam:
    def __init__(self, db: Database, config: Config) -> None:
        self.db = db
        self.cfg = config
        self._rules: list[tuple[str, str, int, re.Pattern | None]] = []
        self._rules_loaded_at = 0.0
        # user_id -> отметки времени последних сообщений (антифлуд)
        self._recent: dict[int, deque[float]] = defaultdict(deque)
        # user_id -> последние тексты (антиповтор)
        self._last_texts: dict[int, deque[str]] = defaultdict(lambda: deque(maxlen=4))

    # ------------------------------------------------------------- правила
    async def rules(self) -> list[tuple[str, str, int, re.Pattern | None]]:
        now = time.monotonic()
        if self._rules and now - self._rules_loaded_at < RULES_TTL_SECONDS:
            return self._rules

        compiled: list[tuple[str, str, int, re.Pattern | None]] = []
        for row in await self.db.get_spam_rules():
            pattern, kind, score = row["pattern"], row["kind"], row["score"]
            rx: re.Pattern | None = None
            if kind == "regex":
                try:
                    rx = re.compile(pattern, re.IGNORECASE | re.UNICODE)
                except re.error as exc:
                    log.warning("Некорректное regex-правило #%s (%s): %s", row["id"], pattern, exc)
                    continue
            compiled.append((pattern, kind, score, rx))

        self._rules = compiled
        self._rules_loaded_at = now
        return compiled

    def invalidate_rules(self) -> None:
        self._rules_loaded_at = 0.0

    # -------------------------------------------------------------- проверка
    async def check(self, message: Message, *, is_newbie: bool) -> Verdict:
        verdict = Verdict()
        user = message.from_user
        if user is None:
            return verdict

        text = message.text or message.caption or ""
        norm = normalize(text)

        # 1. Пересланное из канала / чужого чата — типичная реклама
        if message.forward_origin is not None:
            verdict.add(3 if is_newbie else 2, "пересланное сообщение")

        # 2. Ссылки
        if text:
            if RE_TG_LINK.search(text):
                verdict.add(3, "ссылка на Telegram-ресурс")
            elif RE_URL.search(text):
                verdict.add(3 if is_newbie else 2, "внешняя ссылка")

        # 3. Скрытые ссылки в разметке (text_link) и упоминания каналов
        for entity in (message.entities or []) + (message.caption_entities or []):
            if entity.type == "text_link":
                verdict.add(3, "скрытая ссылка в тексте")
                break
            if entity.type == "mention" and is_newbie:
                verdict.add(1, "упоминание аккаунта")

        # 4. Контакты в обход чата
        if RE_PHONE.search(text):
            verdict.add(2, "номер телефона в сообщении")

        # 5. Стоп-слова из БД (пополняются командой /spam_add)
        for pattern, kind, score, rx in await self.rules():
            if kind == "regex" and rx is not None:
                if rx.search(text) or rx.search(norm):
                    verdict.add(score, f"правило «{pattern}»")
            elif pattern in norm:
                verdict.add(score, f"стоп-слово «{pattern}»")

        # 6. Приглашение в личку (динамические формулировки)
        if re.search(r"\b(лс|личк\w*|дирек\w*|dm|direct)\b", norm) and re.search(
            r"\b(пиш\w+|напиш\w+|скинь\w*|обращ\w+|жду|стуч\w+)\b", norm
        ):
            verdict.add(3, "предложение написать в личные сообщения")

        # 7. Капс и невидимые символы
        if RE_MANY_CAPS.search(text) and len(text) > 40:
            verdict.add(1, "избыточный капс")
        if RE_INVISIBLE.search(text):
            verdict.add(2, "скрытые символы (обход фильтра)")

        # 8. Флуд и повторы
        now = time.monotonic()
        marks = self._recent[user.id]
        marks.append(now)
        while marks and now - marks[0] > self.cfg.flood_seconds:
            marks.popleft()
        if len(marks) > self.cfg.flood_messages:
            verdict.add(3, "флуд (слишком много сообщений подряд)")

        if norm and len(norm) > 15:
            history = self._last_texts[user.id]
            if history.count(norm) >= 2:
                verdict.add(3, "повтор одного и того же сообщения")
            history.append(norm)

        # 9. Новичок, который сразу постит рекламный «блок»
        if is_newbie and len(text) > 350 and verdict.score > 0:
            verdict.add(1, "длинное рекламное сообщение от новичка")

        return verdict

    def is_spam(self, verdict: Verdict) -> bool:
        return verdict.score >= self.cfg.spam_score_threshold

    def forget(self, user_id: int) -> None:
        self._recent.pop(user_id, None)
        self._last_texts.pop(user_id, None)
