"""Розбір тексту команд: час, проміжки, аргументи `/new`.

Приклад: `/new Лаба 3 10:25-13:00 5 перерва 12:00-12:20`
  - «Лаба 3»       — назва (усе до першого проміжку часу)
  - «10:25-13:00»  — слоти від 10:25 до 13:00 (13:00 не включно)
  - «5»            — крок у хвилинах (за замовчуванням 5)
  - «перерва ...»  — один чи кілька проміжків без слотів (кінець не включно)
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from bot.queue_logic import QueueError

MAX_SLOTS = 60  # щоб закріплене повідомлення влізло в ліміт Telegram (4096 символів)
MAX_NAME_LEN = 40
DEFAULT_STEP = 5

_TIME = r"\d{1,2}:\d{2}"
_TIME_RE = re.compile(rf"^({_TIME})$")
_RANGE_RE = re.compile(rf"^({_TIME})-({_TIME})$")
_STEP_RE = re.compile(r"^(\d{1,3})(?:хв|хвилин|m|min)?$", re.IGNORECASE)
_DASHES_RE = re.compile(rf"({_TIME})\s*[-–—]\s*({_TIME})")
_BREAK_WORDS = {"перерва", "перерви", "break", "breaks"}


def parse_time(text: str) -> int:
    """'9:05' -> 545 (хвилин від півночі)."""
    m = _TIME_RE.match(text.strip())
    if not m:
        raise QueueError(f"«{text}» — не схоже на час. Пиши так: 10:25")
    hours, minutes = map(int, m.group(1).split(":"))
    if hours > 23 or minutes > 59:
        raise QueueError(f"Часу {text} не існує.")
    return hours * 60 + minutes


def fmt_time(minutes: int) -> str:
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def normalize_time(text: str) -> str:
    """'9:05' -> '09:05', щоб однаково порівнювати з часом слотів."""
    return fmt_time(parse_time(text))


def clean_name(text: str) -> str:
    """Прибирає зайві пробіли й обрізає занадто довгі імена."""
    name = " ".join(text.split())
    if not name:
        raise QueueError("Ім'я не може бути порожнім.")
    return name[:MAX_NAME_LEN]


@dataclass
class NewQueueArgs:
    title: str
    start: int
    end: int
    step: int = DEFAULT_STEP
    breaks: list[tuple[int, int]] = field(default_factory=list)

    def slot_times(self) -> list[str]:
        times = []
        t = self.start
        while t < self.end:
            if not any(b_start <= t < b_end for b_start, b_end in self.breaks):
                times.append(fmt_time(t))
            t += self.step
        if not times:
            raise QueueError("Вийшло 0 слотів — перевір проміжок і перерви.")
        if len(times) > MAX_SLOTS:
            raise QueueError(
                f"Вийшло {len(times)} слотів, а максимум — {MAX_SLOTS}. "
                "Збільш крок або скороти проміжок."
            )
        return times


def _parse_range(token: str) -> tuple[int, int] | None:
    m = _RANGE_RE.match(token)
    if not m:
        return None
    start, end = parse_time(m.group(1)), parse_time(m.group(2))
    if end <= start:
        raise QueueError(f"У проміжку {token} кінець раніше за початок.")
    return start, end


NEW_USAGE = (
    "Формат: <code>/new Назва 10:25-13:00 5 перерва 12:00-12:20</code>\n"
    "• проміжок — коли приймають (кінець не включно)\n"
    "• число — крок у хвилинах (необов'язково, за замовчуванням 5)\n"
    "• перерва — проміжки без слотів (необов'язково)"
)


def parse_new_args(text: str) -> NewQueueArgs:
    text = _DASHES_RE.sub(r"\1-\2", text or "")
    tokens = text.split()

    range_at = next((i for i, t in enumerate(tokens) if _RANGE_RE.match(t)), None)
    if range_at is None:
        raise QueueError("Не бачу проміжку часу.\n\n" + NEW_USAGE)
    title = " ".join(tokens[:range_at])
    if not title:
        raise QueueError("Додай назву черги перед часом.\n\n" + NEW_USAGE)

    start, end = _parse_range(tokens[range_at])  # type: ignore[misc]
    args = NewQueueArgs(title=title[:100], start=start, end=end)

    step_set = False
    in_breaks = False
    for token in tokens[range_at + 1 :]:
        if token.lower() in _BREAK_WORDS:
            in_breaks = True
        elif in_breaks and (rng := _parse_range(token)):
            args.breaks.append(rng)
        elif not step_set and not in_breaks and (m := _STEP_RE.match(token)):
            args.step = int(m.group(1))
            if not 1 <= args.step <= 180:
                raise QueueError("Крок має бути від 1 до 180 хвилин.")
            step_set = True
        else:
            raise QueueError(f"Не зрозумів «{token}».\n\n" + NEW_USAGE)
    return args
