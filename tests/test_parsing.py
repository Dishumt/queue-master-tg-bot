import pytest

from bot.parsing import NewQueueArgs, parse_break, parse_new_args, parse_number, parse_wait
from bot.queue_logic import QueueError


def test_new_args():
    assert parse_new_args("Лаба 3") == NewQueueArgs("Лаба 3")
    full = NewQueueArgs("Лаба 3", break_every=5, break_minutes=10, wait=2)
    for text in (
        "Лаба 3 перерва кожні 5 по 10 чекати 2",
        "Лаба 3 чекати 2 перерва 5 10",
        "Лаба 3 перерва кожні 5 учасників по 10 хв чекати 2хв",
    ):
        assert parse_new_args(text) == full


@pytest.mark.parametrize(
    "text, error",
    [
        ("", "назву"),
        ("перерва 5 10", "назву"),
        ("X 10:00-11:00", "Час більше не потрібен"),
        ("X перерва 12:00-12:20", "Час більше не потрібен"),
        ("X перерва", "два числа"),
        ("X перерва 5", "два числа"),
        ("X перерва 0 10", "від 1 до"),
        ("X перерва 5 999", "від 1 до"),
        ("X перерва щось", "Не зрозумів"),
        ("X чекати", "від 0 до 60"),
        ("X чекати 99", "від 0 до 60"),
    ],
)
def test_errors(text, error):
    with pytest.raises(QueueError, match=error):
        parse_new_args(text)


def test_small_parsers():
    assert parse_break("0") == (0, 0)
    assert parse_wait("3хв") == 3
    assert parse_number(" 12 ") == 12
    for bad in ("", "-1", "10:45", "²"):
        with pytest.raises(QueueError, match="не номер"):
            parse_number(bad)
