import pytest

from bot.parsing import MAX_SLOTS, normalize_time, parse_new_args
from bot.queue_logic import QueueError


def test_basic():
    args = parse_new_args("Лаба 3 10:25-10:45")
    assert args.title == "Лаба 3"
    assert args.step == 5
    assert args.slot_times() == ["10:25", "10:30", "10:35", "10:40"]


def test_step_and_breaks():
    args = parse_new_args("ООП 10:00-11:00 10хв перерва 10:20-10:40")
    assert args.step == 10
    assert args.slot_times() == ["10:00", "10:10", "10:40", "10:50"]


def test_multiple_breaks_and_dashes_with_spaces():
    args = parse_new_args("X 9:00 – 10:00 15 перерва 9:15-9:30 9:45-10:00")
    assert args.slot_times() == ["09:00", "09:30"]


def test_screenshot_like_schedule():
    args = parse_new_args("Лаба 3 10:25-13:05 5 перерва 12:00-12:20")
    times = args.slot_times()
    assert times[0] == "10:25"
    assert "11:55" in times and "12:00" not in times and "12:15" not in times
    assert times[-1] == "13:00"


@pytest.mark.parametrize(
    "text, error",
    [
        ("", "проміжку"),
        ("10:00-11:00", "назву"),
        ("X 11:00-10:00", "кінець раніше"),
        ("X 10:00-25:00", "не існує"),
        ("X 10:00-11:00 abc", "Не зрозумів"),
        ("X 10:00-11:00 0", "від 1 до 180"),
        ("X 10:00-10:05 перерва 10:00-10:05", "0 слотів"),
        ("X 08:00-20:00 5", str(MAX_SLOTS)),
    ],
)
def test_errors(text, error):
    with pytest.raises(QueueError, match=error):
        parse_new_args(text).slot_times()


def test_normalize_time():
    assert normalize_time("9:05") == "09:05"
    with pytest.raises(QueueError):
        normalize_time("9.05")
