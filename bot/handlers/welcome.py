"""Приветствие новых участников и уборка служебных сообщений."""
from __future__ import annotations

import logging
import time

from aiogram import Bot, F, Router
from aiogram.filters import IS_MEMBER, IS_NOT_MEMBER, ChatMemberUpdatedFilter
from aiogram.types import ChatMemberUpdated, Message, User

from .. import texts
from ..config import Config
from ..db import Database
from ..utils import invalidate_admin_cache, mention, safe_delete, safe_send, schedule_delete

log = logging.getLogger(__name__)

router = Router(name="welcome")

# Защита от двойного приветствия (chat_member + служебное сообщение)
_greeted: dict[tuple[int, int], float] = {}
GREET_DEDUP_SECONDS = 120


def _already_greeted(chat_id: int, user_id: int) -> bool:
    key = (chat_id, user_id)
    now = time.monotonic()
    last = _greeted.get(key)
    if last is not None and now - last < GREET_DEDUP_SECONDS:
        return True
    _greeted[key] = now
    if len(_greeted) > 5000:  # не даём словарю расти бесконечно
        cutoff = now - GREET_DEDUP_SECONDS
        for k, ts in list(_greeted.items()):
            if ts < cutoff:
                _greeted.pop(k, None)
    return False


def _in_target_chat(cfg: Config, chat_id: int) -> bool:
    return not cfg.group_chat_id or chat_id == cfg.group_chat_id


async def greet(bot: Bot, db: Database, cfg: Config, chat_id: int, user: User) -> None:
    if user.is_bot or _already_greeted(chat_id, user.id):
        return

    await db.upsert_user(user.id, user.username, user.first_name, user.last_name, mark_joined=True)

    text = texts.WELCOME
    if cfg.welcome_mention:
        text = f"{mention(user)}\n\n{text}"

    sent = await safe_send(
        bot,
        chat_id,
        text,
        message_thread_id=cfg.welcome_topic_id,
        disable_web_page_preview=True,
    )
    if sent and cfg.welcome_delete_after:
        schedule_delete(bot, chat_id, sent.message_id, cfg.welcome_delete_after)

    # Дублируем в личку — сработает, только если человек уже писал боту
    if cfg.welcome_dm:
        await safe_send(bot, user.id, texts.WELCOME, disable_web_page_preview=True)

    log.info("Приветствие отправлено пользователю %s в чат %s", user.id, chat_id)


@router.chat_member(ChatMemberUpdatedFilter(member_status_changed=IS_NOT_MEMBER >> IS_MEMBER))
async def on_user_joined(event: ChatMemberUpdated, bot: Bot, db: Database, cfg: Config) -> None:
    if not _in_target_chat(cfg, event.chat.id):
        return
    await greet(bot, db, cfg, event.chat.id, event.new_chat_member.user)


@router.chat_member(ChatMemberUpdatedFilter(member_status_changed=IS_MEMBER >> IS_NOT_MEMBER))
async def on_user_left(event: ChatMemberUpdated, db: Database, cfg: Config) -> None:
    if not _in_target_chat(cfg, event.chat.id):
        return
    await db.mark_left(event.new_chat_member.user.id)
    _greeted.pop((event.chat.id, event.new_chat_member.user.id), None)


@router.my_chat_member()
async def on_bot_status_changed(event: ChatMemberUpdated) -> None:
    """Права бота изменились — сбрасываем кэш списка администраторов."""
    invalidate_admin_cache(event.chat.id)
    log.info(
        "Статус бота в чате %s (%s): %s",
        event.chat.id, event.chat.title, event.new_chat_member.status,
    )


@router.message(F.new_chat_members)
async def on_join_service_message(message: Message, bot: Bot, db: Database, cfg: Config) -> None:
    """Служебное «X вошёл в чат»: чистим и приветствуем, если chat_member не пришёл."""
    if not _in_target_chat(cfg, message.chat.id):
        return
    for user in message.new_chat_members or []:
        if user.id != bot.id:
            await greet(bot, db, cfg, message.chat.id, user)
    if cfg.delete_service_messages:
        await safe_delete(message)


@router.message(F.left_chat_member)
async def on_leave_service_message(message: Message, db: Database, cfg: Config) -> None:
    if not _in_target_chat(cfg, message.chat.id):
        return
    if message.left_chat_member and not message.left_chat_member.is_bot:
        await db.mark_left(message.left_chat_member.id)
    if cfg.delete_service_messages:
        await safe_delete(message)
