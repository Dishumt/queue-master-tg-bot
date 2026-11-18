from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from bot.parsing import fmt_time
from bot.queue_logic import (
    Entered,
    Queue,
    QueueError,
    QueueStatus,
    Slot,
    SlotStatus,
    slot_moment,
)

ADMIN = 999
LAB_DAY = datetime(2026, 11, 20, 12, 0, tzinfo=ZoneInfo("Europe/Kyiv")).timestamp()


def at(label: str) -> float:
    """Момент «10:25» у день пари (за київським часом)."""
    return slot_moment(label, LAB_DAY)


def make_queue(n: int = 6) -> Queue:
    times = [fmt_time(10 * 60 + 25 + 5 * i) for i in range(n)]  # 10:25, 10:30, ...
    return Queue(chat_id=1, thread_id=0, title="Лаба", step=5, slots=[Slot(t) for t in times])


def statuses(q: Queue) -> list[str]:
    return [s.status.value for s in q.slots]


def names(q: Queue) -> list[str | None]:
    return [s.name for s in q.slots]


def call_order(q: Queue) -> list[str]:
    """Імена в порядку виклику: поточний, потім ті, хто чекає."""
    order = [s.name for s in q.slots if s.status is SlotStatus.CURRENT]
    return order + [s.name for s in q.slots if s.status is SlotStatus.WAITING]


@pytest.fixture
def clock(monkeypatch):
    """Керований годинник: clock.t — поточний час (секунди). Старт — 10:25 у день пари."""

    class Clock:
        t = at("10:25")

    c = Clock()
    monkeypatch.setattr("bot.queue_logic.now", lambda: c.t)
    return c


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
    with pytest.raises(QueueError, match="вже в черзі"):
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
    with pytest.raises(QueueError, match="вже відмічено"):
        q.leave(1)


# ---------------------------------------------------------------- пропуски і повернення


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


def test_sign_up_returns_skipped_person():
    q = make_queue()
    for uid in (1, 2, 3):
        q.join(uid, f"U{uid}")
    q.start()
    q.skip()
    with pytest.raises(QueueError, match="пропустили"):
        q.join(1, "U1")  # /join підказує, як повернутися
    returned, slot = q.sign_up(1, "U1")
    assert returned and slot.name == "U1"
    assert call_order(q) == ["U2", "U3", "U1"]
    assert q.sign_up(4, "U4")[0] is False  # новенький — просто записується


# ---------------------------------------------------------------- «Йду» і автопропуск


def test_auto_skip_after_timeout(clock):
    q = make_queue()
    q.join(1, "A")
    q.join(2, "B")
    q.start()
    assert q.confirm_deadline == at("10:25") + 3 * 60
    clock.t += 179
    assert q.expire() is None  # ще є секунда
    clock.t += 1
    assert q.expire().name == "A"
    assert q.current.name == "B"


def test_called_before_slot_time_waits_until_slot_time(clock):
    # Черга йде швидше за розклад: B (10:30) кличуть о 10:28.
    q = make_queue()
    q.join(1, "A")
    q.join(2, "B")
    q.start()
    clock.t = at("10:28")
    q.finish(1, is_admin=False)
    assert q.current.name == "B"
    # Відлік 3 хвилин — з 10:30, а не з моменту виклику.
    assert q.confirm_deadline == at("10:30") + 180
    clock.t = at("10:32")
    assert q.expire() is None
    clock.t = at("10:33")
    assert q.expire().name == "B"


def test_confirm_disables_auto_skip(clock):
    q = make_queue()
    q.join(1, "A")
    q.join(2, "B")
    q.start()
    with pytest.raises(QueueError, match="не твоя черга"):
        q.confirm(2)
    q.confirm(1)
    assert q.confirm_deadline is None
    clock.t += 300
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


# ---------------------------------------------------------------- «🚪 Заходжу»


def test_enter_rules_and_no_show(clock):
    q = make_queue()
    for uid in (1, 2, 3):
        q.join(uid, f"U{uid}")
    q.start()
    with pytest.raises(QueueError, match="дай ще"):
        q.enter(2, is_admin=False)  # U1 покликали щойно
    clock.t += 60
    with pytest.raises(QueueError, match="тільки наступна людина"):
        q.enter(3, is_admin=False)  # U3 не наступний
    outcome, previous = q.enter(2, is_admin=False)
    assert outcome is Entered.SKIPPED  # U1 не натиснув «Йду», і його час уже настав
    assert previous.name == "U1" and previous.status is SlotStatus.SKIPPED
    assert q.current.name == "U2"
    assert q.current.confirmed  # хто зайшов — той уже в кабінеті
    assert q.pop_turn_change().skipped.name == "U1"


def test_enter_closes_previous_who_forgot_done(clock):
    q = make_queue()
    q.join(1, "A")
    q.join(2, "B")
    q.join(3, "C")
    q.start()
    q.confirm(1)  # A зайшов…
    clock.t += 300  # …здав і вийшов, а «Готово» не натиснув
    outcome, previous = q.enter(2, is_admin=False)
    assert outcome is Entered.CLOSED
    assert previous.status is SlotStatus.DONE and previous.auto_done
    assert q.current.name == "B" and q.current.confirmed
    change = q.pop_turn_change()
    assert change.closed.name == "A"
    assert change.upcoming.name == "C"  # C дізнається, що далі він


def test_wrongly_closed_person_returns_without_penalty(clock):
    q = make_queue()
    for uid in (1, 2, 3):
        q.join(uid, f"U{uid}")
    q.start()
    q.confirm(1)
    clock.t += 300
    q.enter(2, is_admin=False)  # U1 насправді ще не здавав
    returned, slot = q.sign_up(1, "U1")
    assert returned and slot is q.find_user(1)
    assert call_order(q) == ["U2", "U1", "U3"]  # одразу після поточного
    assert q.find_user(1).skips == 0


def test_enter_when_called_early_defers_without_penalty(clock):
    q = make_queue()
    q.join(1, "A")
    q.join(2, "B")
    q.join(3, "C")
    q.start()
    clock.t = at("10:26")
    q.finish(1, is_admin=False)  # B (10:30) кличуть о 10:26 — раніше за розклад
    clock.t = at("10:27") + 30
    outcome, previous = q.enter(3, is_admin=False)  # C під дверима і заходить
    assert outcome is Entered.DEFERRED
    assert previous.name == "B" and previous.status is SlotStatus.WAITING
    assert call_order(q) == ["C", "B"]
    assert q.find_user(2).skips == 0  # без штрафу
    assert q.current.confirmed
    assert q.pop_turn_change().deferred.name == "B"


def test_enter_by_current_just_confirms(clock):
    q = make_queue()
    q.join(1, "A")
    q.start()
    assert q.enter(1, is_admin=False)[0] is Entered.CONFIRMED
    assert q.current.confirmed


def test_admin_can_enter_on_behalf_without_grace(clock):
    q = make_queue()
    q.join(1, "A")
    q.join(2, "B")
    q.start()
    outcome, _ = q.enter(ADMIN, is_admin=True)
    assert outcome is Entered.SKIPPED
    assert q.current.name == "B"


# ---------------------------------------------------------------- нагадування і середній час


def test_nudge_when_turn_takes_too_long(clock):
    q = make_queue()
    q.join(1, "A")
    q.join(2, "B")
    q.start()
    q.confirm(1)
    q.pop_turn_change()
    # Даних ще немає → середня здача = крок (5 хв) → нагадуємо через max(10, 2×5) хв.
    assert q.nudge_at == at("10:35")
    clock.t = at("10:34")
    q.tick()
    assert q.pop_turn_change() is None
    clock.t = at("10:35")
    q.tick()
    change = q.pop_turn_change()
    assert change.nudge.name == "A"
    assert not change.changed  # поточний той самий — «твоя черга» не повторюємо
    q.tick()
    assert q.pop_turn_change() is None  # нагадуємо лише раз
    assert q.nudge_at is None


def test_avg_turn_from_finished(clock):
    q = make_queue()
    for uid in (1, 2, 3):
        q.join(uid, f"U{uid}")
    q.start()
    clock.t += 240
    q.finish(1, is_admin=False)  # 4 хв
    clock.t += 360
    q.finish(2, is_admin=False)  # 6 хв
    assert q.avg_turn() == 300
    q.confirm(3)
    assert q.nudge_at == q.current.called_at + 600  # max(10 хв, 2 × 5 хв)


def test_people_ahead():
    q = make_queue()
    for uid in (1, 2, 3, 4):
        q.join(uid, f"U{uid}")
    assert q.people_ahead(q.find_user(4)) == 3
    q.start()
    q.finish(1, is_admin=False)
    assert q.people_ahead(q.find_user(4)) == 2  # U2 здає, U3 чекає


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
    with pytest.raises(QueueError, match="у кінці черги"):
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
