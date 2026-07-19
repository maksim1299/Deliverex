"""Middleware: учёт пользователей и антиспам-фильтр до обработчиков."""
from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware, Bot
from aiogram.exceptions import TelegramAPIError
from aiogram.types import ChatPermissions, Message, TelegramObject

from . import texts
from .antispam import AntiSpam
from .config import Config
from .db import Database
from .utils import is_privileged, mention, safe_delete, schedule_delete

log = logging.getLogger(__name__)

SEEN_TTL = 300  # как часто обновляем профиль пользователя в БД
NOTICE_LIFETIME = 30  # сек, через сколько убирать предупреждение из чата

MUTED_PERMISSIONS = ChatPermissions(
    can_send_messages=False,
    can_send_audios=False,
    can_send_documents=False,
    can_send_photos=False,
    can_send_videos=False,
    can_send_video_notes=False,
    can_send_voice_notes=False,
    can_send_polls=False,
    can_send_other_messages=False,
    can_add_web_page_previews=False,
)

UNMUTED_PERMISSIONS = ChatPermissions(
    can_send_messages=True,
    can_send_audios=True,
    can_send_documents=True,
    can_send_photos=True,
    can_send_videos=True,
    can_send_video_notes=True,
    can_send_voice_notes=True,
    can_send_polls=True,
    can_send_other_messages=True,
    can_add_web_page_previews=True,
)


class UserTrackingMiddleware(BaseMiddleware):
    """Держит таблицу users в актуальном состоянии, не нагружая БД на каждом сообщении."""

    def __init__(self, db: Database) -> None:
        self.db = db
        self._seen: dict[int, float] = {}

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        user = data.get("event_from_user")
        if user is not None and not user.is_bot:
            now = time.monotonic()
            last = self._seen.get(user.id, 0.0)
            if now - last > SEEN_TTL:
                self._seen[user.id] = now
                try:
                    await self.db.upsert_user(
                        user.id, user.username, user.first_name, user.last_name
                    )
                except Exception as exc:  # БД не должна ронять обработку сообщений
                    log.warning("Не удалось обновить пользователя %s: %s", user.id, exc)
        return await handler(event, data)


class AntiSpamMiddleware(BaseMiddleware):
    """Проверяет сообщения в группе. Спам удаляется и до обработчиков не доходит."""

    def __init__(self, db: Database, cfg: Config, antispam: AntiSpam) -> None:
        self.db = db
        self.cfg = cfg
        self.antispam = antispam

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        if not isinstance(event, Message) or not self.cfg.antispam_enabled:
            return await handler(event, data)

        # Личка и каналы не модерируются
        if event.chat.type not in {"group", "supergroup"}:
            return await handler(event, data)

        # Если задан GROUP_CHAT_ID — работаем только в этой группе
        if self.cfg.group_chat_id and event.chat.id != self.cfg.group_chat_id:
            return await handler(event, data)

        user = event.from_user
        if user is None or user.is_bot:
            return await handler(event, data)

        bot: Bot = data["bot"]
        if await is_privileged(bot, self.db, self.cfg, user.id, event.chat.id):
            return await handler(event, data)

        is_newbie = await self.db.joined_within_hours(user.id, self.cfg.newbie_hours)
        verdict = await self.antispam.check(event, is_newbie=bool(is_newbie))

        if not self.antispam.is_spam(verdict):
            return await handler(event, data)

        await self._punish(bot, event, verdict)
        return None  # обработчики пропускаем

    # ------------------------------------------------------------------ санкции
    async def _punish(self, bot: Bot, message: Message, verdict) -> None:
        user = message.from_user
        assert user is not None
        chat_id = message.chat.id
        text = message.text or message.caption or ""

        await safe_delete(message)
        warns = await self.db.add_warn(user.id)

        action = "delete"
        notice: str | None = None

        if warns >= self.cfg.warn_limit:
            mutes = await self.db.add_mute(user.id)
            action = "mute"
            muted = await self._mute(bot, chat_id, user.id, self.cfg.mute_minutes)
            if muted:
                notice = texts.SPAM_MUTED.format(
                    mention=mention(user), minutes=self.cfg.mute_minutes
                )
            if self.cfg.ban_after_mutes and mutes >= self.cfg.ban_after_mutes:
                if await self._ban(bot, chat_id, user.id):
                    action = "ban"
                    await self.db.set_banned(user.id, True)
                    notice = texts.SPAM_BANNED.format(mention=mention(user))
        else:
            action = "warn"
            notice = texts.SPAM_WARNING.format(
                mention=mention(user), warns=warns, limit=self.cfg.warn_limit
            )

        await self.db.log_warning(
            user_tg_id=user.id,
            chat_id=chat_id,
            reason=verdict.reason_text,
            score=verdict.score,
            message_text=text[:1000],
            action=action,
        )
        log.info(
            "Спам от %s (%s баллов: %s) → %s",
            user.id, verdict.score, verdict.reason_text, action,
        )

        if notice:
            sent = await self._notify(bot, message, notice)
            if sent:
                schedule_delete(bot, chat_id, sent.message_id, NOTICE_LIFETIME)

    async def _notify(self, bot: Bot, message: Message, text: str) -> Message | None:
        try:
            return await bot.send_message(
                message.chat.id,
                text,
                message_thread_id=message.message_thread_id,
                disable_web_page_preview=True,
            )
        except TelegramAPIError as exc:
            log.warning("Не удалось отправить уведомление о спаме: %s", exc)
            return None

    async def _mute(self, bot: Bot, chat_id: int, user_id: int, minutes: int) -> bool:
        until = datetime.now(timezone.utc) + timedelta(minutes=minutes)
        try:
            await bot.restrict_chat_member(
                chat_id, user_id, permissions=MUTED_PERMISSIONS, until_date=until
            )
            return True
        except TelegramAPIError as exc:
            log.warning("Не удалось замьютить %s: %s", user_id, exc)
            return False

    async def _ban(self, bot: Bot, chat_id: int, user_id: int) -> bool:
        try:
            await bot.ban_chat_member(chat_id, user_id)
            return True
        except TelegramAPIError as exc:
            log.warning("Не удалось забанить %s: %s", user_id, exc)
            return False
