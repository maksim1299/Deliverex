"""Распознавание и разбор заявок на просчёт доставки."""
from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

NUM = r"(\d+(?:[.,]\d+)?)"

# Вес: «120 кг», «вес 1.5 т», «весом 300кг»
RE_WEIGHT = re.compile(rf"{NUM}\s*(кг|kg|килограмм\w*|тонн\w*|т\b)", re.IGNORECASE)
RE_WEIGHT_LABEL = re.compile(rf"вес\w*\s*[:\-]?\s*{NUM}", re.IGNORECASE)

# Объём: «1.2 м3», «0,5 куб», «объем 2 м³»
RE_VOLUME = re.compile(
    rf"{NUM}\s*(м3|м\^3|м³|куб\w*|m3|cbm)", re.IGNORECASE
)
RE_VOLUME_LABEL = re.compile(rf"(?:объ[её]м\w*|обьем\w*)\s*[:\-]?\s*{NUM}", re.IGNORECASE)

RE_CATEGORY_LABEL = re.compile(
    r"(?:категори\w*|товар|груз|продукц\w*)\s*[:\-]?\s*([^\n,;]{2,80})", re.IGNORECASE
)

# Слова, по которым сообщение опознаётся как заявка даже без чётких цифр
LEAD_KEYWORDS = (
    "рассчит", "расчет", "расчёт", "просчит", "просчет", "просчёт", "посчит",
    "стоимость доставки", "сколько будет стоить", "цена доставки", "заявка",
    "хочу привезти", "нужно привезти", "доставить из китая", "тариф",
)

CATEGORY_HINTS = (
    "одежд", "обув", "электрон", "техник", "мебел", "игрушк", "космет", "аксессуар",
    "инструмент", "запчаст", "текстил", "посуд", "спорт", "сумк", "часы", "бижутер",
    "упаковк", "стройматериал", "автозапчаст", "телефон", "наушник", "чехл",
)


@dataclass
class ParsedLead:
    category: str | None
    weight_kg: Decimal | None
    volume_m3: Decimal | None
    raw_text: str

    @property
    def has_params(self) -> bool:
        """Заявка считается полной, если есть хотя бы вес или объём."""
        return self.weight_kg is not None or self.volume_m3 is not None

    @property
    def field_count(self) -> int:
        return sum(x is not None for x in (self.category, self.weight_kg, self.volume_m3))


def _to_decimal(raw: str) -> Decimal | None:
    try:
        return Decimal(raw.replace(",", "."))
    except (InvalidOperation, AttributeError):
        return None


def _extract_weight(text: str) -> Decimal | None:
    match = RE_WEIGHT.search(text)
    if match:
        value = _to_decimal(match.group(1))
        unit = match.group(2).lower()
        if value is not None and unit.startswith(("тонн", "т")):
            value *= 1000  # приводим тонны к килограммам
        return value
    match = RE_WEIGHT_LABEL.search(text)
    return _to_decimal(match.group(1)) if match else None


def _extract_volume(text: str) -> Decimal | None:
    match = RE_VOLUME.search(text)
    if match:
        return _to_decimal(match.group(1))
    match = RE_VOLUME_LABEL.search(text)
    return _to_decimal(match.group(1)) if match else None


def _extract_category(text: str) -> str | None:
    match = RE_CATEGORY_LABEL.search(text)
    if match:
        candidate = match.group(1).strip(" .:-—")
        if candidate:
            return candidate[:120]

    lowered = text.lower()
    for hint in CATEGORY_HINTS:
        if hint in lowered:
            # возвращаем строку, в которой встретилась подсказка
            for line in text.splitlines():
                if hint in line.lower():
                    return line.strip(" .:-—")[:120]
    return None


def parse_lead(text: str) -> ParsedLead:
    text = (text or "").strip()
    return ParsedLead(
        category=_extract_category(text),
        weight_kg=_extract_weight(text),
        volume_m3=_extract_volume(text),
        raw_text=text,
    )


def looks_like_lead(text: str) -> bool:
    """Похоже ли сообщение на запрос расчёта (для чата, где идёт и болталка)."""
    if not text or len(text.strip()) < 5:
        return False
    lowered = text.lower()

    if any(keyword in lowered for keyword in LEAD_KEYWORDS):
        return True

    parsed = parse_lead(text)
    if parsed.weight_kg is not None and parsed.volume_m3 is not None:
        return True
    if parsed.weight_kg is not None and parsed.category is not None:
        return True
    if parsed.volume_m3 is not None and parsed.category is not None:
        return True
    return False


def format_weight(value: Decimal | None) -> str:
    if value is None:
        return "не указан"
    return f"{value.normalize():f} кг".replace("E+", "e")


def format_volume(value: Decimal | None) -> str:
    if value is None:
        return "не указан"
    return f"{value.normalize():f} м³".replace("E+", "e")
