"""Рассылка от имени бота: админ присылает пост боту в личку — бот публикует его в группу."""
from __future__ import annotations

import logging

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command, CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import Message

from .. import texts
from ..config import Config
from ..db import Database
from ..utils import esc, is_privileged

log = logging.getLogger(__name__)

router = Router(name="broadcast")

# Команда пишется кириллицей (/соо), латинские варианты — на случай другой раскладки.
POST_COMMANDS = ("соо", "coo", "post", "send")
CANCEL_COMMANDS = ("отмена", "cancel", "стоп")


class Broadcast(StatesGroup):
    waiting_post = State()


async def _publish(bot: Bot, cfg: Config, source: Message, text: str | None) -> bool:
    """Публикует пост в рабочую группу от имени бота (без пометки «переслано»)."""
    try:
        if text is not None:
            await bot.send_message(cfg.group_chat_id, text, disable_web_page_preview=False)
        else:
            # copy_message переносит текст/фото/видео/документ, но не показывает автора.
            await bot.copy_message(
                chat_id=cfg.group_chat_id,
                from_chat_id=source.chat.id,
                message_id=source.message_id,
            )
        return True
    except TelegramAPIError as exc:
        log.warning("Не удалось опубликовать пост в группу %s: %s", cfg.group_chat_id, exc)
        return False


@router.message(Command(*POST_COMMANDS, ignore_case=True))
async def cmd_post(
    message: Message,
    bot: Bot,
    db: Database,
    cfg: Config,
    command: CommandObject,
    state: FSMContext,
) -> None:
    user = message.from_user
    if user is None or not await is_privileged(bot, db, cfg, user.id, message.chat.id):
        await message.reply(texts.NOT_ALLOWED)
        return

    if not cfg.group_chat_id:
        await message.reply(texts.POST_NO_GROUP)
        return

    text = command.args.strip() if command.args else ""
    if text:
        # Быстрый режим: /соо сразу с текстом поста.
        ok = await _publish(bot, cfg, message, text)
        await message.reply(texts.POST_DONE if ok else texts.POST_FAILED)
        return

    if message.chat.type != "private":
        # В группе нет смысла копить состояние — просим прислать пост боту в личку.
        await message.reply(texts.POST_USE_DM)
        return

    # Пошаговый режим: ждём следующий пост (можно с фото/видео).
    await state.set_state(Broadcast.waiting_post)
    await message.reply(texts.POST_PROMPT)


@router.message(Broadcast.waiting_post, Command(*CANCEL_COMMANDS, ignore_case=True))
async def cmd_post_cancel(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.reply(texts.POST_CANCELLED)


@router.message(Broadcast.waiting_post, F.chat.type == "private")
async def receive_post(message: Message, bot: Bot, cfg: Config, state: FSMContext) -> None:
    # Пришла другая команда, а не пост — выходим из режима, ничего не публикуем.
    if message.text and message.text.startswith("/"):
        await state.clear()
        await message.reply(texts.POST_NOT_A_POST)
        return

    await state.clear()

    if not cfg.group_chat_id:
        await message.reply(texts.POST_NO_GROUP)
        return

    # Текст без медиа отправляем как обычное сообщение (сохраняем форматирование),
    # всё остальное (фото/видео/документ) — копированием.
    if message.text:
        ok = await _publish(bot, cfg, message, message.html_text)
    else:
        ok = await _publish(bot, cfg, message, None)

    await message.reply(texts.POST_DONE if ok else texts.POST_FAILED)
