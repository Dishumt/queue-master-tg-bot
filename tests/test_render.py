from bot.parsing import parse_new_args
from bot.queue_logic import Queue, Slot
from bot.render import mention, parse_callback, queue_keyboard, render_queue


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
    assert "▶️ <b>Андрій</b>" in text
    assert "⏳ Марія" in text
    assert "перерва" in text
    # перерва стоїть між 10:05 і 10:20
    assert text.index("10:05") < text.index("перерва") < text.index("10:20")


def test_render_long_queue_fits_telegram_limit():
    times = parse_new_args("X 08:00-13:00 5").slot_times()
    q = Queue(chat_id=1, thread_id=0, title="X" * 100, step=5, slots=[Slot(t) for t in times])
    for i, _ in enumerate(times):
        q.join(i, "Я" * 40)
    assert len(render_queue(q)) < 4096 + 1500  # з HTML-тегами; видимий текст коротший


def test_mention():
    assert mention(Slot("10:00", 5, "A&B")) == '<a href="tg://user?id=5">A&amp;B</a>'
    assert mention(Slot("10:00", None, "Без ТГ")) == "<b>Без ТГ</b>"


def test_keyboard_and_callback_roundtrip():
    q = make_queue()
    kb = queue_keyboard(q)
    datas = [b.callback_data for row in kb.inline_keyboard for b in row]
    assert datas == ["q:join:7", "q:leave:7", "q:done:7", "q:later:7"]
    assert parse_callback("q:done:7") == ("done", 7)
    assert parse_callback("q:hack:7") is None
    assert parse_callback("q:done:x") is None
    assert parse_callback(None) is None
    q.close()
    assert queue_keyboard(q) is None
