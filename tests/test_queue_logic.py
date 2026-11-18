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


def call_order(q: Queue) -> list[str]:
    """Імена в порядку виклику: поточний, потім ті, хто чекає."""
    order = [s.name for s in q.slots if s.status is SlotStatus.CURRENT]
    return order + [s.name for s in q.slots if s.status is SlotStatus.WAITING]


def test_first_return_lets_one_person_ahead():
    q = make_queue(8)
    for uid in range(1, 6):
        q.join(uid, f"U{uid}")
    q.start()
    q.skip()  # U1 не прийшов
    assert q.current.name == "U2"
    q.here(1)  # U1 прибіг — пропускає вперед одного (U3)
    assert call_order(q) == ["U2", "U3", "U1", "U4", "U5"]
    assert q.find_user(1).time == "10:25"  # час-мітка переїхала разом із людиною


def test_second_return_lets_two_ahead_then_end():
    q = make_queue(10)
    for uid in range(1, 8):
        q.join(uid, f"U{uid}")
    q.start()
    q.skip()
    q.here(1)  # 1-й пропуск: U2 | U3 U1 U4 ...
    q.finish(2, is_admin=False)
    q.finish(3, is_admin=False)
    assert q.current.name == "U1"
    q.skip()  # знову не прийшов
    q.here(1)  # 2-й пропуск: U4 | U5 U6 U1 U7
    assert call_order(q) == ["U4", "U5", "U6", "U1", "U7"]
    q.finish(4, is_admin=False)
    q.finish(5, is_admin=False)
    q.finish(6, is_admin=False)
    q.skip()  # утретє
    q.here(1)  # далі — завжди в кінець
    assert call_order(q) == ["U7", "U1"]


def test_return_to_end_goes_after_last_occupied_slot():
    q = make_queue(8)
    q.join(1, "U1")
    q.join(2, "U2")
    q.join(3, "U3")
    q.find_user(1).skips = 2  # уже двічі пропускали
    q.start()
    q.skip()
    q.here(1)
    assert call_order(q) == ["U2", "U3", "U1"]
    # Хто запишеться пізніше — піде вже після U1.
    q.join(4, "U4")
    assert call_order(q) == ["U2", "U3", "U1", "U4"]


def test_return_with_nobody_waiting_calls_immediately():
    q = make_queue()
    q.join(1, "A")
    q.join(2, "B")
    q.start()
    q.skip()
    q.here(1)  # чекає тільки B (поточний) — після нього нікого, тож A наступна
    assert call_order(q) == ["B", "A"]


# ---------------------------------------------------------------- «Йду» і автопропуск


@pytest.fixture
def clock(monkeypatch):
    """Керований годинник: clock.t — поточний час у секундах."""

    class Clock:
        t = 1000.0

    c = Clock()
    monkeypatch.setattr("bot.queue_logic.now", lambda: c.t)
    return c


def test_auto_skip_after_timeout(clock):
    q = make_queue()
    q.join(1, "A")
    q.join(2, "B")
    q.start()
    assert q.confirm_deadline == 1000 + 3 * 60
    clock.t += 179
    assert q.expire() is None  # ще є секунда
    clock.t += 1
    assert q.expire().name == "A"
    assert q.current.name == "B"
    assert q.confirm_deadline == clock.t + 180  # B має свої 3 хвилини


def test_confirm_disables_auto_skip(clock):
    q = make_queue()
    q.join(1, "A")
    q.join(2, "B")
    q.start()
    with pytest.raises(QueueError, match="не твоя черга"):
        q.confirm(2)
    q.confirm(1)
    assert q.confirm_deadline is None
    clock.t += 3600
    assert q.expire() is None
    assert q.current.name == "A"


def test_wait_zero_disables_auto_skip(clock):
    q = make_queue()
    q.join(1, "A")
    q.set_wait(0)
    q.start()
    assert q.confirm_deadline is None


def test_person_without_telegram_is_not_auto_skipped(clock):
    q = make_queue()
    q.add("10:25", "Без ТГ")
    q.start()
    assert q.confirm_deadline is None


def test_absent_button_rules(clock):
    q = make_queue()
    for uid in (1, 2, 3):
        q.join(uid, f"U{uid}")
    q.start()
    with pytest.raises(QueueError, match="ще"):
        q.absent(2, is_admin=False)  # одразу не можна — дай хвилину
    clock.t += 60
    with pytest.raises(QueueError, match="наступний у черзі"):
        q.absent(3, is_admin=False)  # U3 не наступний
    assert q.absent(2, is_admin=False).name == "U1"
    assert q.current.name == "U2"


def test_absent_not_allowed_after_confirm_but_admin_can(clock):
    q = make_queue()
    q.join(1, "A")
    q.join(2, "B")
    q.start()
    q.confirm(1)
    clock.t += 600
    with pytest.raises(QueueError, match="уже підтвердив"):
        q.absent(2, is_admin=False)
    q.absent(999, is_admin=True)
    assert q.current.name == "B"


def test_later_by_current_restarts_timer(clock):
    q = make_queue()
    q.join(1, "A")
    q.join(2, "B")
    q.start()
    q.confirm(1)
    clock.t += 100
    q.later(1)
    assert q.current.name == "B"
    assert not q.current.confirmed
    assert q.confirm_deadline == clock.t + 180


def test_skip_reported_in_turn_change(clock):
    q = make_queue()
    q.join(1, "A")
    q.join(2, "B")
    q.start()
    q.pop_turn_change()
    clock.t += 180
    q.expire()
    change = q.pop_turn_change()
    assert change.skipped.name == "A"
    assert change.current.name == "B"


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
