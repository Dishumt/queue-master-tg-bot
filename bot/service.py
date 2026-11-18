"""Сервіс черги: склеює логіку (queue_logic), сховище (storage) і Telegram.

Обробники команд викликають `service.apply(...)`, а сервіс:
  1. бере чергу з кешу і блокує її;
  2. виконує дію (чиста функція з queue_logic);
  3. зберігає в SQLite;
  4. у фоні оновлює закріплене повідомлення і тегає наступного.

Пункт 4 — фонові задачі, тому обробник одразу відповідає користувачу,
не чекаючи, поки Telegram відредагує повідомлення. Це головне для швидкості.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Coroutine
from typing import Any, TypeVar

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest, TelegramRetryAfter
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from bot.queue_logic import Queue, QueueError, QueueStatus, TurnChange
from bot.render import CB_PREFIX, mention, queue_keyboard, render_queue
from bot.storage import QueueKey, Storage

log = logging.getLogger(__name__)

T = TypeVar("T")

# Не частіше одного редагування закріпленого повідомлення на цей проміжок.
# Перше редагування йде одразу, наступні зливаються в одне.
REFRESH_INTERVAL = 1.0  # секунд


class QueueService:
    def __init__(self, bot: Bot, storage: Storage) -> None:
        self.bot = bot
        self.storage = storage
        self._dirty: set[QueueKey] = set()
        self._refreshers: dict[QueueKey, asyncio.Task[None]] = {}
        self._announce_locks: dict[QueueKey, asyncio.Lock] = {}
        self._background: set[asyncio.Task[Any]] = set()
        self.refresh_interval = REFRESH_INTERVAL

    # ------------------------------------------------------------------
    # Головний вхід для обробників
    # ------------------------------------------------------------------

    def get(self, key: QueueKey, queue_id: int | None = None) -> Queue:
        queue = self.storage.get(key)
        if queue is None or (queue_id is not None and queue.id != queue_id):
            raise QueueError("Ця черга вже неактивна." if queue_id else "Тут немає активної черги.")
        return queue

    async def apply(
        self, key: QueueKey, action: Callable[[Queue], T], queue_id: int | None = None
    ) -> T:
        """Виконати дію над чергою, зберегти і запланувати оновлення в чаті."""
        async with self.storage.lock(key):
            queue = self.get(key, queue_id)
            result = action(queue)  # QueueError вилетить до збереження — нічого не зміниться
            await self.storage.save(queue)
            change = queue.pop_turn_change()
        self.refresh(queue)
        if change is not None:
            self._announce(queue, change)
        return result

    # ------------------------------------------------------------------
    # Нова черга / перепублікація
    # ------------------------------------------------------------------

    async def create(self, queue: Queue, created_by: int) -> None:
        key = (queue.chat_id, queue.thread_id)
        async with self.storage.lock(key):
            if self.storage.get(key) is not None:
                raise QueueError("Тут уже є активна черга. Спершу закрий її: /close")
            await self.storage.create(queue, created_by)
            await self._post(queue)
            await self.storage.save(queue)

    async def repost(self, key: QueueKey) -> None:
        """Надіслати повідомлення черги заново (вниз чату) і закріпити."""
        async with self.storage.lock(key):
            queue = self.get(key)
            old_id = queue.message_id
            await self._post(queue)
            await self.storage.save(queue)
        if old_id:
            self._spawn(self._safe(self.bot.delete_message(queue.chat_id, old_id)))

    async def _post(self, queue: Queue) -> None:
        msg = await self.bot.send_message(
            queue.chat_id,
            render_queue(queue),
            message_thread_id=queue.thread_id or None,
            reply_markup=queue_keyboard(queue),
        )
        queue.message_id = msg.message_id
        try:
            await self.bot.pin_chat_message(queue.chat_id, msg.message_id, disable_notification=True)
        except TelegramAPIError:
            await self.bot.send_message(
                queue.chat_id,
                "ℹ️ Дайте мені право закріплювати повідомлення — так чергу буде легше знайти.",
                message_thread_id=queue.thread_id or None,
            )

    async def unpin(self, queue: Queue) -> None:
        if queue.message_id:
            await self._safe(self.bot.unpin_chat_message(queue.chat_id, message_id=queue.message_id))

    # ------------------------------------------------------------------
    # Оновлення закріпленого повідомлення (з обмеженням частоти)
    # ------------------------------------------------------------------

    def refresh(self, queue: Queue) -> None:
        key = (queue.chat_id, queue.thread_id)
        self._dirty.add(key)
        if key not in self._refreshers:
            self._refreshers[key] = self._spawn(self._refresh_loop(key, queue))

    async def _refresh_loop(self, key: QueueKey, queue: Queue) -> None:
        try:
            while key in self._dirty:
                self._dirty.discard(key)
                await self._edit(queue)
                await asyncio.sleep(self.refresh_interval)
        finally:
            self._refreshers.pop(key, None)

    async def _edit(self, queue: Queue) -> None:
        if not queue.message_id:
            return
        for _ in range(2):  # друга спроба — тільки після RetryAfter
            try:
                await self.bot.edit_message_text(
                    text=render_queue(queue),
                    chat_id=queue.chat_id,
                    message_id=queue.message_id,
                    reply_markup=queue_keyboard(queue),
                )
                return
            except TelegramRetryAfter as e:
                await asyncio.sleep(e.retry_after)
            except TelegramBadRequest as e:
                if "not modified" in e.message:
                    return
                if "not found" in e.message and queue.status is not QueueStatus.CLOSED:
                    # Повідомлення черги видалили — публікуємо заново.
                    key = (queue.chat_id, queue.thread_id)
                    async with self.storage.lock(key):
                        await self._post(queue)
                        await self.storage.save(queue)
                    return
                log.warning("Не вдалося оновити чергу %s: %s", queue.id, e)
                return
            except TelegramAPIError as e:
                log.warning("Не вдалося оновити чергу %s: %s", queue.id, e)
                return

    # ------------------------------------------------------------------
    # «Твоя черга!»
    # ------------------------------------------------------------------

    def _announce(self, queue: Queue, change: TurnChange) -> None:
        # Текст складаємо одразу (синхронно), поки стан черги саме такий.
        if change.current is None:
            if queue.status is not QueueStatus.RUNNING:
                return
            text = "🎉 Усі записані здали! Можна дописатися — бот покличе."
            keyboard = None
        else:
            text = f"▶️ {mention(change.current)}, твоя черга! ({change.current.time})"
            if change.upcoming is not None:
                text += f"\n⏳ {mention(change.upcoming)}, готуйся — ти наступний."
            keyboard = InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(
                            text="✅ Я закінчив", callback_data=f"{CB_PREFIX}:done:{queue.id}"
                        ),
                        InlineKeyboardButton(
                            text="⏬ Пізніше", callback_data=f"{CB_PREFIX}:later:{queue.id}"
                        ),
                    ]
                ]
            )
        key = (queue.chat_id, queue.thread_id)
        lock = self._announce_locks.setdefault(key, asyncio.Lock())

        async def send() -> None:
            async with lock:  # щоб оголошення не переплутали порядок
                await self._safe(
                    self.bot.send_message(
                        queue.chat_id,
                        text,
                        message_thread_id=queue.thread_id or None,
                        reply_markup=keyboard,
                    )
                )

        self._spawn(send())

    # ------------------------------------------------------------------
    # Допоміжне
    # ------------------------------------------------------------------

    def _spawn(self, coro: Coroutine[Any, Any, T]) -> asyncio.Task[T]:
        # Тримаємо посилання на задачу, інакше збирач сміття може її прибрати.
        task = asyncio.create_task(coro)
        self._background.add(task)
        task.add_done_callback(self._background.discard)
        return task

    @staticmethod
    async def _safe(coro: Coroutine[Any, Any, Any]) -> None:
        try:
            await coro
        except TelegramAPIError as e:
            log.warning("Помилка Telegram: %s", e)

    async def wait_background(self) -> None:
        """Дочекатися фонових задач (для тестів і акуратної зупинки)."""
        while self._background:
            await asyncio.gather(*list(self._background), return_exceptions=True)
