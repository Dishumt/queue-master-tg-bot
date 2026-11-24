import pytest

from bot.parsing import NewQueueArgs, parse_break, parse_new_args, parse_number, parse_wait
from bot.queue_logic import QueueError


def test_new_args():
    assert parse_new_args("Лаба 3") == NewQueueArgs("Лаба 3")
    full = NewQueueArgs("Лаба 3", break_every=5, wait=2)
    assert parse_new_args("Лаба 3 місць 25 перерва 5").places == 25
    for text in (
        "Лаба 3 перерва кожні 5 чекати 2",
        "Лаба 3 чекати 2 перерва 5",
        "Лаба 3 перерва кожні 5 учасників чекати 2хв",
    ):
        assert parse_new_args(text) == full


@pytest.mark.parametrize(
    "text, error",
    [
        ("", "назву"),
        ("перерва 5", "назву"),
        ("X 10:00-11:00", "Час більше не потрібен"),
        ("X перерва 12:00-12:20", "Час більше не потрібен"),
        ("X перерва", "одне число"),
        ("X перерва 5 10", "одне число"),
        ("X перерва 999", "від 1 до"),
        ("X перерва щось", "Не зрозумів"),
        ("X місць", "не номер"),
        ("X місць 99", "від 1 до 50"),
        ("X чекати", "від 0 до 60"),
        ("X чекати 99", "від 0 до 60"),
    ],
)
def test_errors(text, error):
    with pytest.raises(QueueError, match=error):
        parse_new_args(text)


def test_small_parsers():
    assert parse_break("0") == 0
    assert parse_wait("3хв") == 3
    assert parse_number(" 12 ") == 12
    for bad in ("", "-1", "10:45", "²"):
        with pytest.raises(QueueError, match="не номер"):
            parse_number(bad)
