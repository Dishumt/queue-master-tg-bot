"""Зберігання черг: SQLite на диску + кеш у пам'яті.

Чому так:
  - Усі активні черги тримаються в пам'яті (dict), тому читання миттєве —
    обробник кнопки не чекає на диск, щоб дізнатися стан черги.
  - Після кожної зміни черга записується в SQLite. Якщо бот перезапуститься,
    він підніме всі незакриті черги з бази.
  - Бот працює в одному процесі, тож кеш завжди збігається з базою.

Блокування (`lock`) потрібне тому, що aiogram обробляє оновлення паралельно:
якщо двоє натиснуть кнопку одночасно, зміни черги мають іти по черзі 🙂
"""

from __future__ import annotations

import asyncio
import time

import aiosqlite

from bot.queue_logic import Queue, QueueStatus, Slot, SlotStatus

QueueKey = tuple[int, int]  # (chat_id, thread_id)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS queues (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id     INTEGER NOT NULL,
    thread_id   INTEGER NOT NULL DEFAULT 0,
    title       TEXT    NOT NULL,
    step        INTEGER NOT NULL,
    status      TEXT    NOT NULL,
    message_id  INTEGER,
    created_by  INTEGER,
    created_at  INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS queues_status ON queues(status);

CREATE TABLE IF NOT EXISTS slots (
    queue_id  INTEGER NOT NULL REFERENCES queues(id) ON DELETE CASCADE,
    position  INTEGER NOT NULL,
    time      TEXT    NOT NULL,
    user_id   INTEGER,
    name      TEXT,
    status    TEXT    NOT NULL,
    PRIMARY KEY (queue_id, position)
);

-- Ім'я, яке студент сам задав через /name (інакше беремо ім'я з Telegram).
CREATE TABLE IF NOT EXISTS users (
    user_id  INTEGER PRIMARY KEY,
    name     TEXT NOT NULL
);
"""


class Storage:
    def __init__(self, path: str) -> None:
        self._path = path
        self._db: aiosqlite.Connection | None = None
        self._queues: dict[QueueKey, Queue] = {}
        self._names: dict[int, str] = {}
        self._locks: dict[QueueKey, asyncio.Lock] = {}

    # ------------------------------------------------------------------
    # Запуск / зупинка
    # ------------------------------------------------------------------

    async def open(self) -> None:
        self._db = await aiosqlite.connect(self._path)
        # WAL + NORMAL — швидкий запис без втрати надійності для нашого випадку.
        await self._db.execute("PRAGMA journal_mode=WAL")
        await self._db.execute("PRAGMA synchronous=NORMAL")
        await self._db.execute("PRAGMA foreign_keys=ON")
        await self._db.executescript(_SCHEMA)
        await self._db.commit()
        await self._load()

    async def close(self) -> None:
        if self._db is not None:
            await self._db.close()
            self._db = None

    @property
    def db(self) -> aiosqlite.Connection:
        if self._db is None:
            raise RuntimeError("Storage.open() не викликано")
        return self._db

    async def _load(self) -> None:
        async with self.db.execute(
            "SELECT id, chat_id, thread_id, title, step, status, message_id "
            "FROM queues WHERE status != ? ORDER BY id",
            (QueueStatus.CLOSED.value,),
        ) as cur:
            rows = await cur.fetchall()

        for qid, chat_id, thread_id, title, step, status, message_id in rows:
            async with self.db.execute(
                "SELECT time, user_id, name, status FROM slots "
                "WHERE queue_id = ? ORDER BY position",
                (qid,),
            ) as cur:
                slots = [
                    Slot(time=t, user_id=uid, name=name, status=SlotStatus(st))
                    for t, uid, name, st in await cur.fetchall()
                ]
            queue = Queue(
                chat_id=chat_id,
                thread_id=thread_id,
                title=title,
                step=step,
                slots=slots,
                status=QueueStatus(status),
                message_id=message_id,
                id=qid,
            )
            queue.pop_new_current()  # поточного вже кликали до перезапуску
            self._queues[(chat_id, thread_id)] = queue  # новіша перезапише старішу

        async with self.db.execute("SELECT user_id, name FROM users") as cur:
            self._names = {uid: name for uid, name in await cur.fetchall()}

    # ------------------------------------------------------------------
    # Черги
    # ------------------------------------------------------------------

    def get(self, key: QueueKey) -> Queue | None:
        """Активна черга в чаті/топіку. Без звернення до диска."""
        return self._queues.get(key)

    def lock(self, key: QueueKey) -> asyncio.Lock:
        return self._locks.setdefault(key, asyncio.Lock())

    async def create(self, queue: Queue, created_by: int) -> Queue:
        cur = await self.db.execute(
            "INSERT INTO queues (chat_id, thread_id, title, step, status, message_id, "
            "created_by, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                queue.chat_id,
                queue.thread_id,
                queue.title,
                queue.step,
                queue.status.value,
                queue.message_id,
                created_by,
                int(time.time()),
            ),
        )
        queue.id = cur.lastrowid
        await self.db.executemany(
            "INSERT INTO slots (queue_id, position, time, user_id, name, status) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            [
                (queue.id, i, s.time, s.user_id, s.name, s.status.value)
                for i, s in enumerate(queue.slots)
            ],
        )
        await self.db.commit()
        self._queues[(queue.chat_id, queue.thread_id)] = queue
        return queue

    async def save(self, queue: Queue) -> None:
        """Записати поточний стан черги. Закриті черги прибираються з кешу."""
        await self.db.execute(
            "UPDATE queues SET status = ?, message_id = ? WHERE id = ?",
            (queue.status.value, queue.message_id, queue.id),
        )
        await self.db.executemany(
            "UPDATE slots SET user_id = ?, name = ?, status = ? "
            "WHERE queue_id = ? AND position = ?",
            [
                (s.user_id, s.name, s.status.value, queue.id, i)
                for i, s in enumerate(queue.slots)
            ],
        )
        await self.db.commit()
        key = (queue.chat_id, queue.thread_id)
        if queue.status is QueueStatus.CLOSED and self._queues.get(key) is queue:
            del self._queues[key]

    # ------------------------------------------------------------------
    # Імена студентів
    # ------------------------------------------------------------------

    def get_name(self, user_id: int) -> str | None:
        return self._names.get(user_id)

    async def set_name(self, user_id: int, name: str) -> None:
        await self.db.execute(
            "INSERT INTO users (user_id, name) VALUES (?, ?) "
            "ON CONFLICT(user_id) DO UPDATE SET name = excluded.name",
            (user_id, name),
        )
        await self.db.commit()
        self._names[user_id] = name
