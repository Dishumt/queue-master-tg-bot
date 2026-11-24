"""Розбір тексту команд.

Приклад: `/new Лаба 3 перерва кожні 5 по 10 чекати 3`
  - «Лаба 3»                — назва (усе до слова «перерва» чи «чекати»)
  - «перерва кожні 5 по 10» — після кожних 5 здач перерва 10 хв
  - «чекати 3»              — скільки хвилин чекати на «🏃 Йду» (0 — без автопропуску)
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from bot.queue_logic import DEFAULT_WAIT_MINUTES, MAX_PEOPLE, QueueError

MAX_NAME_LEN = 40

_TIME_RE = re.compile(r"\d{1,2}:\d{2}")
_NUMBER_RE = re.compile(r"^(\d{1,3})(?:хв|хвилин|m|min)?$", re.IGNORECASE)
_BREAK_WORDS = {"перерва", "перерви", "break"}
_WAIT_WORDS = {"чекати", "очікування", "wait"}
# Слова для зручності: «перерва кожні 5 учасників по 10 хв».
_FILLERS = {
    "кожні", "кожних", "через", "після", "по", "на", "хв", "хвилин",
    "учасників", "учасники", "людей", "людини", "студентів", "студенти",
}  # fmt: skip

NEW_USAGE = (
    "Формат: <code>/new Назва перерва кожні 5 по 10 чекати 3</code>\n"
    "• перерва — після кожних 5 здач 10 хв (необов'язково)\n"
    f"• чекати — скільки хвилин чекати на «🏃 Йду» (за замовчуванням {DEFAULT_WAIT_MINUTES}, 0 — без автопропуску)"
)


def clean_name(text: str) -> str:
    """Прибирає зайві пробіли й обрізає занадто довгі імена."""
    name = " ".join(text.split())
    if not name:
        raise QueueError("Ім'я не може бути порожнім.")
    return name[:MAX_NAME_LEN]


def parse_number(text: str) -> int:
    """Номер людини у списку: '3' -> 3."""
    text = text.strip()
    if not (text.isascii() and text.isdigit()):
        raise QueueError(f"«{text}» — не номер. Вкажи номер зі списку, наприклад 3")
    return int(text)


def parse_wait(text: str) -> int:
    """'3' або '3хв' -> 3. Дозволено 0–60 (0 — без автопропуску)."""
    m = _NUMBER_RE.match(text.strip())
    minutes = int(m.group(1)) if m else -1
    if not 0 <= minutes <= 60:
        raise QueueError("Час очікування — від 0 до 60 хвилин (0 — вимкнути автопропуск).")
    return minutes


def parse_break(text: str) -> tuple[int, int]:
    """'кожні 5 по 10' або '5 10' -> (5, 10). '0' -> (0, 0), тобто без перерв."""
    numbers = []
    for token in text.lower().split():
        if m := _NUMBER_RE.match(token):
            numbers.append(int(m.group(1)))
        elif token not in _FILLERS:
            raise QueueError(f"Не зрозумів «{token}». Приклад: перерва кожні 5 по 10")
    if numbers == [0]:
        return 0, 0
    if len(numbers) != 2:
        raise QueueError("Для перерви потрібні два числа, наприклад: перерва кожні 5 по 10")
    every, minutes = numbers
    if not 1 <= every <= MAX_PEOPLE or not 1 <= minutes <= 180:
        raise QueueError(f"Перерва: від 1 до {MAX_PEOPLE} людей і від 1 до 180 хвилин.")
    return every, minutes


@dataclass
class NewQueueArgs:
    title: str
    break_every: int = 0
    break_minutes: int = 0
    wait: int = DEFAULT_WAIT_MINUTES


def parse_new_args(text: str) -> NewQueueArgs:
    # Розкладаємо слова по трьох купках: назва, перерва, очікування.
    parts: dict[str, list[str]] = {"title": []}
    mode = "title"
    for token in (text or "").split():
        word = token.lower()
        if word in _BREAK_WORDS:
            mode = "break"
            parts.setdefault(mode, [])
        elif word in _WAIT_WORDS:
            mode = "wait"
            parts.setdefault(mode, [])
        else:
            parts[mode].append(token)

    title = " ".join(parts["title"])
    if not title:
        raise QueueError("Додай назву черги.\n\n" + NEW_USAGE)
    if any(_TIME_RE.search(t) for tokens in parts.values() for t in tokens):
        raise QueueError("Час більше не потрібен — черга йде за номерами.\n\n" + NEW_USAGE)

    args = NewQueueArgs(title=title[:100])
    if "break" in parts:
        args.break_every, args.break_minutes = parse_break(" ".join(parts["break"]))
    if "wait" in parts:
        args.wait = parse_wait(" ".join(parts["wait"]))
    return args
