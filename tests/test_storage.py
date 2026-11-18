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
