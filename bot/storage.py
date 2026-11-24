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
    status      TEXT    NOT NULL,
    wait_minutes INTEGER NOT NULL DEFAULT 3,
    break_every INTEGER NOT NULL DEFAULT 0,
    turns       INTEGER NOT NULL DEFAULT 0,
    on_break    INTEGER NOT NULL DEFAULT 0,
    message_id  INTEGER,
    created_by  INTEGER,
    created_at  INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS queues_status ON queues(status);

CREATE TABLE IF NOT EXISTS slots (
    queue_id  INTEGER NOT NULL REFERENCES queues(id) ON DELETE CASCADE,
    position  INTEGER NOT NULL,
    user_id   INTEGER,
    name      TEXT,
    status    TEXT    NOT NULL,
    skips     INTEGER NOT NULL DEFAULT 0,
    called_at REAL,
    confirmed INTEGER NOT NULL DEFAULT 0,
    done_at   REAL,
    nudged    INTEGER NOT NULL DEFAULT 0,
    auto_done INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (queue_id, position)
);

-- Ім'я, яке студент сам задав через /name (інакше беремо ім'я з Telegram).
CREATE TABLE IF NOT EXISTS users (
    user_id  INTEGER PRIMARY KEY,
    name     TEXT NOT NULL
);
"""

# Колонки, яких не було в першій версії. Якщо база створена старим кодом,
# _migrate() їх допише (CREATE TABLE IF NOT EXISTS не змінює вже існуючі таблиці).
_ADDED_COLUMNS = {
    "queues": {
        "wait_minutes": "INTEGER NOT NULL DEFAULT 3",
        "break_every": "INTEGER NOT NULL DEFAULT 0",
        "turns": "INTEGER NOT NULL DEFAULT 0",
        "on_break": "INTEGER NOT NULL DEFAULT 0",
    },
    "slots": {
        "skips": "INTEGER NOT NULL DEFAULT 0",
        "called_at": "REAL",
        "confirmed": "INTEGER NOT NULL DEFAULT 0",
        "done_at": "REAL",
        "nudged": "INTEGER NOT NULL DEFAULT 0",
        "auto_done": "INTEGER NOT NULL DEFAULT 0",
    },
}

# Колонки старих версій, де були час слотів і довжина перерви.
_REMOVED_COLUMNS = {"queues": ["step", "break_minutes", "break_until"], "slots": ["time"]}

# Поля слота в тому порядку, в якому вони читаються і пишуться.
# Додаєш поле в Slot — додай його сюди, у _slot_row/_slot_from_row і в схему вище.
_SLOT_FIELDS = (
    "user_id", "name", "status", "skips",
    "called_at", "confirmed", "done_at", "nudged", "auto_done",
)  # fmt: skip
_SLOT_COLUMNS = ", ".join(_SLOT_FIELDS)

# Поля черги, які змінюються після створення.
_QUEUE_FIELDS = (
    "status", "wait_minutes", "break_every",
    "turns", "on_break", "message_id",
)  # fmt: skip


def _queue_row(queue: Queue) -> tuple:
    return tuple(getattr(queue, f) for f in _QUEUE_FIELDS)


def _slot_row(slot: Slot) -> tuple:
    return (
        slot.user_id,
        slot.name,
        slot.status.value,
        slot.skips,
        slot.called_at,
        int(slot.confirmed),
        slot.done_at,
        int(slot.nudged),
        int(slot.auto_done),
    )


def _slot_from_row(row: tuple) -> Slot:
    user_id, name, status, skips, called_at, confirmed, done_at, nudged, auto_done = row
    return Slot(
        user_id=user_id,
        name=name or "",
        status=SlotStatus(status),
        skips=skips,
        called_at=called_at,
        confirmed=bool(confirmed),
        done_at=done_at,
        nudged=bool(nudged),
        auto_done=bool(auto_done),
    )


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
        await self._migrate()
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

    async def _migrate(self) -> None:
        for table, columns in _ADDED_COLUMNS.items():
            async with self.db.execute(f"PRAGMA table_info({table})") as cur:
                existing = {row[1] for row in await cur.fetchall()}
            for column, definition in columns.items():
                if column not in existing:
                    await self.db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
            for column in _REMOVED_COLUMNS[table]:
                if column in existing:
                    await self.db.execute(f"ALTER TABLE {table} DROP COLUMN {column}")
        # Порожніх слотів більше не буває.
        await self.db.execute("DELETE FROM slots WHERE status = 'empty'")

    def all(self) -> list[Queue]:
        """Усі активні черги (для відновлення таймерів після перезапуску)."""
        return list(self._queues.values())

    async def _load(self) -> None:
        async with self.db.execute(
            f"SELECT id, chat_id, thread_id, title, {', '.join(_QUEUE_FIELDS)} "
            "FROM queues WHERE status != ? ORDER BY id",
            (QueueStatus.CLOSED.value,),
        ) as cur:
            rows = await cur.fetchall()

        for qid, chat_id, thread_id, title, *values in rows:
            async with self.db.execute(
                f"SELECT {_SLOT_COLUMNS} FROM slots WHERE queue_id = ? ORDER BY position",
                (qid,),
            ) as cur:
                slots = [_slot_from_row(row) for row in await cur.fetchall()]
            fields = dict(zip(_QUEUE_FIELDS, values))
            fields["status"] = QueueStatus(fields["status"])
            fields["on_break"] = bool(fields["on_break"])
            queue = Queue(
                id=qid, chat_id=chat_id, thread_id=thread_id, title=title, slots=slots, **fields
            )
            queue.pop_turn_change()  # поточного вже кликали до перезапуску
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
        columns = ("chat_id", "thread_id", "title", "created_by", "created_at", *_QUEUE_FIELDS)
        cur = await self.db.execute(
            f"INSERT INTO queues ({', '.join(columns)}) VALUES ({', '.join('?' * len(columns))})",
            (
                queue.chat_id,
                queue.thread_id,
                queue.title,
                created_by,
                int(time.time()),
                *_queue_row(queue),
            ),
        )
        queue.id = cur.lastrowid
        await self._write_slots(queue)
        await self.db.commit()
        self._queues[(queue.chat_id, queue.thread_id)] = queue
        return queue

    async def _write_slots(self, queue: Queue) -> None:
        # Людей може стати більше чи менше, а порядок — змінитись,
        # тому простіше переписати список цілком (до 50 рядків — це миттєво).
        await self.db.execute("DELETE FROM slots WHERE queue_id = ?", (queue.id,))
        await self.db.executemany(
            f"INSERT INTO slots (queue_id, position, {_SLOT_COLUMNS}) "
            f"VALUES ({', '.join('?' * (len(_SLOT_FIELDS) + 2))})",
            [(queue.id, i, *_slot_row(s)) for i, s in enumerate(queue.slots)],
        )

    async def save(self, queue: Queue) -> None:
        """Записати поточний стан черги. Закриті черги прибираються з кешу."""
        await self.db.execute(
            f"UPDATE queues SET {', '.join(f'{c} = ?' for c in _QUEUE_FIELDS)} WHERE id = ?",
            (*_queue_row(queue), queue.id),
        )
        await self._write_slots(queue)
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
