"""Ручная модерация и управление антиспам-словарём."""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from aiogram import Bot, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command, CommandObject
from aiogram.types import Message, User

from .. import texts
from ..antispam import AntiSpam
from ..config import Config
from ..db import Database
from ..leads import format_volume, format_weight
from ..middlewares import MUTED_PERMISSIONS, UNMUTED_PERMISSIONS
from ..utils import MSK, esc, is_privileged, mention

log = logging.getLogger(__name__)

router = Router(name="moderation")


async def _guard(message: Message, bot: Bot, db: Database, cfg: Config) -> bool:
    user = message.from_user
    if user is None or not await is_privileged(bot, db, cfg, user.id, message.chat.id):
        await message.reply(texts.NOT_ALLOWED)
        return False
    return True


def _target(message: Message) -> User | None:
    reply = message.reply_to_message
    return reply.from_user if reply and reply.from_user else None


async def _require_target(message: Message) -> User | None:
    target = _target(message)
    if target is None:
        await message.reply("↩️ Используйте команду ответом на сообщение пользователя.")
    return target


# ------------------------------------------------------------------ модерация
@router.message(Command("warn"))
async def cmd_warn(message: Message, bot: Bot, db: Database, cfg: Config, command: CommandObject) -> None:
    if not await _guard(message, bot, db, cfg):
        return
    target = await _require_target(message)
    if target is None:
        return

    await db.upsert_user(target.id, target.username, target.first_name, target.last_name)
    warns = await db.add_warn(target.id)
    reason = command.args or "нарушение правил чата"
    await db.log_warning(
        user_tg_id=target.id,
        chat_id=message.chat.id,
        reason=reason,
        score=0,
        message_text=(message.reply_to_message.text or "")[:1000] if message.reply_to_message else None,
        action="warn",
        by_admin_id=message.from_user.id if message.from_user else None,
    )
    await message.reply(
        f"⚠️ {mention(target)} получил(а) предупреждение {warns}/{cfg.warn_limit}.\nПричина: {esc(reason)}"
    )


@router.message(Command("unwarn"))
async def cmd_unwarn(message: Message, bot: Bot, db: Database, cfg: Config) -> None:
    if not await _guard(message, bot, db, cfg):
        return
    target = await _require_target(message)
    if target is None:
        return
    await db.reset_warns(target.id)
    await message.reply(f"✅ Предупреждения {mention(target)} сняты.")


@router.message(Command("mute"))
async def cmd_mute(message: Message, bot: Bot, db: Database, cfg: Config, command: CommandObject) -> None:
    if not await _guard(message, bot, db, cfg):
        return
    target = await _require_target(message)
    if target is None:
        return

    try:
        minutes = int(command.args) if command.args else cfg.mute_minutes
    except ValueError:
        minutes = cfg.mute_minutes

    until = datetime.now(timezone.utc) + timedelta(minutes=minutes)
    try:
        await bot.restrict_chat_member(
            message.chat.id, target.id, permissions=MUTED_PERMISSIONS, until_date=until
        )
    except TelegramAPIError as exc:
        await message.reply(f"❌ Не удалось ограничить пользователя: {esc(str(exc))}")
        return

    await db.add_mute(target.id)
    await db.log_warning(
        user_tg_id=target.id, chat_id=message.chat.id, reason="ручной мут",
        score=0, message_text=None, action="mute",
        by_admin_id=message.from_user.id if message.from_user else None,
    )
    await message.reply(f"🔇 {mention(target)} ограничен(а) на {minutes} мин.")


@router.message(Command("unmute"))
async def cmd_unmute(message: Message, bot: Bot, db: Database, cfg: Config) -> None:
    if not await _guard(message, bot, db, cfg):
        return
    target = await _require_target(message)
    if target is None:
        return
    try:
        await bot.restrict_chat_member(
            message.chat.id, target.id, permissions=UNMUTED_PERMISSIONS
        )
    except TelegramAPIError as exc:
        await message.reply(f"❌ Не удалось снять ограничение: {esc(str(exc))}")
        return
    await db.reset_warns(target.id)
    await message.reply(f"🔊 Ограничения с {mention(target)} сняты.")


@router.message(Command("ban"))
async def cmd_ban(message: Message, bot: Bot, db: Database, cfg: Config) -> None:
    if not await _guard(message, bot, db, cfg):
        return
    target = await _require_target(message)
    if target is None:
        return
    try:
        await bot.ban_chat_member(message.chat.id, target.id)
    except TelegramAPIError as exc:
        await message.reply(f"❌ Не удалось заблокировать: {esc(str(exc))}")
        return
    await db.set_banned(target.id, True)
    await message.reply(f"⛔️ {mention(target)} заблокирован(а).")


@router.message(Command("unban"))
async def cmd_unban(message: Message, bot: Bot, db: Database, cfg: Config, command: CommandObject) -> None:
    if not await _guard(message, bot, db, cfg):
        return

    target = _target(message)
    user_id: int | None = target.id if target else None
    if user_id is None and command.args:
        arg = command.args.strip()
        if arg.lstrip("-").isdigit():
            user_id = int(arg)
        else:
            row = await db.find_user_by_username(arg)
            user_id = row["tg_id"] if row else None

    if user_id is None:
        await message.reply("↩️ Ответьте на сообщение или укажите ID/@username: /unban 123456789")
        return

    try:
        await bot.unban_chat_member(message.chat.id, user_id, only_if_banned=True)
    except TelegramAPIError as exc:
        await message.reply(f"❌ Не удалось разблокировать: {esc(str(exc))}")
        return
    await db.set_banned(user_id, False)
    await db.reset_warns(user_id)
    await message.reply(f"✅ Пользователь <code>{user_id}</code> разблокирован.")


@router.message(Command("trust"))
async def cmd_trust(message: Message, bot: Bot, db: Database, cfg: Config) -> None:
    if not await _guard(message, bot, db, cfg):
        return
    target = await _require_target(message)
    if target is None:
        return
    await db.upsert_user(target.id, target.username, target.first_name, target.last_name)
    await db.set_trusted(target.id, True)
    await message.reply(f"✅ {mention(target)} больше не проверяется антиспамом.")


@router.message(Command("untrust"))
async def cmd_untrust(message: Message, bot: Bot, db: Database, cfg: Config) -> None:
    if not await _guard(message, bot, db, cfg):
        return
    target = await _require_target(message)
    if target is None:
        return
    await db.set_trusted(target.id, False)
    await message.reply(f"🔁 {mention(target)} снова проверяется антиспамом.")


# ------------------------------------------------------------ антиспам-словарь
@router.message(Command("spam_list"))
async def cmd_spam_list(message: Message, bot: Bot, db: Database, cfg: Config) -> None:
    if not await _guard(message, bot, db, cfg):
        return
    rows = await db.get_spam_rules()
    if not rows:
        await message.reply("Словарь пуст.")
        return

    lines = ["<b>🛡 Правила антиспама</b>", ""]
    for row in rows:
        kind = "regex" if row["kind"] == "regex" else "слово"
        lines.append(f"<code>{row['id']}</code> [{kind}, {row['score']}] {esc(row['pattern'])}")

    chunk = ""
    for line in lines:
        if len(chunk) + len(line) > 3500:
            await message.answer(chunk)
            chunk = ""
        chunk += line + "\n"
    if chunk:
        await message.answer(chunk)


@router.message(Command("spam_add"))
async def cmd_spam_add(
    message: Message, bot: Bot, db: Database, cfg: Config, command: CommandObject, antispam: AntiSpam
) -> None:
    if not await _guard(message, bot, db, cfg):
        return
    if not command.args:
        await message.reply("Использование: <code>/spam_add слово или фраза</code>")
        return

    pattern = command.args.strip().lower()
    await db.add_spam_rule(pattern, "word", 3, message.from_user.id if message.from_user else None)
    antispam.invalidate_rules()
    await message.reply(f"✅ Добавлено стоп-слово: <code>{esc(pattern)}</code>")


@router.message(Command("spam_add_re"))
async def cmd_spam_add_re(
    message: Message, bot: Bot, db: Database, cfg: Config, command: CommandObject, antispam: AntiSpam
) -> None:
    if not await _guard(message, bot, db, cfg):
        return
    if not command.args:
        await message.reply("Использование: <code>/spam_add_re регулярное_выражение</code>")
        return

    import re

    pattern = command.args.strip()
    try:
        re.compile(pattern)
    except re.error as exc:
        await message.reply(f"❌ Некорректное регулярное выражение: {esc(str(exc))}")
        return

    await db.add_spam_rule(pattern, "regex", 3, message.from_user.id if message.from_user else None)
    antispam.invalidate_rules()
    await message.reply(f"✅ Добавлено regex-правило: <code>{esc(pattern)}</code>")


@router.message(Command("spam_del"))
async def cmd_spam_del(
    message: Message, bot: Bot, db: Database, cfg: Config, command: CommandObject, antispam: AntiSpam
) -> None:
    if not await _guard(message, bot, db, cfg):
        return
    if not command.args or not command.args.strip().isdigit():
        await message.reply("Использование: <code>/spam_del ID</code> (ID см. в /spam_list)")
        return

    ok = await db.disable_spam_rule(int(command.args.strip()))
    antispam.invalidate_rules()
    await message.reply("✅ Правило отключено." if ok else "❌ Правило с таким ID не найдено.")


# --------------------------------------------------------------- заявки/статы
@router.message(Command("leads"))
async def cmd_leads(message: Message, bot: Bot, db: Database, cfg: Config, command: CommandObject) -> None:
    if not await _guard(message, bot, db, cfg):
        return

    try:
        limit = max(1, min(int(command.args), 30)) if command.args else 10
    except ValueError:
        limit = 10

    rows = await db.last_leads(limit)
    if not rows:
        await message.reply("Заявок пока нет.")
        return

    lines = [f"<b>📋 Последние заявки ({len(rows)})</b>", ""]
    for row in rows:
        created = row["created_at"].astimezone(MSK).strftime("%d.%m %H:%M")
        who = f"@{esc(row['username'])}" if row["username"] else esc(row["full_name"] or "клиент")
        lines.append(
            f"<b>#{row['id']}</b> · {created} · {who}\n"
            f"   📂 {esc(row['category']) if row['category'] else '—'} · "
            f"⚖️ {format_weight(row['weight_kg'])} · 📐 {format_volume(row['volume_m3'])}"
        )
    await message.reply("\n".join(lines))


@router.message(Command("stats"))
async def cmd_stats(message: Message, bot: Bot, db: Database, cfg: Config) -> None:
    if not await _guard(message, bot, db, cfg):
        return
    s = await db.stats()
    await message.reply(
        "<b>📊 Статистика Deliverex Bot</b>\n"
        "\n"
        f"👥 Участников в базе: {s.get('members', 0)}\n"
        "\n"
        f"📝 Заявок всего: {s.get('leads_total', 0)}\n"
        f"📝 За сутки: {s.get('leads_today', 0)}\n"
        f"📝 За неделю: {s.get('leads_week', 0)}\n"
        "\n"
        f"🛡 Заблокировано спама всего: {s.get('spam_total', 0)}\n"
        f"🛡 За сутки: {s.get('spam_today', 0)}"
    )
