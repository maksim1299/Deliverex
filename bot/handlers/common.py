"""Базовые команды: /start, /help, /id, /setmanager, /welcome."""
from __future__ import annotations

import logging

from aiogram import Bot, F, Router
from aiogram.filters import Command, CommandStart
from aiogram.types import Message

from .. import texts
from ..config import Config
from ..db import Database
from ..utils import esc, full_name, is_privileged

log = logging.getLogger(__name__)

router = Router(name="common")


def _is_manager_candidate(cfg: Config, message: Message) -> bool:
    user = message.from_user
    if user is None:
        return False
    if user.id in cfg.admin_ids:
        return True
    if cfg.manager_chat_id and message.chat.id == cfg.manager_chat_id:
        return True
    return bool(user.username and user.username.lower() == cfg.manager_username.lower())


@router.message(CommandStart(), F.chat.type == "private")
async def cmd_start(message: Message, db: Database, cfg: Config) -> None:
    user = message.from_user
    if user is None:
        return

    await db.upsert_user(user.id, user.username, user.first_name, user.last_name)

    if _is_manager_candidate(cfg, message):
        await db.upsert_manager(user.id, user.username, full_name(user), message.chat.id)
        await db.set_admin(user.id, True)
        await message.answer(texts.DM_START_MANAGER)
        log.info("Менеджер зарегистрирован: %s (@%s), chat_id=%s", user.id, user.username, message.chat.id)
        return

    await message.answer(texts.DM_START_CLIENT)


@router.message(Command("help"))
async def cmd_help(message: Message, bot: Bot, db: Database, cfg: Config) -> None:
    user = message.from_user
    if user and await is_privileged(bot, db, cfg, user.id, message.chat.id):
        await message.reply(texts.HELP_ADMIN)
    else:
        await message.reply(texts.HELP_USER)


@router.message(Command("id"))
async def cmd_id(message: Message) -> None:
    user = message.from_user
    lines = [
        f"💬 Chat ID: <code>{message.chat.id}</code>",
        f"📁 Тип чата: {message.chat.type}",
    ]
    if message.message_thread_id:
        lines.append(f"🧵 Topic ID: <code>{message.message_thread_id}</code>")
    if user:
        lines.append(f"👤 Ваш ID: <code>{user.id}</code>")
        if user.username:
            lines.append(f"🔗 Username: @{esc(user.username)}")
    await message.reply("\n".join(lines))


@router.message(Command("setmanager"))
async def cmd_setmanager(message: Message, bot: Bot, db: Database, cfg: Config) -> None:
    """Делает текущий чат получателем заявок (для менеджера/админа)."""
    user = message.from_user
    if user is None or not await is_privileged(bot, db, cfg, user.id, message.chat.id):
        await message.reply(texts.NOT_ALLOWED)
        return

    await db.upsert_manager(user.id, user.username, full_name(user), message.chat.id)
    await message.reply(
        f"✅ Заявки будут приходить в этот чат (ID <code>{message.chat.id}</code>)."
    )


@router.message(Command("welcome"))
async def cmd_welcome(message: Message, bot: Bot, db: Database, cfg: Config) -> None:
    user = message.from_user
    if user is None or not await is_privileged(bot, db, cfg, user.id, message.chat.id):
        await message.reply(texts.NOT_ALLOWED)
        return
    await message.reply("👇 Текущее приветствие для новых участников:")
    await message.answer(texts.WELCOME, disable_web_page_preview=True)
