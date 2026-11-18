"""Чиста логіка черги — без Telegram і без бази даних.

Тут описано, ЩО таке черга і які з нею можна робити дії. Цей модуль
нічого не знає про повідомлення, кнопки чи SQLite, тому його легко
тестувати (див. tests/test_queue_logic.py).

Головне правило черги: «наступний» — це перший за часом слот зі статусом
WAITING. Усі операції лише змінюють статуси слотів, а після кожної зміни
викликається `_advance()`, яка за потреби призначає нового поточного.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum


class SlotStatus(StrEnum):
    EMPTY = "empty"  # у слоті нікого немає
    WAITING = "waiting"  # записаний, чекає своєї черги
    CURRENT = "current"  # зараз здає
    DONE = "done"  # здав ✅
    SKIPPED = "skipped"  # не прийшов, коли кликали


class QueueStatus(StrEnum):
    OPEN = "open"  # запис іде, але черга ще не запущена
    RUNNING = "running"  # викладач приймає, бот кличе людей
    CLOSED = "closed"  # черга завершена


class QueueError(Exception):
    """Помилка, текст якої можна показати користувачу як є."""


@dataclass
class Slot:
    time: str  # "10:25"
    user_id: int | None = None  # None — людина без Telegram (додана адміном)
    name: str | None = None
    status: SlotStatus = SlotStatus.EMPTY

    @property
    def is_free(self) -> bool:
        return self.status is SlotStatus.EMPTY

    def assign(self, user_id: int | None, name: str) -> None:
        self.user_id = user_id
        self.name = name
        self.status = SlotStatus.WAITING

    def clear(self) -> None:
        self.user_id = None
        self.name = None
        self.status = SlotStatus.EMPTY


@dataclass
class Queue:
    chat_id: int
    thread_id: int  # 0, якщо в групі немає топіків
    title: str
    step: int  # крок між слотами у хвилинах (потрібен, щоб малювати перерви)
    slots: list[Slot]
    status: QueueStatus = QueueStatus.OPEN
    message_id: int | None = None  # id закріпленого повідомлення
    id: int | None = None  # id у базі даних
    # Хто був поточним до останньої операції — щоб зрозуміти, кого кликати.
    _last_called: tuple[int, int | None] | None = field(default=None, repr=False)

    # ------------------------------------------------------------------
    # Пошук
    # ------------------------------------------------------------------

    @property
    def current(self) -> Slot | None:
        return next((s for s in self.slots if s.status is SlotStatus.CURRENT), None)

    @property
    def upcoming(self) -> Slot | None:
        """Хто піде після поточного (його попереджаємо «готуйся»)."""
        return next((s for s in self.slots if s.status is SlotStatus.WAITING), None)

    def find_user(self, user_id: int) -> Slot | None:
        return next((s for s in self.slots if s.user_id == user_id), None)

    def find_time(self, time: str) -> Slot:
        for slot in self.slots:
            if slot.time == time:
                return slot
        raise QueueError(f"Слоту {time} немає в цій черзі.")

    def _index(self, slot: Slot) -> int:
        return next(i for i, s in enumerate(self.slots) if s is slot)

    def _cursor(self) -> int:
        """Індекс, з якого починаються «майбутні» слоти.

        Слоти до курсора вже минули: туди не можна записатися самому,
        інакше можна було б перестрибнути всю чергу.
        """
        if self.status is QueueStatus.OPEN:
            return 0
        passed = [
            i
            for i, s in enumerate(self.slots)
            if s.status in (SlotStatus.CURRENT, SlotStatus.DONE, SlotStatus.SKIPPED)
        ]
        return max(passed) + 1 if passed else 0

    def _ensure_not_closed(self) -> None:
        if self.status is QueueStatus.CLOSED:
            raise QueueError("Ця черга вже закрита.")

    # ------------------------------------------------------------------
    # Головний механізм: хто зараз здає
    # ------------------------------------------------------------------

    def _advance(self) -> None:
        """Якщо черга запущена і зараз ніхто не здає — кличемо наступного."""
        if self.status is not QueueStatus.RUNNING or self.current is not None:
            return
        nxt = self.upcoming
        if nxt is not None:
            nxt.status = SlotStatus.CURRENT

    def pop_new_current(self) -> Slot | None:
        """Повертає поточного, якщо він змінився з минулого виклику.

        Обробники викликають це після кожної операції: якщо повернувся слот —
        треба тегнути людину «твоя черга».
        """
        cur = self.current
        key = (self._index(cur), cur.user_id) if cur else None
        if key == self._last_called:
            return None
        self._last_called = key
        return cur

    @property
    def is_finished(self) -> bool:
        """Черга запущена, але кликати більше нікого."""
        return self.status is QueueStatus.RUNNING and self.current is None

    # ------------------------------------------------------------------
    # Дії студентів
    # ------------------------------------------------------------------

    def join(self, user_id: int, name: str, time: str | None = None) -> Slot:
        self._ensure_not_closed()
        existing = self.find_user(user_id)
        if existing is not None:
            raise QueueError(f"Ти вже записаний на {existing.time}.")

        cursor = self._cursor()
        if time is None:
            slot = next((s for s in self.slots[cursor:] if s.is_free), None)
            if slot is None:
                raise QueueError("Вільних слотів не лишилось 😔")
        else:
            slot = self.find_time(time)
            if not slot.is_free:
                raise QueueError(f"Слот {time} уже зайнятий ({slot.name}).")
            if self._index(slot) < cursor:
                raise QueueError(f"Слот {time} уже минув — обери пізніший.")

        slot.assign(user_id, name)
        self._advance()
        return slot

    def leave(self, user_id: int) -> Slot:
        self._ensure_not_closed()
        slot = self._require_user(user_id)
        if slot.status is SlotStatus.DONE:
            raise QueueError("Ти вже здав — виписуватись не треба 🙂")
        freed = Slot(time=slot.time, user_id=slot.user_id, name=slot.name)
        slot.clear()
        self._advance()
        return freed

    def later(self, user_id: int) -> tuple[Slot, Slot]:
        """Поміняти себе місцями з наступним, хто чекає.

        Статуси лишаються на місці, міняються тільки люди. Тож якщо «пізніше»
        натиснув поточний — поточним стає той, з ким помінялись.
        Повертає (мій новий слот, слот людини, яка тепер іде раніше).
        """
        self._ensure_not_closed()
        mine = self._require_user(user_id)
        if mine.status not in (SlotStatus.WAITING, SlotStatus.CURRENT):
            raise QueueError("Посунутись можна, тільки поки ти в черзі.")
        start = self._index(mine) + 1
        other = next(
            (s for s in self.slots[start:] if s.status is SlotStatus.WAITING), None
        )
        if other is None:
            raise QueueError("Після тебе нікого немає — ти і так останній.")
        mine.user_id, other.user_id = other.user_id, mine.user_id
        mine.name, other.name = other.name, mine.name
        return other, mine

    def here(self, user_id: int) -> Slot:
        """Пропущений студент повернувся.

        Його слот раніше за поточний, тому щойно він знову WAITING —
        він автоматично стає наступним.
        """
        self._ensure_not_closed()
        slot = self._require_user(user_id)
        if slot.status is not SlotStatus.SKIPPED:
            raise QueueError("Тебе не пропускали — просто чекай своєї черги.")
        slot.status = SlotStatus.WAITING
        self._advance()
        return slot

    def _require_user(self, user_id: int) -> Slot:
        slot = self.find_user(user_id)
        if slot is None:
            raise QueueError("Тебе немає в цій черзі.")
        return slot

    # ------------------------------------------------------------------
    # «Закінчив»
    # ------------------------------------------------------------------

    def finish(self, user_id: int, is_admin: bool, time: str | None = None) -> Slot:
        """Позначити здачу.

        Без `time` — завершує поточного (може сам поточний або адмін).
        З `time` (тільки адмін) — позначає конкретний слот: буває, що
        викладач прийняв когось поза чергою.
        """
        self._ensure_not_closed()
        if time is not None:
            if not is_admin:
                raise QueueError("Відмічати конкретний слот може тільки адмін.")
            slot = self.find_time(time)
            if slot.status in (SlotStatus.EMPTY, SlotStatus.DONE):
                raise QueueError(f"У слоті {time} немає кого відмічати.")
        else:
            slot = self.current
            if slot is None:
                if self.status is QueueStatus.OPEN:
                    raise QueueError("Черга ще не запущена.")
                raise QueueError("Зараз ніхто не здає.")
            if slot.user_id != user_id and not is_admin:
                raise QueueError(f"Зараз здає {slot.name}, а не ти.")
        slot.status = SlotStatus.DONE
        self._advance()
        return slot

    # ------------------------------------------------------------------
    # Дії адміна
    # ------------------------------------------------------------------

    def start(self) -> None:
        self._ensure_not_closed()
        if self.status is QueueStatus.RUNNING:
            raise QueueError("Черга вже запущена.")
        if not any(s.status is SlotStatus.WAITING for s in self.slots):
            raise QueueError("У черзі ще ніхто не записався.")
        self.status = QueueStatus.RUNNING
        self._advance()

    def skip(self) -> Slot:
        self._ensure_not_closed()
        slot = self.current
        if slot is None:
            raise QueueError("Зараз нікого пропускати.")
        slot.status = SlotStatus.SKIPPED
        self._advance()
        return slot

    def add(self, time: str, name: str) -> Slot:
        """Вписати людину без Telegram у конкретний слот."""
        self._ensure_not_closed()
        slot = self.find_time(time)
        if not slot.is_free:
            raise QueueError(f"Слот {time} уже зайнятий ({slot.name}).")
        slot.assign(None, name)
        self._advance()
        return slot

    def remove(self, time: str) -> Slot:
        self._ensure_not_closed()
        slot = self.find_time(time)
        if slot.is_free:
            raise QueueError(f"Слот {time} і так порожній.")
        freed = Slot(time=slot.time, user_id=slot.user_id, name=slot.name)
        slot.clear()
        self._advance()
        return freed

    def close(self) -> None:
        self._ensure_not_closed()
        self.status = QueueStatus.CLOSED
