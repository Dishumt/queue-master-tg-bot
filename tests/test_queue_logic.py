import pytest

from bot.parsing import fmt_time
from bot.queue_logic import Queue, QueueError, QueueStatus, Slot, SlotStatus

ADMIN = 999


def make_queue(n: int = 6) -> Queue:
    times = [fmt_time(10 * 60 + 25 + 5 * i) for i in range(n)]  # 10:25, 10:30, ...
    return Queue(chat_id=1, thread_id=0, title="Лаба", step=5, slots=[Slot(t) for t in times])


def statuses(q: Queue) -> list[str]:
    return [s.status.value for s in q.slots]


def names(q: Queue) -> list[str | None]:
    return [s.name for s in q.slots]


# ---------------------------------------------------------------- запис


def test_join_takes_first_free_slot():
    q = make_queue()
    assert q.join(1, "A").time == "10:25"
    assert q.join(2, "B").time == "10:30"


def test_join_specific_slot():
    q = make_queue()
    assert q.join(1, "A", "10:40").time == "10:40"
    assert q.join(2, "B").time == "10:25"


def test_cannot_join_twice():
    q = make_queue()
    q.join(1, "A")
    with pytest.raises(QueueError, match="вже записаний"):
        q.join(1, "A")


def test_cannot_take_busy_slot():
    q = make_queue()
    q.join(1, "A", "10:30")
    with pytest.raises(QueueError, match="зайнятий"):
        q.join(2, "B", "10:30")


def test_unknown_slot():
    q = make_queue()
    with pytest.raises(QueueError, match="немає"):
        q.join(1, "A", "15:00")


def test_no_free_slots():
    q = make_queue(2)
    q.join(1, "A")
    q.join(2, "B")
    with pytest.raises(QueueError, match="Вільних"):
        q.join(3, "C")


def test_leave_frees_slot():
    q = make_queue()
    q.join(1, "A")
    q.leave(1)
    assert q.slots[0].is_free
    with pytest.raises(QueueError, match="немає в цій черзі"):
        q.leave(1)


# ---------------------------------------------------------------- запуск і хід черги


def test_start_requires_people():
    q = make_queue()
    with pytest.raises(QueueError, match="ніхто не записався"):
        q.start()


def test_full_flow():
    q = make_queue()
    q.join(1, "A")
    q.join(2, "B", "10:35")  # 10:30 порожній — має пропуститись
    q.start()
    assert q.current.name == "A"
    assert q.upcoming.name == "B"

    q.finish(1, is_admin=False)
    assert q.current.name == "B"
    assert q.upcoming is None

    q.finish(2, is_admin=False)
    assert q.current is None
    assert q.is_finished
    assert statuses(q)[:3] == ["done", "empty", "done"]


def test_only_current_or_admin_can_finish():
    q = make_queue()
    q.join(1, "A")
    q.join(2, "B")
    q.start()
    with pytest.raises(QueueError, match="Зараз здає A"):
        q.finish(2, is_admin=False)
    q.finish(ADMIN, is_admin=True)
    assert q.current.name == "B"


def test_finish_before_start():
    q = make_queue()
    q.join(1, "A")
    with pytest.raises(QueueError, match="не запущена"):
        q.finish(1, is_admin=False)


def test_admin_marks_specific_slot_out_of_order():
    q = make_queue()
    for uid in (1, 2, 3):
        q.join(uid, f"U{uid}")
    q.start()
    q.finish(ADMIN, is_admin=True, time="10:35")  # U3 прийняли поза чергою
    assert statuses(q)[:3] == ["current", "waiting", "done"]
    q.finish(1, is_admin=False)
    q.finish(2, is_admin=False)
    assert q.is_finished


def test_student_cannot_mark_specific_slot():
    q = make_queue()
    q.join(1, "A")
    q.start()
    with pytest.raises(QueueError, match="тільки адмін"):
        q.finish(1, is_admin=False, time="10:25")


def test_join_while_running_and_queue_empty_becomes_current():
    q = make_queue()
    q.join(1, "A")
    q.start()
    q.finish(1, is_admin=False)
    assert q.is_finished
    q.join(2, "B")
    assert q.current.name == "B"
    assert q.current.time == "10:30"


def test_cannot_join_past_slot_after_start():
    q = make_queue()
    q.join(1, "A", "10:35")
    q.start()
    with pytest.raises(QueueError, match="минув"):
        q.join(2, "B", "10:25")
    # Кнопка «Записатися» бере перший вільний після поточного.
    assert q.join(2, "B").time == "10:40"


def test_current_leaving_calls_next():
    q = make_queue()
    q.join(1, "A")
    q.join(2, "B")
    q.start()
    q.leave(1)
    assert q.current.name == "B"


def test_done_cannot_leave():
    q = make_queue()
    q.join(1, "A")
    q.join(2, "B")
    q.start()
    q.finish(1, is_admin=False)
    with pytest.raises(QueueError, match="вже здав"):
        q.leave(1)


# ---------------------------------------------------------------- пропуски


def test_skip_and_return():
    q = make_queue()
    for uid in (1, 2, 3):
        q.join(uid, f"U{uid}")
    q.start()
    q.skip()  # U1 не прийшов
    assert q.current.name == "U2"
    q.here(1)  # U1 прибіг
    assert q.upcoming.name == "U1"  # іде одразу після поточного
    q.finish(2, is_admin=False)
    assert q.current.name == "U1"
    q.finish(1, is_admin=False)
    assert q.current.name == "U3"


def test_here_when_not_skipped():
    q = make_queue()
    q.join(1, "A")
    with pytest.raises(QueueError, match="не пропускали"):
        q.here(1)


def test_here_after_queue_finished_becomes_current():
    q = make_queue()
    q.join(1, "A")
    q.start()
    q.skip()
    assert q.is_finished
    q.here(1)
    assert q.current.name == "A"


# ---------------------------------------------------------------- «пізніше»


def test_later_swaps_with_next_waiting():
    q = make_queue()
    q.join(1, "A")
    q.join(2, "B", "10:35")
    q.join(3, "C", "10:40")
    q.later(1)
    assert names(q)[:4] == ["B", None, "A", "C"]


def test_later_by_current_passes_the_turn():
    q = make_queue()
    q.join(1, "A")
    q.join(2, "B")
    q.start()
    q.later(1)
    assert q.current.name == "B"
    assert q.current.user_id == 2
    assert q.upcoming.name == "A"


def test_later_when_last():
    q = make_queue()
    q.join(1, "A")
    with pytest.raises(QueueError, match="останній"):
        q.later(1)


# ---------------------------------------------------------------- адмін


def test_add_and_remove():
    q = make_queue()
    q.add("10:30", "Без Телеграму")
    assert q.slots[1].user_id is None
    assert q.slots[1].status is SlotStatus.WAITING
    q.remove("10:30")
    assert q.slots[1].is_free
    with pytest.raises(QueueError, match="порожній"):
        q.remove("10:30")


def test_closed_queue_rejects_everything():
    q = make_queue()
    q.close()
    assert q.status is QueueStatus.CLOSED
    with pytest.raises(QueueError, match="закрита"):
        q.join(1, "A")


# ---------------------------------------------------------------- кого тегати


def test_pop_turn_change_reports_each_change_once():
    q = make_queue()
    q.join(1, "A")
    q.join(2, "B")
    assert q.pop_turn_change() is None  # ще не запущено
    q.start()
    change = q.pop_turn_change()
    assert change.current.name == "A"
    assert change.upcoming.name == "B"
    assert q.pop_turn_change() is None  # вдруге — вже не новий
    q.later(1)  # той самий слот, але інша людина
    assert q.pop_turn_change().current.name == "B"
    q.finish(2, is_admin=False)
    change = q.pop_turn_change()
    assert change.current.name == "A"
    assert change.upcoming is None
    q.finish(1, is_admin=False)
    change = q.pop_turn_change()
    assert change is not None and change.current is None  # черга скінчилась
    assert q.pop_turn_change() is None
