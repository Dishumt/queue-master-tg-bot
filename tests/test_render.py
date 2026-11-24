import re

from bot.queue_logic import MAX_PEOPLE, Slot
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


def buttons(kb) -> list[str]:
    return [b.callback_data.split(":")[1] for row in kb.inline_keyboard for b in row]


def test_render_queue(make):
    q = make(4)
    assert "зайнято 4 з 50" in render_queue(q)
    q.start()
    q.finish(1, False)
    q.skip()
    text = render_queue(q)
    assert "Лаба &lt;3&gt;" in text  # HTML екранується
    assert "здали 1 з 4" in text
    assert "1. ✅ U1" in text
    assert "2. ⏭ <s>U2</s>" in text
    assert "3. 📣 <b>U3</b>" in text
    assert "4. ⏳ U4" in text
    q.confirm(3)
    assert "3. ▶️ <b>U3</b>" in render_queue(q)
    q.here(2)
    assert "U2 ↩️" in render_queue(q)


def test_render_breaks(make):
    q = make(5, break_every=2)
    lines = render_queue(q).split("\n")
    before = [lines[i - 1] for i, line in enumerate(lines) if "── ☕" in line]
    assert ["U2" in before[0], "U4" in before[1], len(before)] == [True, True, 2]
    q.start()
    q.finish(1, False)
    q.finish(2, False)
    text = render_queue(q)
    assert "☕ Перерва · здали 2 з 5" in text
    assert text.count("── ☕") == 1  # лишилась тільки майбутня, після U4
    # Після останнього перерви не буває.
    last = make(2, break_every=2)
    assert "── ☕" not in render_queue(last)
    last.start()
    assert "── ☕" not in render_queue(last)


def test_long_queue_fits_telegram_limit(make):
    q = make(break_every=1)
    for i in range(MAX_PEOPLE):
        q.join(i, "Я" * 40)
    q.start()
    visible = re.sub(r"<[^>]+>", "", render_queue(q))
    assert len(visible.encode("utf-16-le")) // 2 <= 4096


def test_mention():
    assert mention(Slot(5, "A&B")) == '<a href="tg://user?id=5">A&amp;B</a>'
    assert mention(Slot(None, "Без ТГ")) == "<b>Без ТГ</b>"


def test_keyboard_and_callback_roundtrip(make):
    q = make(1)
    open_buttons = buttons(queue_keyboard(make(1, places=3)))
    assert open_buttons == ["p2", "p3", "join", "where", "leave"]  # 1 зайняте
    assert parse_callback("q:p15:7") == ("p15", 7)
    assert parse_callback("q:px:7") is None
    q.start()
    assert buttons(queue_keyboard(q)) == [
        "join", "where", "coming", "done", "later", "enter", "leave",
    ]  # fmt: skip
    assert parse_callback("q:done:7") == ("done", 7)
    for bad in ("q:hack:7", "q:done:x", None):
        assert parse_callback(bad) is None
    q.close()
    assert queue_keyboard(q) is None


def test_turn_messages(make, clock):
    q = make(4, start=True)
    change = q.pop_turn_change()
    text = render_turn(q, change)
    assert "U1</a>, твоя черга!" in text and "протягом 3 хв" in text
    assert "U2</a>, готуйся — далі ти." in text
    assert "here" not in buttons(turn_keyboard(q, change))

    clock.t += 180
    q.tick()
    change = q.pop_turn_change()
    assert render_turn(q, change).startswith("⏭ ")
    assert buttons(turn_keyboard(q, change))[-1] == "here"

    q.confirm(2)
    clock.t += 600
    q.tick()
    text = render_turn(q, q.pop_turn_change())
    assert text.startswith("⏰ ") and "U3</a>, якщо бачиш" in text

    q.enter(3, False)
    text = render_turn(q, q.pop_turn_change())
    assert "поставлено автоматично" in text and "Зараз здає" in text


def test_break_message(make):
    q = make(2, start=True, break_every=1)
    q.pop_turn_change()
    q.finish(1, False)
    text = render_turn(q, q.pop_turn_change())
    assert "☕ Перерва" in text and "U2</a>, далі ти" in text and "✅ Готово" in text


def test_position(make, clock):
    q = make(6)
    assert render_position(q, 99).startswith("Тебе немає")
    assert "Перед тобою 3 людини. Черга ще не почалась" in render_position(q, 4)
    q.start()
    assert render_position(q, 1) == "Зараз твоя черга! Натисни «🏃 Йду»."
    assert render_position(q, 6) == "Твоє місце — 6. Перед тобою 5 людей, орієнтовно ~25 хв."
    q.skip()
    assert "ще 1 людина" in render_position(q, 1)
    assert all(len(render_position(q, uid)) <= 200 for uid in range(1, 8))


def test_plural():
    assert [plural(n, "людина", "людини", "людей") for n in (1, 2, 5, 11, 21, 22, 25)] == [
        "людина", "людини", "людей", "людей", "людина", "людини", "людей",
    ]  # fmt: skip
