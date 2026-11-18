"""Збирає всі обробники в один роутер і ловить помилки."""

from __future__ import annotations

import logging
import re

from aiogram import Router
from aiogram.filters import ExceptionTypeFilter
from aiogram.types import ErrorEvent

from bot.handlers import admin, common, student
from bot.queue_logic import QueueError
from bot.service import QueueService

log = logging.getLogger(__name__)


def build_router() -> Router:
    root = Router(name="root")
    root.include_routers(common.router, admin.router, student.router)

    @root.errors(ExceptionTypeFilter(QueueError))
    async def on_queue_error(event: ErrorEvent, service: QueueService):
        """QueueError — очікувана помилка: показуємо її текст користувачу.

        На кнопку — спливаюче вікно (бачить лише той, хто натиснув).
        На команду — відповідь у чаті, яка сама зникає, щоб не смітити.
        """
        text = str(event.exception)
        update = event.update
        if update.callback_query is not None:
            # У спливаючому вікні HTML не працює — прибираємо теги.
            plain = re.sub(r"<[^>]+>", "", text)
            await update.callback_query.answer(plain[:200], show_alert=True)
        elif update.message is not None:
            reply = await update.message.reply(text)
            # Довгі підказки (як формат /new) лишаємо довше — їх треба встигнути прочитати.
            service.delete_later(reply.chat.id, reply.message_id, 15 if len(text) < 150 else 90)

    @root.errors()
    async def on_unexpected_error(event: ErrorEvent):
        log.exception("Неочікувана помилка", exc_info=event.exception)
        if event.update.callback_query is not None:
            await event.update.callback_query.answer("Щось пішло не так 😵", show_alert=True)
        elif event.update.message is not None:
            await event.update.message.reply("Щось пішло не так 😵 Спробуй ще раз.")

    return root
