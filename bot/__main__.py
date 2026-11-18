"""Точка входу: `uv run python -m bot`."""

from __future__ import annotations

import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.types import (
    BotCommand,
    BotCommandScopeAllGroupChats,
    BotCommandScopeAllPrivateChats,
    LinkPreviewOptions,
)

from bot.config import load_config
from bot.handlers import build_router
from bot.permissions import AdminCache
from bot.service import QueueService
from bot.storage import Storage

GROUP_COMMANDS = [
    BotCommand(command="join", description="Записатися (можна з часом: /join 10:45)"),
    BotCommand(command="done", description="Я здав — кличте наступного"),
    BotCommand(command="leave", description="Виписатися"),
    BotCommand(command="later", description="Пропустити вперед наступного"),
    BotCommand(command="here", description="Мене пропустили, але я прийшов"),
    BotCommand(command="name", description="Як мене показувати в черзі"),
    BotCommand(command="help", description="Усі команди"),
    BotCommand(command="new", description="[адмін] Нова черга"),
    BotCommand(command="go", description="[адмін] Почати приймати"),
    BotCommand(command="skip", description="[адмін] Поточний не прийшов"),
    BotCommand(command="repost", description="[адмін] Переслати чергу вниз"),
    BotCommand(command="close", description="[адмін] Закрити чергу"),
]


async def main() -> None:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    config = load_config()

    bot = Bot(
        config.bot_token,
        default=DefaultBotProperties(
            parse_mode="HTML", link_preview=LinkPreviewOptions(is_disabled=True)
        ),
    )
    storage = Storage(config.db_path)
    await storage.open()
    service = QueueService(bot, storage)

    dp = Dispatcher()
    dp.include_router(build_router())
    # Ці об'єкти aiogram передасть в обробники за назвою параметра.
    dp["service"] = service
    dp["admins"] = AdminCache(bot)

    await bot.set_my_commands(GROUP_COMMANDS, scope=BotCommandScopeAllGroupChats())
    await bot.set_my_commands(
        [BotCommand(command="help", description="Як користуватись")],
        scope=BotCommandScopeAllPrivateChats(),
    )

    try:
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        await service.wait_background()
        await storage.close()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
