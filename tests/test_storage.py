from bot.queue_logic import Queue, QueueStatus, Slot
from bot.storage import Storage


async def test_queue_survives_restart(tmp_path):
    path = str(tmp_path / "q.db")

    storage = Storage(path)
    await storage.open()
    q = Queue(chat_id=10, thread_id=0, title="Лаба", step=5, slots=[Slot("10:25"), Slot("10:30")])
    await storage.create(q, created_by=1)
    q.join(1, "A")
    q.join(2, "B")
    q.start()
    q.message_id = 777
    q.pop_turn_change()
    await storage.save(q)
    await storage.set_name(1, "Петрашкевич Артем")
    await storage.close()

    storage = Storage(path)
    await storage.open()
    loaded = storage.get((10, 0))
    assert loaded is not None
    assert loaded.message_id == 777
    assert loaded.status is QueueStatus.RUNNING
    assert loaded.current.name == "A"
    assert loaded.upcoming.name == "B"
    # Поточного вже тегали до перезапуску — повторно не тегаємо.
    assert loaded.pop_turn_change() is None
    assert storage.get_name(1) == "Петрашкевич Артем"
    await storage.close()


async def test_closed_queue_leaves_cache(tmp_path):
    storage = Storage(str(tmp_path / "q.db"))
    await storage.open()
    q = Queue(chat_id=10, thread_id=5, title="Лаба", step=5, slots=[Slot("10:25")])
    await storage.create(q, created_by=1)
    assert storage.get((10, 5)) is q
    q.close()
    await storage.save(q)
    assert storage.get((10, 5)) is None
    await storage.close()

    storage = Storage(str(tmp_path / "q.db"))
    await storage.open()
    assert storage.get((10, 5)) is None
    await storage.close()


async def test_skips_order_and_timer_survive_restart(tmp_path):
    path = str(tmp_path / "q.db")
    storage = Storage(path)
    await storage.open()
    q = Queue(chat_id=1, thread_id=0, title="Л", step=5, wait_minutes=4,
              slots=[Slot(t) for t in ("10:00", "10:05", "10:10", "10:15")])
    await storage.create(q, created_by=1)
    for uid in (1, 2, 3):
        q.join(uid, f"U{uid}")
    q.start()
    q.skip()
    q.here(1)  # U1 переїхав за U3: U2 | U3 U1
    q.confirm(2)
    await storage.save(q)
    await storage.close()

    storage = Storage(path)
    await storage.open()
    loaded = storage.get((1, 0))
    assert [s.name for s in loaded.slots[:3]] == ["U2", "U3", "U1"]
    assert [s.time for s in loaded.slots[:3]] == ["10:05", "10:10", "10:00"]
    assert loaded.find_user(1).skips == 1
    assert loaded.current.confirmed is True
    assert loaded.current.called_at is not None
    assert loaded.wait_minutes == 4
    await storage.close()


async def test_migrates_database_from_first_version(tmp_path):
    import aiosqlite

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
            INSERT INTO slots VALUES (1, 0, '10:00', 7, 'A', 'waiting');
            """
        )
        await db.commit()

    storage = Storage(path)
    await storage.open()
    q = storage.get((5, 0))
    assert q.title == "Стара"
    assert q.wait_minutes == 3
    assert q.slots[0].skips == 0 and q.slots[0].confirmed is False
    await storage.close()


async def test_turn_details_survive_restart(tmp_path):
    path = str(tmp_path / "q.db")
    storage = Storage(path)
    await storage.open()
    q = Queue(chat_id=2, thread_id=0, title="Л", step=5, slots=[Slot("10:00"), Slot("10:05")])
    await storage.create(q, created_by=1)
    q.join(1, "A")
    q.join(2, "B")
    q.start()
    q.confirm(1)
    q.enter(2, is_admin=True)  # A забув «Готово» — закрито автоматично
    q.current.nudged = True
    await storage.save(q)
    await storage.close()

    storage = Storage(path)
    await storage.open()
    loaded = storage.get((2, 0))
    a = loaded.find_user(1)
    assert a.auto_done and a.done_at is not None
    assert loaded.current.nudged and loaded.current.confirmed
    await storage.close()
