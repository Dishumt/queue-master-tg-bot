"""Чиста логіка черги — без Telegram і без бази даних.

Тут описано, ЩО таке черга і які з нею можна робити дії. Цей модуль
нічого не знає про повідомлення, кнопки чи SQLite, тому його легко
тестувати (див. tests/test_queue_logic.py).

Головне правило черги: `slots` — це список у порядку виклику, а «наступний» —
перший слот у списку зі статусом WAITING. Спочатку порядок збігається з часом
слотів, але пропущений студент, який повернувся, переїжджає далі по списку
(разом зі своїм часом-міткою). Після кожної дії викликається `_advance()`,
яка за потреби призначає нового поточного.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import StrEnum

DEFAULT_WAIT_MINUTES = 3  # скільки чекати на «🏃 Йду» до автопропуску
ABSENT_GRACE_SECONDS = 60  # через скільки наступний може натиснути «Його немає»
# Скільки людей пропустити вперед, коли пропущений повертається:
# після 1-го пропуску — 1, після 2-го — 2, далі — в кінець черги.
RETURN_PENALTY = {1: 1, 2: 2}


def now() -> float:
    """Поточний час. Окрема функція — щоб у тестах можна було «перемотати» час."""
    return time.time()


class SlotStatus(StrEnum):
    EMPTY = "empty"  # у слоті нікого немає
    WAITING = "waiting"  # записаний, чекає своєї черги
    CURRENT = "current"  # зараз його кличуть / він здає
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
    time: str  # "10:25" — мітка, на який час людина записалась
    user_id: int | None = None  # None — людина без Telegram (додана адміном)
    name: str | None = None
    status: SlotStatus = SlotStatus.EMPTY
    skips: int = 0  # скільки разів людину вже пропускали
    called_at: float | None = None  # коли покликали (для автопропуску)
    confirmed: bool = False  # натиснув «🏃 Йду»

    @property
    def is_free(self) -> bool:
        return self.status is SlotStatus.EMPTY

    def assign(self, user_id: int | None, name: str) -> None:
        self.user_id = user_id
        self.name = name
        self.status = SlotStatus.WAITING
        self.skips = 0
        self.called_at = None
        self.confirmed = False

    def clear(self) -> None:
        self.assign(None, "")
        self.name = None
        self.status = SlotStatus.EMPTY

    def call(self) -> None:
        """Зробити поточним: таймер підтвердження стартує заново."""
        self.status = SlotStatus.CURRENT
        self.called_at = now()
        self.confirmed = False


@dataclass(frozen=True)
class TurnChange:
    """Що сказати в чаті після дії."""

    current: Slot | None  # кого кликати (None — більше нікого)
    upcoming: Slot | None  # кого попередити «готуйся»
    skipped: Slot | None = None  # кого щойно пропустили (якщо пропустили)


@dataclass
class Queue:
    chat_id: int
    thread_id: int  # 0, якщо в групі немає топіків
    title: str
    step: int  # крок між слотами у хвилинах (потрібен, щоб малювати перерви)
    slots: list[Slot]
    status: QueueStatus = QueueStatus.OPEN
    wait_minutes: int = DEFAULT_WAIT_MINUTES  # 0 — автопропуск вимкнено
    message_id: int | None = None  # id закріпленого повідомлення
    id: int | None = None  # id у базі даних
    # Хто був поточним до останньої операції — щоб зрозуміти, кого кликати.
    _last_called: tuple[str, int | None] | None = field(default=None, repr=False)
    _last_skipped: Slot | None = field(default=None, repr=False)

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

    def _require_user(self, user_id: int) -> Slot:
        slot = self.find_user(user_id)
        if slot is None:
            raise QueueError("Тебе немає в цій черзі.")
        return slot

    # ------------------------------------------------------------------
    # Головний механізм: хто зараз здає
    # ------------------------------------------------------------------

    def _advance(self) -> None:
        """Якщо черга запущена і зараз ніхто не здає — кличемо наступного."""
        if self.status is not QueueStatus.RUNNING or self.current is not None:
            return
        nxt = self.upcoming
        if nxt is not None:
            nxt.call()

    def pop_turn_change(self) -> TurnChange | None:
        """Чи змінився поточний з минулого виклику.

        Сервіс викликає це після кожної операції:
          - None                       — нічого не змінилось, мовчимо;
          - TurnChange(current=слот)   — тегнути «твоя черга»;
          - TurnChange(current=None)   — кликати більше нікого.
        """
        cur = self.current
        key = (cur.time, cur.user_id) if cur else None
        skipped, self._last_skipped = self._last_skipped, None
        if key == self._last_called and skipped is None:
            return None
        self._last_called = key
        return TurnChange(
            current=cur, upcoming=self.upcoming if cur else None, skipped=skipped
        )

    @property
    def is_finished(self) -> bool:
        """Черга запущена, але кликати більше нікого."""
        return self.status is QueueStatus.RUNNING and self.current is None

    # ------------------------------------------------------------------
    # Підтвердження і автопропуск
    # ------------------------------------------------------------------

    @property
    def confirm_deadline(self) -> float | None:
        """Коли пропустити поточного, якщо він не натисне «Йду» (None — не треба)."""
        cur = self.current
        if (
            self.status is not QueueStatus.RUNNING
            or cur is None
            or cur.confirmed
            or cur.user_id is None  # людина без Telegram не натисне «Йду»
            or cur.called_at is None
            or self.wait_minutes <= 0
        ):
            return None
        return cur.called_at + self.wait_minutes * 60

    def confirm(self, user_id: int) -> Slot:
        """Поточний натиснув «🏃 Йду»."""
        self._ensure_not_closed()
        cur = self.current
        if cur is None or cur.user_id != user_id:
            raise QueueError("Зараз не твоя черга 🙂")
        cur.confirmed = True
        return cur

    def expire(self) -> Slot | None:
        """Автопропуск: якщо час на підтвердження вийшов — пропустити поточного."""
        deadline = self.confirm_deadline
        if deadline is None or now() < deadline:
            return None
        return self.skip()

    def absent(self, user_id: int, is_admin: bool) -> Slot:
        """Кнопка «🙅 Його немає»."""
        self._ensure_not_closed()
        cur = self.current
        if cur is None:
            raise QueueError("Зараз нікого не кличуть.")
        if not is_admin:
            if cur.confirmed:
                raise QueueError(f"{cur.name} уже підтвердив, що йде — зачекай.")
            upcoming = self.upcoming
            if upcoming is None or upcoming.user_id != user_id:
                raise QueueError("Відмітити відсутність може наступний у черзі або адмін.")
            waited = now() - (cur.called_at or 0)
            if waited < ABSENT_GRACE_SECONDS:
                left = int(ABSENT_GRACE_SECONDS - waited) + 1
                raise QueueError(f"Дай {cur.name} ще {left} с, щоб відгукнутись 🙂")
        return self.skip()

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
        for attr in ("user_id", "name", "skips"):
            a, b = getattr(mine, attr), getattr(other, attr)
            setattr(mine, attr, b)
            setattr(other, attr, a)
        if mine.status is SlotStatus.CURRENT:
            mine.call()  # кличемо іншу людину — таймер з нуля
        return other, mine

    def here(self, user_id: int) -> Slot:
        """Пропущений студент повернувся.

        Він стає в чергу не одразу, а пропускає вперед кількох людей:
        після 1-го пропуску — 1, після 2-го — 2, далі — в кінець
        (див. RETURN_PENALTY). Слот переїжджає в списку разом із часом-міткою.
        """
        self._ensure_not_closed()
        slot = self._require_user(user_id)
        if slot.status is not SlotStatus.SKIPPED:
            raise QueueError("Тебе не пропускали — просто чекай своєї черги.")

        self.slots.remove(slot)
        slot.status = SlotStatus.WAITING
        cursor = self._cursor()
        future = range(cursor, len(self.slots))
        waiting = [i for i in future if self.slots[i].status is SlotStatus.WAITING]
        ahead = RETURN_PENALTY.get(slot.skips)

        if ahead is None:  # третій і далі пропуск — у кінець
            occupied = [i for i in future if not self.slots[i].is_free]
            insert_at = occupied[-1] + 1 if occupied else cursor
        elif waiting:
            insert_at = waiting[min(ahead, len(waiting)) - 1] + 1
        else:
            insert_at = cursor
        self.slots.insert(insert_at, slot)
        self._advance()
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
        slot.skips += 1
        slot.called_at = None
        self._last_skipped = slot
        self._advance()
        return slot

    def set_wait(self, minutes: int) -> None:
        self._ensure_not_closed()
        if not 0 <= minutes <= 60:
            raise QueueError("Час очікування — від 0 до 60 хвилин (0 — вимкнути автопропуск).")
        self.wait_minutes = minutes

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
