import pytest

from bot.queue_logic import MAX_PEOPLE, Entered, Queue, QueueError, SlotStatus

ADMIN = 999


def order(q: Queue) -> list[str]:
    """Імена в порядку виклику: поточний, потім ті, хто чекає."""
    return [s.name for s in ([q.current] if q.current else []) + q.waiting]


# ---------------------------------------------------------------- запис


def test_join_goes_to_the_end(make):
    q = make(2)
    q.join(3, "U3")
    assert order(q) == ["U1", "U2", "U3"]
    with pytest.raises(QueueError, match="місце 3"):
        q.join(3, "U3")


def test_queue_has_a_limit(make):
    q = make(MAX_PEOPLE, places=MAX_PEOPLE)
    with pytest.raises(QueueError, match="не лишилось"):
        q.join(1000, "X")


def test_leave(make):
    q = make(3)
    q.leave(2)
    assert order(q) == ["U1", "U3"]
    with pytest.raises(QueueError, match="немає в цій черзі"):
        q.leave(2)


def test_closed_queue_rejects_everything(make):
    q = make(1)
    q.close()
    for action in (lambda: q.join(2, "B"), lambda: q.sign_up(1, "U1"), q.start, q.skip):
        with pytest.raises(QueueError, match="закрита"):
            action()


# ---------------------------------------------------------------- хід черги


def test_start_requires_people(make):
    with pytest.raises(QueueError, match="ніхто не записався"):
        make().start()


def test_full_flow(make):
    q = make(2, start=True)
    assert (q.current.name, q.upcoming.name) == ("U1", "U2")
    q.finish(1, is_admin=False)
    assert q.current.name == "U2"
    q.finish(2, is_admin=False)
    assert q.current is None
    q.join(3, "U3")  # дописався після всіх — кличемо одразу
    assert q.current.name == "U3"
    with pytest.raises(QueueError, match="вже запущена"):
        q.start()


def test_only_current_or_admin_can_finish(make):
    q = make(2)
    with pytest.raises(QueueError, match="не запущена"):
        q.finish(1, is_admin=False)
    q.start()
    with pytest.raises(QueueError, match="Зараз здає U1"):
        q.finish(2, is_admin=False)
    q.finish(ADMIN, is_admin=True)
    assert q.current.name == "U2"


def test_admin_marks_by_place(make):
    q = make(3, start=True)
    with pytest.raises(QueueError, match="тільки адмін"):
        q.finish(1, False, 2)
    q.finish(ADMIN, True, 2)
    assert order(q) == ["U1", "U3"]
    for number, error in ((2, "вже відмічено"), (0, "нікого"), (9, "нікого")):
        with pytest.raises(QueueError, match=error):
            q.finish(ADMIN, True, number)


def test_current_leaving_calls_next_and_done_cannot_leave(make):
    q = make(3, start=True)
    q.leave(1)
    assert q.current.name == "U2"
    q.finish(2, is_admin=False)
    with pytest.raises(QueueError, match="вже відмічено"):
        q.leave(2)


def test_add_and_remove(make):
    q = make(1, start=True)
    q.add("Тезка")
    q.add("Тезка")  # двоє з однаковим ім'ям — різні люди
    q.remove(3)
    assert order(q) == ["U1", "Тезка"]
    q.remove(1)
    assert q.current.name == "Тезка"
    assert q.confirm_deadline is None  # без Telegram «Йду» не натиснеш


def test_later(make, clock):
    q = make(3)
    q.later(1)
    assert order(q) == ["U2", "U1", "U3"]
    q.start()
    q.confirm(2)
    clock.t += 100
    q.later(2)  # поточний передає чергу
    assert order(q) == ["U1", "U2", "U3"]
    assert not q.current.confirmed and q.confirm_deadline == clock.t + 180
    with pytest.raises(QueueError, match="у кінці"):
        q.later(3)


# ---------------------------------------------------------------- пропуски і повернення


def test_return_penalties(make):
    q = make(7, start=True)
    q.skip()
    q.here(1)  # 1-й пропуск — вперед 1 людина
    assert order(q) == ["U2", "U3", "U1", "U4", "U5", "U6", "U7"]
    q.finish(2, False)
    q.finish(3, False)
    q.skip()
    q.here(1)  # 2-й — 2 людини
    assert order(q) == ["U4", "U5", "U6", "U1", "U7"]
    for uid in (4, 5, 6):
        q.finish(uid, False)
    q.join(8, "U8")
    q.skip()
    q.here(1)  # далі — в кінець
    assert order(q) == ["U7", "U8", "U1"]


def test_return_ignores_person_done_out_of_order(make):
    q = make(4, start=True)
    q.finish(ADMIN, True, 3)
    q.skip()
    q.here(1)
    assert order(q) == ["U2", "U4", "U1"]


def test_return_when_nobody_waits(make):
    q = make(1, start=True)
    q.skip()
    q.here(1)
    assert q.current.name == "U1"


def test_here_only_for_skipped(make):
    with pytest.raises(QueueError, match="не пропускали"):
        make(1).here(1)


def test_sign_up_returns_skipped_person(make):
    q = make(2, start=True)
    q.skip()
    assert q.sign_up(1, "U1")[0] is True
    assert q.sign_up(3, "U3")[0] is False


# ---------------------------------------------------------------- автопропуск


def test_auto_skip(make, clock):
    q = make(2, start=True)
    clock.t += 179
    assert q.expire() is None
    clock.t += 1
    assert q.expire().name == "U1"
    assert q.current.name == "U2"


def test_confirm_or_zero_wait_disables_auto_skip(make, clock):
    q = make(2, start=True)
    with pytest.raises(QueueError, match="не твоя черга"):
        q.confirm(2)
    q.confirm(1)
    assert q.confirm_deadline is None
    assert make(1, start=True, wait_minutes=0).confirm_deadline is None


# ---------------------------------------------------------------- «🚪 Заходжу»


def test_enter_when_previous_did_not_come(make, clock):
    q = make(3, start=True)
    with pytest.raises(QueueError, match="дай ще"):
        q.enter(2, False)
    clock.t += 60
    with pytest.raises(QueueError, match="тільки наступна"):
        q.enter(3, False)
    outcome, previous = q.enter(2, False)
    assert outcome is Entered.SKIPPED and previous.status is SlotStatus.SKIPPED
    assert q.current.name == "U2" and q.current.confirmed


def test_enter_when_previous_forgot_done(make, clock):
    q = make(3, start=True)
    q.confirm(1)
    clock.t += 300
    assert q.enter(2, False)[0] is Entered.CLOSED
    change = q.pop_turn_change()
    assert (change.closed.name, change.upcoming.name) == ("U1", "U3")
    q.sign_up(1, "U1")  # насправді ще не здавав — повертається без штрафу
    assert order(q) == ["U2", "U1", "U3"]


def test_enter_by_current_and_admin(make):
    q = make(2, start=True)
    assert q.enter(1, False)[0] is Entered.CONFIRMED
    assert q.enter(ADMIN, True)[0] is Entered.CLOSED


# ---------------------------------------------------------------- перерви


def test_break_after_every_n(make, clock):
    q = make(6, start=True, break_every=2)
    q.finish(1, False)
    assert q.current.name == "U2"  # ще рано
    q.finish(2, False)
    assert q.on_break and q.current is None and q.next_timer_at is None
    clock.t += 3600
    q.tick()
    q.join(7, "U7")
    assert q.current is None  # сама перерва не закінчується
    with pytest.raises(QueueError, match="перерва"):
        q.finish(4, False)
    q.finish(3, False)  # наступний каже «Готово» — перерва закінчилась
    assert q.current.name == "U3" and q.current.confirmed and not q.on_break
    q.finish(3, False)
    q.finish(4, False)
    assert q.on_break  # лік почався заново
    q.finish(ADMIN, True)
    assert q.current.name == "U5" and not q.current.confirmed


def test_enter_ends_break(make):
    q = make(2, start=True, break_every=1)
    q.finish(1, False)
    q.enter(2, False)
    assert q.current.name == "U2" and q.current.confirmed


def test_go_ends_break_early(make):
    q = make(2, start=True, break_every=1)
    q.finish(1, False)
    q.start()
    assert q.current.name == "U2" and not q.on_break


def test_skipped_do_not_count_and_no_break_after_last(make):
    q = make(3, start=True, break_every=2)
    q.skip()
    q.finish(2, False)
    assert q.current.name == "U3"
    q.finish(3, False)
    assert not q.on_break  # далі нікого — перерва не потрібна
    q.join(4, "U4")
    assert q.current.name == "U4"


def test_enter_postpones_break(make, clock):
    q = make(3, start=True, break_every=1)
    q.confirm(1)
    clock.t += 300
    q.enter(2, False)  # U2 вже в кабінеті
    assert q.current.name == "U2" and q.current.confirmed
    q.finish(2, False)
    assert q.on_break


def test_break_with_nobody_left(make):
    q = make(2, start=True, break_every=1)
    q.finish(1, False)
    q.leave(2)
    q.join(3, "U3")
    assert q.on_break
    q.finish(3, False)
    assert q.current.name == "U3"


# ---------------------------------------------------------------- нагадування, очікування


def test_nudge_once(make, clock):
    q = make(2, start=True)
    q.confirm(1)
    q.pop_turn_change()
    clock.t += 599
    q.tick()
    assert q.pop_turn_change() is None
    clock.t += 1
    q.tick()
    change = q.pop_turn_change()
    assert change.nudge.name == "U1" and not change.changed
    q.tick()
    assert q.pop_turn_change() is None and q.next_timer_at is None


def test_wait_estimate(make, clock):
    q = make(4, start=True)
    assert q.wait_seconds(q.find_user(4)) == 3 * 300
    clock.t += 240
    q.finish(1, False)
    clock.t += 360
    q.finish(2, False)
    assert q.avg_turn() == 300
    assert q.wait_seconds(q.find_user(4)) == 300


def test_pop_turn_change_reports_each_change_once(make):
    q = make(2)
    assert q.pop_turn_change() is None
    q.start()
    assert q.pop_turn_change().current.name == "U1"
    assert q.pop_turn_change() is None
    q.skip()
    change = q.pop_turn_change()
    assert (change.skipped.name, change.current.name) == ("U1", "U2")
    q.finish(2, False)
    assert q.pop_turn_change().current is None
    assert q.pop_turn_change() is None


# ---------------------------------------------------------------- місця


def places(q: Queue) -> list[int]:
    return [s.place for s in q.slots]


def test_pick_place_and_skip_gaps(make):
    q = make(places=30)
    q.join(1, "A", 15)
    q.join(2, "B", 10)
    q.join(3, "C")  # без номера — найближче вільне
    assert (order(q), places(q)) == (["C", "B", "A"], [1, 10, 15])
    for place, error in ((10, "зайняте"), (0, "від 1 до 30"), (31, "від 1 до 30")):
        with pytest.raises(QueueError, match=error):
            q.join(4, "D", place)
    q.start()
    q.finish(3, False)
    assert q.current.name == "B"  # місця 2–9 порожні — пропустили
    with pytest.raises(QueueError, match="минуло"):
        q.join(4, "D", 5)
    q.join(4, "D")
    assert places(q) == [1, 10, 11, 15]


def test_move_to_another_place(make):
    q = make(3)
    q.join(1, "U1", 20)
    assert (order(q), places(q)) == (["U2", "U3", "U1"], [2, 3, 20])
    q.start()
    with pytest.raises(QueueError, match="Пізніше"):
        q.join(2, "U2", 25)
    q.skip()
    q.here(2)
    with pytest.raises(QueueError, match="Після пропуску"):
        q.join(2, "U2", 25)


def test_latecomer_gets_passed_gap_but_goes_last(make):
    q = make(places=3)
    q.join(1, "A", 2)
    q.join(2, "B", 3)
    q.start()
    q.finish(1, False)
    q.join(3, "C")
    assert order(q) == ["B", "C"] and q.find_user(3).place == 1
    with pytest.raises(QueueError, match="не лишилось"):
        q.join(4, "D")


def test_done_out_of_order_does_not_close_places(make):
    q = make(places=30)
    q.join(1, "A", 1)
    q.join(2, "B", 20)
    q.start()
    q.finish(ADMIN, True, 20)
    assert q.join(3, "C", 5).place == 5


def test_later_swaps_places(make):
    q = make(places=30)
    q.join(1, "A", 5)
    q.join(2, "B", 9)
    q.later(1)
    assert (order(q), places(q)) == (["B", "A"], [5, 9])


def test_join_after_returned_person(make):
    q = make(places=30)
    for uid, place in ((1, 1), (2, 2), (3, 8)):
        q.join(uid, f"U{uid}", place)
    q.start()
    q.skip()
    q.here(1)  # U2 | U3(8) U1(1)
    q.join(4, "U4", 5)
    q.join(5, "U5", 10)
    assert order(q) == ["U2", "U4", "U3", "U1", "U5"]
