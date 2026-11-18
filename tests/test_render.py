from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from bot.parsing import parse_new_args
from bot.queue_logic import Queue, Slot, slot_moment
from bot.render import (
    mention,
    parse_callback,
    plural,
    queue_keyboard,
    render_position,
    render_queue,
    render_turn,
    turn_keyboard,
)

LAB_DAY = datetime(2026, 11, 20, 12, 0, tzinfo=ZoneInfo("Europe/Kyiv")).timestamp()


def make_queue() -> Queue:
    times = parse_new_args("Лаба <3> 10:00-10:30 5 перерва 10:10-10:20").slot_times()
    return Queue(chat_id=1, thread_id=0, title="Лаба <3>", step=5, slots=[Slot(t) for t in times], id=7)


def test_render_running_queue():
    q = make_queue()
    q.join(1, "Олена")
    q.join(2, "Андрій")
    q.join(3, "Марія")
    q.start()
    q.finish(1, is_admin=False)
    text = render_queue(q)

    assert "Лаба &lt;3&gt;" in text  # HTML екранується
    assert "здали 1 з 3" in text
    assert "✅ Олена" in text
    assert "📣 <b>Андрій</b>" in text  # покликали, ще не підтвердив
    assert "⏳ Марія" in text
    assert "перерва" in text
    # перерва стоїть між 10:05 і 10:20
    assert text.index("10:05") < text.index("перерва") < text.index("10:20")

    q.confirm(2)
    assert "▶️ <b>Андрій</b>  ← здає" in render_queue(q)


def test_returned_person_out_of_time_order_has_no_fake_break():
    times = parse_new_args("X 10:00-10:30 5").slot_times()
    q = Queue(chat_id=1, thread_id=0, title="X", step=5, slots=[Slot(t) for t in times])
    for uid in (1, 2, 3, 4):
        q.join(uid, f"U{uid}")
    q.start()
    q.skip()
    q.here(1)  # 10:05 U2 | 10:10 U3 | 10:00 U1 | 10:15 U4
    text = render_queue(q)
    assert "перерва" not in text
    assert "↩️" in text
    assert text.index("U3") < text.index("U1") < text.index("U4")


def test_return_in_the_middle_keeps_only_real_break():
    # Як у симуляції: Андрій (10:30) пропущений і повернувся — між 10:25
    # і 10:35 не має з'являтися фальшива перерва, а справжня (до 11:10) лишається.
    times = parse_new_args("X 10:25-11:20 5 перерва 11:00-11:10").slot_times()
    q = Queue(chat_id=1, thread_id=0, title="X", step=5, slots=[Slot(t) for t in times])
    for uid in (1, 2, 3, 4):
        q.join(uid, f"U{uid}")
    q.join(5, "U5", "11:10")
    q.start()
    q.finish(1, is_admin=False)
    q.skip()  # U2 (10:30)
    q.here(2)  # 10:25 U1 | 10:35 U3 | 10:40 U4 | 10:30 U2 | ...
    text = render_queue(q)
    assert text.count("перерва") == 1
    assert text.index("10:55") < text.index("перерва") < text.index("11:10")


def test_render_long_queue_fits_telegram_limit():
    times = parse_new_args("X 08:00-13:00 5").slot_times()
    q = Queue(chat_id=1, thread_id=0, title="X" * 100, step=5, slots=[Slot(t) for t in times])
    for i, _ in enumerate(times):
        q.join(i, "Я" * 40)
    assert len(render_queue(q)) < 4096 + 1500  # з HTML-тегами; видимий текст коротший


def test_mention():
    assert mention(Slot("10:00", 5, "A&B")) == '<a href="tg://user?id=5">A&amp;B</a>'
    assert mention(Slot("10:00", None, "Без ТГ")) == "<b>Без ТГ</b>"


def buttons(kb) -> list[str]:
    return [b.callback_data.split(":")[1] for row in kb.inline_keyboard for b in row]


def test_keyboard_and_callback_roundtrip():
    q = make_queue()
    assert buttons(queue_keyboard(q)) == ["join", "where", "leave"]  # до старту — тільки запис
    q.join(1, "A")
    q.start()
    assert buttons(queue_keyboard(q)) == [
        "join", "where", "coming", "done", "later", "enter", "leave",
    ]  # fmt: skip
    # «Виписатися» — окремим рядком унизу, далеко від «Записатися».
    assert [b.text for b in queue_keyboard(q).inline_keyboard[-1]] == ["❌ Виписатися"]
    assert parse_callback("q:done:7") == ("done", 7)
    assert parse_callback("q:hack:7") is None
    assert parse_callback("q:done:x") is None
    assert parse_callback(None) is None
    q.close()
    assert queue_keyboard(q) is None


@pytest.fixture
def clock(monkeypatch):
    class Clock:
        t = slot_moment("10:00", LAB_DAY)

    c = Clock()
    monkeypatch.setattr("bot.queue_logic.now", lambda: c.t)
    return c


def test_render_turn_messages(clock):
    q = make_queue()
    q.join(1, "Олена")
    q.join(2, "Андрій")
    q.join(3, "Марія")
    q.start()
    change = q.pop_turn_change()
    text = render_turn(q, change)
    assert "Олена</a>, твоя черга!" in text
    assert "(10:00)" not in text  # час слота в оголошенні не показуємо
    assert "Натисни «🏃 Йду» до 10:03" in text
    assert "Андрій</a>, готуйся — далі ти." in text
    assert buttons(turn_keyboard(q, change)) == ["coming", "done", "later", "enter", "where"]

    clock.t += 180
    q.expire()
    change = q.pop_turn_change()
    text = render_turn(q, change)
    assert text.startswith("⏭ ") and "не відгукується" in text and "🙋 Я тут" in text
    assert "Андрій</a>, твоя черга" in text
    assert buttons(turn_keyboard(q, change))[-1] == "here"  # можна одразу повернутися

    q.confirm(2)
    clock.t += 300
    q.enter(3, is_admin=False)  # Андрій забув «Готово», Марія заходить
    text = render_turn(q, q.pop_turn_change())
    assert "Андрій</a> — «✅ Готово» поставлено автоматично" in text
    assert "▶️ Зараз здає: <a" in text and "Марія" in text

    q.finish(3, is_admin=False)
    assert "Більше нікого" in render_turn(q, q.pop_turn_change())


def test_render_deferred_and_nudge(clock):
    q = make_queue()
    q.join(1, "Олена")
    q.join(2, "Андрій", "10:05")
    q.join(3, "Марія", "10:20")
    q.start()
    q.finish(1, is_admin=False)  # Андрія (10:05) кличуть о 10:00 — зарано
    q.pop_turn_change()
    clock.t += 90
    q.enter(3, is_admin=False)
    text = render_turn(q, q.pop_turn_change())
    assert "Андрій</a>, тебе покликали раніше за розклад" in text
    assert "готуйся" not in text  # «далі ти» вже сказано в рядку про Андрія

    clock.t = slot_moment("10:20", LAB_DAY)
    q.tick()
    text = render_turn(q, q.pop_turn_change())
    assert text.startswith("⏰ ") and "Марія</a>, ти ще в кабінеті?" in text
    assert "Андрій</a>, якщо бачиш, що кабінет вільний" in text


def test_render_position(clock):
    times = parse_new_args("X 10:00-10:40 5").slot_times()
    q = Queue(chat_id=1, thread_id=0, title="X", step=5, slots=[Slot(t) for t in times], id=7)
    for uid, name in enumerate(["A", "B", "C", "D", "E", "F"], start=1):
        q.join(uid, name)
    assert render_position(q, 99).startswith("Тебе немає")
    assert "Перед тобою 3 людини. Черга ще не почалась" in render_position(q, 4)
    q.start()
    assert render_position(q, 1) == "Зараз твоя черга! Натисни «🏃 Йду»."
    # Даних ще немає → 5 хв на людину (крок розкладу).
    assert render_position(q, 6) == "Перед тобою 5 людей, орієнтовно ~25 хв."
    assert render_position(q, 2) == "Перед тобою 1 людина, орієнтовно ~5 хв."
    q.skip()
    assert "Тебе пропустили" in render_position(q, 1)
    assert "ще 1 людина" in render_position(q, 1)
    assert all(len(render_position(q, uid)) <= 200 for uid in range(1, 8))


def test_plural():
    assert [plural(n, "людина", "людини", "людей") for n in (1, 2, 5, 11, 21, 22, 25)] == [
        "людина", "людини", "людей", "людей", "людина", "людини", "людей",
    ]  # fmt: skip
