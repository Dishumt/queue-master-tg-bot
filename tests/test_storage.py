import aiosqlite

from bot.queue_logic import QueueStatus
from bot.storage import Storage


async def reopen(storage: Storage, path: str) -> Storage:
    await storage.close()
    fresh = Storage(path)
    await fresh.open()
    return fresh


async def test_queue_survives_restart(tmp_path, make):
    path = str(tmp_path / "q.db")
    storage = Storage(path)
    await storage.open()
    q = make(wait_minutes=4, break_every=2, break_minutes=10)
    await storage.create(q, created_by=1)
    for uid in (1, 2, 3, 4):  # люди додаються вже після створення
        q.join(uid, f"U{uid}")
    q.start()
    q.skip()
    q.here(1)
    q.leave(4)
    q.confirm(2)
    q.message_id = 777
    q.pop_turn_change()
    await storage.save(q)
    await storage.set_name(1, "Коваленко Олена")

    storage = await reopen(storage, path)
    loaded = storage.get((1, 0))
    assert [s.name for s in loaded.slots] == ["U2", "U3", "U1"]
    assert loaded.slots == q.slots
    assert (loaded.status, loaded.message_id) == (QueueStatus.RUNNING, 777)
    assert (loaded.wait_minutes, loaded.break_every, loaded.break_minutes) == (4, 2, 10)
    assert loaded.pop_turn_change() is None  # поточного вже кликали
    assert storage.get_name(1) == "Коваленко Олена"

    loaded.finish(2, False)
    loaded.finish(3, False)
    assert loaded.on_break
    await storage.save(loaded)
    storage = await reopen(storage, path)
    assert storage.get((1, 0)).break_until == loaded.break_until  # перерва теж триває
    await storage.close()


async def test_closed_queue_leaves_cache(tmp_path, make):
    path = str(tmp_path / "q.db")
    storage = Storage(path)
    await storage.open()
    q = make(1)
    await storage.create(q, created_by=1)
    q.close()
    await storage.save(q)
    assert storage.get((1, 0)) is None
    storage = await reopen(storage, path)
    assert storage.get((1, 0)) is None
    await storage.close()


async def test_migrates_database_with_time_slots(tmp_path, make):
    path = str(tmp_path / "old.db")
    async with aiosqlite.connect(path) as db:
        await db.executescript(
            """
            CREATE TABLE queues (id INTEGER PRIMARY KEY AUTOINCREMENT, chat_id INTEGER NOT NULL,
                thread_id INTEGER NOT NULL DEFAULT 0, title TEXT NOT NULL, step INTEGER NOT NULL,
                status TEXT NOT NULL, message_id INTEGER, created_by INTEGER, created_at INTEGER NOT NULL);
            CREATE TABLE slots (queue_id INTEGER NOT NULL, position INTEGER NOT NULL, time TEXT NOT NULL,
                user_id INTEGER, name TEXT, status TEXT NOT NULL, PRIMARY KEY (queue_id, position));
            INSERT INTO queues VALUES (1, 5, 0, 'Стара', 5, 'open', 10, 1, 0);
            INSERT INTO slots VALUES (1, 0, '10:00', NULL, NULL, 'empty');
            INSERT INTO slots VALUES (1, 1, '10:05', 7, 'A', 'waiting');
            """
        )
        await db.commit()

    storage = Storage(path)
    await storage.open()
    q = storage.get((5, 0))
    assert [s.name for s in q.slots] == ["A"]  # порожні слоти зникли
    q.join(8, "B")
    await storage.save(q)
    await storage.create(make(1), created_by=1)  # нові черги теж записуються
    storage = await reopen(storage, path)
    assert [s.name for s in storage.get((5, 0)).slots] == ["A", "B"]
    await storage.close()
