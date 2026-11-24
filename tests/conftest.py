import pytest

from bot.queue_logic import Queue


@pytest.fixture
def clock(monkeypatch):
    """Керований годинник: clock.t — поточний час у секундах."""

    class Clock:
        t = 1000.0

    c = Clock()
    monkeypatch.setattr("bot.queue_logic.now", lambda: c.t)
    return c


@pytest.fixture
def make():
    """make(3) — черга з людьми U1, U2, U3 (user_id 1, 2, 3)."""

    def build(people: int = 0, start: bool = False, **kwargs) -> Queue:
        q = Queue(chat_id=1, thread_id=0, title="Лаба <3>", id=7, **kwargs)
        for uid in range(1, people + 1):
            q.join(uid, f"U{uid}")
        if start:
            q.start()
        return q

    return build
