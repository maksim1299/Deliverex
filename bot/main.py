"""Точка входа: инициализация, регистрация роутеров, запуск (polling или webhook)."""
from __future__ import annotations

import asyncio
import logging
import sys

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramAPIError
from aiogram.types import (
    BotCommand,
    BotCommandScopeAllChatAdministrators,
    BotCommandScopeAllPrivateChats,
)

from .antispam import AntiSpam
from .config import Config, load_config
from .db import Database
from .handlers import build_router
from .middlewares import AntiSpamMiddleware, PrivateChatGuardMiddleware, UserTrackingMiddleware

log = logging.getLogger(__name__)

USER_COMMANDS = [
    BotCommand(command="start", description="Начать и получить расчёт доставки"),
    BotCommand(command="zayavka", description="Оставить заявку на просчёт"),
    BotCommand(command="help", description="Как это работает"),
]

ADMIN_COMMANDS = USER_COMMANDS + [
    BotCommand(command="leads", description="Последние заявки"),
    BotCommand(command="stats", description="Статистика"),
    BotCommand(command="warn", description="Предупреждение (ответом)"),
    BotCommand(command="mute", description="Ограничить (ответом)"),
    BotCommand(command="unmute", description="Снять ограничение (ответом)"),
    BotCommand(command="ban", description="Заблокировать (ответом)"),
    BotCommand(command="trust", description="Снять антиспам с пользователя"),
    BotCommand(command="spam_list", description="Правила антиспама"),
    BotCommand(command="spam_add", description="Добавить стоп-слово"),
    BotCommand(command="spam_del", description="Отключить правило"),
    BotCommand(command="setmanager", description="Слать заявки в этот чат"),
    BotCommand(command="id", description="ID чата и пользователя"),
]


def setup_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level, logging.INFO),
        format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
        stream=sys.stdout,
    )
    logging.getLogger("aiogram.event").setLevel(logging.WARNING)


async def setup_commands(bot: Bot) -> None:
    try:
        await bot.set_my_commands(USER_COMMANDS, scope=BotCommandScopeAllPrivateChats())
        await bot.set_my_commands(ADMIN_COMMANDS, scope=BotCommandScopeAllChatAdministrators())
    except TelegramAPIError as exc:
        log.warning("Не удалось установить меню команд: %s", exc)


def build_dispatcher(db: Database, cfg: Config, antispam: AntiSpam) -> Dispatcher:
    dp = Dispatcher()

    # доступно во всех обработчиках как аргументы db / cfg / antispam
    dp["db"] = db
    dp["cfg"] = cfg
    dp["antispam"] = antispam

    # один экземпляр на оба типа апдейтов — счётчики флуда должны быть общими
    spam_guard = AntiSpamMiddleware(db, cfg, antispam)
    dp.message.outer_middleware(UserTrackingMiddleware(db))
    dp.message.outer_middleware(PrivateChatGuardMiddleware(db, cfg))
    dp.message.outer_middleware(spam_guard)
    dp.edited_message.outer_middleware(spam_guard)

    dp.include_router(build_router())

    # Пустой обработчик правок нужен, чтобы Telegram присылал edited_message
    # и антиспам ловил «чистое сообщение → отредактировали, вставили рекламу».
    dp.edited_message.register(_ignore_edited)

    return dp


async def _ignore_edited(*_args, **_kwargs) -> None:
    """Вся работа по правкам выполняется в AntiSpamMiddleware."""
    return None


async def on_startup(bot: Bot, cfg: Config) -> None:
    me = await bot.get_me()
    log.info("Бот запущен: @%s (id=%s)", me.username, me.id)
    await setup_commands(bot)
    if cfg.group_chat_id:
        log.info("Рабочая группа: %s", cfg.group_chat_id)
    else:
        log.warning("GROUP_CHAT_ID не задан — бот работает во всех чатах, куда его добавили")
    if not cfg.manager_chat_id:
        log.warning(
            "MANAGER_CHAT_ID не задан. Менеджер @%s должен написать боту /start в личку, "
            "иначе заявки будет некуда отправлять.",
            cfg.manager_username,
        )


async def run_polling(bot: Bot, dp: Dispatcher, cfg: Config) -> None:
    await bot.delete_webhook(drop_pending_updates=True)
    await on_startup(bot, cfg)
    await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())


async def run_webhook(bot: Bot, dp: Dispatcher, cfg: Config) -> None:
    from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application
    from aiohttp import web

    await bot.set_webhook(
        url=cfg.webhook_url.rstrip("/") + cfg.webhook_path,
        secret_token=cfg.webhook_secret or None,
        drop_pending_updates=True,
        allowed_updates=dp.resolve_used_update_types(),
    )
    await on_startup(bot, cfg)

    async def healthcheck(_request: web.Request) -> web.Response:
        return web.json_response({"status": "ok"})

    app = web.Application()
    app.router.add_get("/", healthcheck)
    SimpleRequestHandler(
        dispatcher=dp, bot=bot, secret_token=cfg.webhook_secret or None
    ).register(app, path=cfg.webhook_path)
    setup_application(app, dp, bot=bot)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host="0.0.0.0", port=cfg.port)
    await site.start()
    log.info("Webhook принимает обновления на порту %s%s", cfg.port, cfg.webhook_path)

    try:
        await asyncio.Event().wait()
    finally:
        await runner.cleanup()


async def main() -> None:
    cfg = load_config()
    setup_logging(cfg.log_level)

    db = Database(cfg.database_url)
    await db.connect()
    await db.init_schema()

    antispam = AntiSpam(db, cfg)
    bot = Bot(
        token=cfg.bot_token,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML, link_preview_is_disabled=True),
    )
    dp = build_dispatcher(db, cfg, antispam)

    try:
        if cfg.use_webhook:
            await run_webhook(bot, dp, cfg)
        else:
            await run_polling(bot, dp, cfg)
    finally:
        await bot.session.close()
        await db.close()
        log.info("Бот остановлен")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        log.info("Выход по сигналу пользователя")
