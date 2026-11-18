"""Збирає всі обробники в один роутер і ловить помилки."""

from __future__ import annotations

import logging

from aiogram import Router
from aiogram.filters import ExceptionTypeFilter
from aiogram.types import ErrorEvent

from bot.handlers import admin, common, student
from bot.queue_logic import QueueError

log = logging.getLogger(__name__)


def build_router() -> Router:
    root = Router(name="root")
    root.include_routers(common.router, admin.router, student.router)

    @root.errors(ExceptionTypeFilter(QueueError))
    async def on_queue_error(event: ErrorEvent):
        """QueueError — очікувана помилка: показуємо її текст користувачу."""
        text = str(event.exception)
        update = event.update
        if update.callback_query is not None:
            # У спливаючому вікні HTML не працює — прибираємо теги.
            plain = text.replace("<b>", "").replace("</b>", "").replace("<code>", "").replace("</code>", "")
            await update.callback_query.answer(plain[:200], show_alert=True)
        elif update.message is not None:
            await update.message.reply(text)

    @root.errors()
    async def on_unexpected_error(event: ErrorEvent):
        log.exception("Неочікувана помилка", exc_info=event.exception)
        if event.update.callback_query is not None:
            await event.update.callback_query.answer("Щось пішло не так 😵", show_alert=True)
        elif event.update.message is not None:
            await event.update.message.reply("Щось пішло не так 😵 Спробуй ще раз.")

    return root
