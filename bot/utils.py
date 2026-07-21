"""Вспомогательные функции: права, безопасные вызовы API, доставка заявок."""
from __future__ import annotations

import asyncio
import html
import logging
import time
from datetime import datetime, timedelta, timezone

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest, TelegramForbiddenError
from aiogram.types import Message, User

from . import texts
from .config import Config
from .db import Database
from .leads import format_volume, format_weight

log = logging.getLogger(__name__)

MSK = timezone(timedelta(hours=3))
ADMIN_CACHE_TTL = 300

_admin_cache: dict[int, tuple[float, set[int]]] = {}

SOURCE_LABELS = {
    "group": "чат Deliverex",
    "dm": "личные сообщения боту",
    "command": "команда /заявка",
}


def esc(text: str | None) -> str:
    return html.escape(text or "", quote=False)


def full_name(user: User) -> str:
    parts = [user.first_name or "", user.last_name or ""]
    name = " ".join(p for p in parts if p).strip()
    return name or f"id{user.id}"


def mention(user: User) -> str:
    return f'<a href="tg://user?id={user.id}">{esc(full_name(user))}</a>'


def username_suffix(username: str | None) -> str:
    return f" (@{esc(username)})" if username else ""


# ---------------------------------------------------------------------- права
async def chat_admin_ids(bot: Bot, chat_id: int) -> set[int]:
    cached = _admin_cache.get(chat_id)
    now = time.monotonic()
    if cached and now - cached[0] < ADMIN_CACHE_TTL:
        return cached[1]
    try:
        members = await bot.get_chat_administrators(chat_id)
        ids = {m.user.id for m in members}
    except TelegramAPIError as exc:
        log.warning("Не удалось получить админов чата %s: %s", chat_id, exc)
        ids = cached[1] if cached else set()
    _admin_cache[chat_id] = (now, ids)
    return ids


def invalidate_admin_cache(chat_id: int) -> None:
    _admin_cache.pop(chat_id, None)


async def is_privileged(bot: Bot, db: Database, cfg: Config, user_id: int, chat_id: int | None) -> bool:
    """Владелец/админ группы, ADMIN_IDS из .env, менеджер или доверенный пользователь."""
    if user_id in cfg.admin_ids:
        return True
    # список админов кэшируется, поэтому проверяем его до обращения к БД
    if chat_id is not None and chat_id < 0 and user_id in await chat_admin_ids(bot, chat_id):
        return True
    return await db.is_exempt(user_id)


# ------------------------------------------------------- безопасные вызовы API
async def safe_delete(message: Message) -> bool:
    try:
        await message.delete()
        return True
    except TelegramAPIError as exc:
        log.debug("Не удалось удалить сообщение %s: %s", message.message_id, exc)
        return False


async def delete_later(bot: Bot, chat_id: int, message_id: int, delay: int) -> None:
    if delay <= 0:
        return
    await asyncio.sleep(delay)
    try:
        await bot.delete_message(chat_id, message_id)
    except TelegramAPIError:
        pass


def schedule_delete(bot: Bot, chat_id: int, message_id: int, delay: int) -> None:
    if delay > 0:
        asyncio.create_task(delete_later(bot, chat_id, message_id, delay))


async def safe_send(bot: Bot, chat_id: int, text: str, **kwargs) -> Message | None:
    try:
        return await bot.send_message(chat_id, text, **kwargs)
    except TelegramForbiddenError:
        log.info("Чат %s заблокировал бота — сообщение не отправлено", chat_id)
    except TelegramBadRequest as exc:
        log.warning("Ошибка отправки в %s: %s", chat_id, exc)
    except TelegramAPIError as exc:
        log.warning("Telegram API error при отправке в %s: %s", chat_id, exc)
    return None


# ------------------------------------------------------------- доставка заявок
async def resolve_manager_chats(db: Database, cfg: Config) -> list[int]:
    """Куда слать заявки: MANAGER_CHAT_ID из .env + все, кто нажал /start как менеджер."""
    targets: list[int] = []
    if cfg.manager_chat_id:
        targets.append(cfg.manager_chat_id)
    for chat_id in await db.manager_chat_ids():
        if chat_id not in targets:
            targets.append(chat_id)
    # Карточка заявки уходит только в личку менеджеру/админу. Групповые чаты
    # (у них отрицательный chat_id) исключаем, чтобы её не видели клиенты в группе.
    targets = [chat_id for chat_id in targets if chat_id > 0]
    if not targets:
        # крайний случай — шлём первому админу из .env
        targets.extend(sorted(cfg.admin_ids)[:1])
    return targets


def render_lead_card(lead, user: User, source: str) -> str:
    created = lead["created_at"]
    created_local = created.astimezone(MSK) if created else datetime.now(MSK)
    return texts.LEAD_CARD.format(
        lead_id=lead["id"],
        user_id=user.id,
        full_name=esc(full_name(user)),
        username=username_suffix(user.username),
        source=SOURCE_LABELS.get(source, source),
        category=esc(lead["category"]) if lead["category"] else "не указана",
        weight=format_weight(lead["weight_kg"]),
        volume=format_volume(lead["volume_m3"]),
        raw_text=esc(lead["raw_text"])[:2000],
        created_at=created_local.strftime("%d.%m.%Y %H:%M МСК"),
    )


async def deliver_lead(bot: Bot, db: Database, cfg: Config, lead, user: User, source: str) -> bool:
    """Отправляет карточку заявки менеджеру(ам). Возвращает True, если хоть куда-то дошло."""
    card = render_lead_card(lead, user, source)
    delivered_msg_id: int | None = None

    for chat_id in await resolve_manager_chats(db, cfg):
        sent = await safe_send(bot, chat_id, card, disable_web_page_preview=True)
        if sent and delivered_msg_id is None:
            delivered_msg_id = sent.message_id

    if delivered_msg_id is not None:
        await db.mark_lead_forwarded(lead["id"], delivered_msg_id)
        return True

    log.error(
        "Заявка #%s не доставлена: нет доступных получателей. "
        "Менеджер @%s должен написать боту /start в личку.",
        lead["id"], cfg.manager_username,
    )
    return False
