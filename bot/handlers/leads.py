"""Приём заявок на просчёт и пересылка их менеджеру."""
from __future__ import annotations

import logging

from aiogram import Bot, F, Router
from aiogram.filters import Command
from aiogram.types import Message

from .. import texts
from ..config import Config
from ..db import Database
from ..leads import looks_like_lead, parse_lead
from ..utils import deliver_lead, full_name

log = logging.getLogger(__name__)

router = Router(name="leads")


async def _register_lead(
    message: Message, bot: Bot, db: Database, cfg: Config, source: str, *, force: bool = False
) -> bool:
    user = message.from_user
    text = (message.text or message.caption or "").strip()
    if user is None or not text:
        return False

    parsed = parse_lead(text)
    if not force and not parsed.has_params and not looks_like_lead(text):
        return False

    # Антидубль: одна заявка от клиента в пределах окна
    if await db.recent_lead_exists(user.id, cfg.lead_cooldown_min):
        log.info("Заявка от %s пропущена — дубль в пределах %s мин", user.id, cfg.lead_cooldown_min)
        return False

    await db.upsert_user(user.id, user.username, user.first_name, user.last_name)
    lead = await db.create_lead(
        user_tg_id=user.id,
        username=user.username,
        full_name=full_name(user),
        chat_id=message.chat.id,
        thread_id=message.message_thread_id,
        message_id=message.message_id,
        source=source,
        raw_text=text[:4000],
        category=parsed.category,
        weight_kg=parsed.weight_kg,
        volume_m3=parsed.volume_m3,
    )

    delivered = await deliver_lead(bot, db, cfg, lead, user, source)
    log.info("Заявка #%s от %s (доставлена: %s)", lead["id"], user.id, delivered)
    return True


# --------------------------------------------------------------------- команда
@router.message(Command("zayavka", "заявка", "raschet", "расчет", "calc"))
async def cmd_lead(message: Message, bot: Bot, db: Database, cfg: Config) -> None:
    payload = (message.text or "").split(maxsplit=1)
    if len(payload) < 2:
        await message.reply(texts.LEAD_NEED_MORE)
        return

    # подменяем текст, чтобы в заявку не попала сама команда
    stripped = message.model_copy(update={"text": payload[1]})
    if await _register_lead(stripped, bot, db, cfg, "command", force=True):
        await message.reply(texts.LEAD_ACCEPTED)
    else:
        await message.reply(texts.LEAD_NEED_MORE)


# ----------------------------------------------------------------------- личка
@router.message(F.chat.type == "private", (F.text | F.caption), ~F.text.startswith("/"))
async def dm_lead(message: Message, bot: Bot, db: Database, cfg: Config) -> None:
    if await _register_lead(message, bot, db, cfg, "dm"):
        await message.answer(texts.LEAD_ACCEPTED)
    else:
        await message.answer(texts.LEAD_NEED_MORE)


# ----------------------------------------------------------------------- группа
@router.message(
    F.chat.type.in_({"group", "supergroup"}),
    (F.text | F.caption),
    ~F.text.startswith("/"),
)
async def group_lead(message: Message, bot: Bot, db: Database, cfg: Config) -> None:
    if cfg.group_chat_id and message.chat.id != cfg.group_chat_id:
        return

    user = message.from_user
    if user is None or user.is_bot:
        return

    text = (message.text or message.caption or "").strip()

    # Ответ на приветствие бота — это и есть заявка (как договорились с заказчиком)
    reply = message.reply_to_message
    is_reply_to_bot = reply is not None and reply.from_user is not None and reply.from_user.id == bot.id

    # Новичок в течение окна ожидания: первое содержательное сообщение = заявка
    is_awaiting = await db.joined_within_hours(user.id, cfg.lead_window_hours) and not (
        await db.has_lead_since_join(user.id)
    )

    if not (is_reply_to_bot or is_awaiting or looks_like_lead(text)):
        return

    parsed = parse_lead(text)
    if is_reply_to_bot and not parsed.has_params and not looks_like_lead(text):
        await message.reply(texts.LEAD_NEED_MORE)
        return

    force = bool(is_reply_to_bot and parsed.has_params)
    if await _register_lead(message, bot, db, cfg, "group", force=force):
        await message.reply(texts.LEAD_ACCEPTED)
