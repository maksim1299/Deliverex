"""Роутеры бота. Порядок подключения важен: команды → приветствие → заявки."""
from __future__ import annotations

from aiogram import Router

from . import common, leads, moderation, welcome


def build_router() -> Router:
    root = Router(name="root")
    root.include_router(common.router)      # /start, /help, /id, /setmanager
    root.include_router(moderation.router)  # админские команды
    root.include_router(welcome.router)     # вход/выход участников
    root.include_router(leads.router)       # заявки — последними, ловят обычный текст
    return root


__all__ = ["build_router"]
