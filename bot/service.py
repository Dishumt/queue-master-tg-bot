"""Сервіс черги: склеює логіку (queue_logic), сховище (storage) і Telegram.

Обробники команд викликають `service.apply(...)`, а сервіс:
  1. бере чергу з кешу і блокує її;
  2. виконує дію (чиста функція з queue_logic);
  3. зберігає в SQLite;
  4. у фоні оновлює закріплене повідомлення і пише «твоя черга»;
  5. переставляє таймер (автопропуск / нагадування «ти ще в кабінеті?»).

Пункт 4 — фонові задачі, тому обробник одразу відповідає користувачу,
не чекаючи, поки Telegram відредагує повідомлення. Це головне для швидкості.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable, Coroutine
from typing import Any, TypeVar

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest, TelegramRetryAfter

from bot import queue_logic
from bot.queue_logic import Queue, QueueError, QueueStatus, TurnChange
from bot.render import queue_keyboard, render_queue, render_turn, turn_keyboard
from bot.storage import QueueKey, Storage

log = logging.getLogger(__name__)

T = TypeVar("T")

# Не частіше одного редагування закріпленого повідомлення на цей проміжок.
# Перше редагування йде одразу, наступні зливаються в одне.
REFRESH_INTERVAL = 1.0  # секунд
# Через скільки зникає відповідь бота з помилкою на команду (щоб не смітити в чаті).
ERROR_TTL = 15.0  # секунд
# Скільки чекати на друге натискання «Виписатися».
LEAVE_CONFIRM_WINDOW = 5.0  # секунд


class QueueService:
    def __init__(self, bot: Bot, storage: Storage) -> None:
        self.bot = bot
        self.storage = storage
        self._dirty: set[QueueKey] = set()
        self._refreshers: dict[QueueKey, asyncio.Task[None]] = {}
        self._announce_locks: dict[QueueKey, asyncio.Lock] = {}
        self._background: set[asyncio.Task[Any]] = set()
        # Довгі задачі (хвилини): таймери черг і відкладене видалення повідомлень.
        # Окремо від _background, бо на них не треба чекати при зупинці.
        self._timers: dict[QueueKey, asyncio.Task[None]] = {}
        self._delayed: set[asyncio.Task[Any]] = set()
        # Останнє «твоя черга» кожної черги — його видаляємо, коли пишемо нове.
        self._turn_messages: dict[int, int] = {}
        # Перше натискання «Виписатися»: (id черги, user_id) -> коли натиснули.
        self._leave_requests: dict[tuple[int, int], float] = {}
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
        self._schedule_timer(queue)
        return result

    def confirm_leave(self, queue_id: int, user_id: int) -> bool:
        """«Виписатися» спрацьовує з другого натискання протягом 5 с.

        Повертає True, якщо це друге натискання (можна виписувати).
        """
        key = (queue_id, user_id)
        first = self._leave_requests.pop(key, None)
        moment = time.monotonic()
        if first is not None and moment - first <= LEAVE_CONFIRM_WINDOW:
            return True
        self._leave_requests[key] = moment
        return False

    # ------------------------------------------------------------------
    # Таймери: автопропуск і нагадування
    # ------------------------------------------------------------------

    def _schedule_timer(self, queue: Queue, min_delay: float = 0.0) -> None:
        """Перезапустити таймер черги під її поточний стан.

        Викликається після кожної дії: якщо поточний змінився або підтвердив —
        старий таймер скасовується, а новий ставиться тільки коли треба.
        """
        key = (queue.chat_id, queue.thread_id)
        old = self._timers.pop(key, None)
        if old is not None:
            old.cancel()
        moment = queue.next_timer_at
        if moment is not None:
            self._timers[key] = asyncio.create_task(
                self._timer(key, queue.id, moment, min_delay)
            )

    async def _timer(
        self, key: QueueKey, queue_id: int | None, moment: float, min_delay: float
    ) -> None:
        await asyncio.sleep(max(min_delay, moment - queue_logic.now()))
        # Прибираємо себе зі списку ДО apply: apply поставить новий таймер
        # і не повинен скасувати цю задачу посеред роботи.
        if self._timers.get(key) is asyncio.current_task():
            del self._timers[key]
        try:
            await self.apply(key, lambda q: q.tick(), queue_id)
        except QueueError:
            pass  # черга закрилась, поки ми чекали
        except Exception:
            log.exception("Помилка таймера в черзі %s", queue_id)

    def start_timers(self, min_delay: float = 0.0) -> None:
        """Після перезапуску бота — відновити таймери всіх активних черг.

        min_delay — мінімальна пауза перед спрацюванням. Поки бот лежав,
        натиснути «Йду» було неможливо, тож при старті даємо людині ще шанс.
        """
        for queue in self.storage.all():
            self._schedule_timer(queue, min_delay)

    def stop_timers(self) -> None:
        for task in [*self._timers.values(), *self._delayed]:
            task.cancel()
        self._timers.clear()
        self._delayed.clear()

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
        text = render_turn(queue, change)
        if text is None:
            return
        keyboard = turn_keyboard(queue, change) if queue.current is not None else None
        key = (queue.chat_id, queue.thread_id)
        lock = self._announce_locks.setdefault(key, asyncio.Lock())

        async def send() -> None:
            async with lock:  # щоб оголошення не переплутали порядок
                try:
                    msg = await self.bot.send_message(
                        queue.chat_id,
                        text,
                        message_thread_id=queue.thread_id or None,
                        reply_markup=keyboard,
                    )
                except TelegramAPIError as e:
                    log.warning("Не вдалося надіслати оголошення: %s", e)
                    return
                # У чаті лишається тільки останнє «твоя черга» — менше шуму.
                # Свої повідомлення бот може видаляти й без прав адміна.
                previous = self._turn_messages.get(queue.id)
                self._turn_messages[queue.id] = msg.message_id
                if previous is not None:
                    await self._safe(self.bot.delete_message(queue.chat_id, previous))

        self._spawn(send())

    # ------------------------------------------------------------------
    # Допоміжне
    # ------------------------------------------------------------------

    def delete_later(self, chat_id: int, message_id: int, delay: float = ERROR_TTL) -> None:
        """Видалити повідомлення бота через `delay` секунд (відповіді з помилками)."""

        async def run() -> None:
            await asyncio.sleep(delay)
            await self._safe(self.bot.delete_message(chat_id, message_id))

        task = asyncio.create_task(run())
        self._delayed.add(task)
        task.add_done_callback(self._delayed.discard)

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
