"""Перевірка, чи людина — адмін групи.

Запит до Telegram (getChatAdministrators) повільний порівняно з усім іншим,
тому список адмінів кешується на кілька хвилин.
"""

from __future__ import annotations

import logging
import time

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError
from aiogram.types import CallbackQuery, Message

log = logging.getLogger(__name__)

CACHE_TTL = 300  # секунд


class AdminCache:
    def __init__(self, bot: Bot) -> None:
        self._bot = bot
        self._cache: dict[int, tuple[float, frozenset[int]]] = {}

    async def _admins(self, chat_id: int) -> frozenset[int]:
        cached = self._cache.get(chat_id)
        if cached and time.monotonic() - cached[0] < CACHE_TTL:
            return cached[1]
        try:
            members = await self._bot.get_chat_administrators(chat_id)
        except TelegramAPIError as e:
            log.warning("Не вдалося отримати адмінів чату %s: %s", chat_id, e)
            return cached[1] if cached else frozenset()
        ids = frozenset(m.user.id for m in members)
        self._cache[chat_id] = (time.monotonic(), ids)
        return ids

    async def is_admin(self, event: Message | CallbackQuery) -> bool:
        if isinstance(event, Message):
            chat_id = event.chat.id
            # Анонімний адмін пише від імені самої групи.
            if event.sender_chat is not None and event.sender_chat.id == chat_id:
                return True
        else:
            if event.message is None:
                return False
            chat_id = event.message.chat.id
        if event.from_user is None:
            return False
        return event.from_user.id in await self._admins(chat_id)
