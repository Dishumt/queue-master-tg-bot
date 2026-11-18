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
from datetime import datetime
from enum import StrEnum
from zoneinfo import ZoneInfo

DEFAULT_WAIT_MINUTES = 3  # скільки чекати на «🏃 Йду» до автопропуску
ENTER_GRACE_SECONDS = 60  # через скільки після виклику наступний може натиснути «Заходжу»
NUDGE_MIN_SECONDS = 10 * 60  # «ти ще в кабінеті?» — не раніше ніж через 10 хв
AVG_WINDOW = 5  # середній час здачі рахуємо за останніми 5 людьми
# Скільки людей пропустити вперед, коли пропущений повертається:
# після 1-го пропуску — 1, після 2-го — 2, далі — в кінець черги.
RETURN_PENALTY = {1: 1, 2: 2}

_local_tz = ZoneInfo("Europe/Kyiv")


def set_timezone(name: str) -> None:
    """Часовий пояс, у якому записані час слотів («10:25»). Задається з .env."""
    global _local_tz
    _local_tz = ZoneInfo(name)


def now() -> float:
    """Поточний час. Окрема функція — щоб у тестах можна було «перемотати» час."""
    return time.time()


def slot_moment(label: str, day_of: float) -> float:
    """Мітка «10:25» → момент 10:25 (місцевого часу) того ж дня, що й `day_of`."""
    day = datetime.fromtimestamp(day_of, _local_tz)
    hours, minutes = map(int, label.split(":"))
    return day.replace(hour=hours, minute=minutes, second=0, microsecond=0).timestamp()


def local_hhmm(moment: float) -> str:
    return datetime.fromtimestamp(moment, _local_tz).strftime("%H:%M")


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


class Entered(StrEnum):
    """Що сталося з попереднім, коли наступний натиснув «🚪 Заходжу»."""

    CONFIRMED = "confirmed"  # натиснув сам поточний — те саме, що «Йду»
    CLOSED = "closed"  # попередній був у кабінеті й вийшов, але забув «Готово»
    SKIPPED = "skipped"  # попередній так і не прийшов — пропуск зі штрафом
    DEFERRED = "deferred"  # попереднього покликали раніше за його час — без штрафу


class QueueError(Exception):
    """Помилка, текст якої можна показати користувачу як є."""


@dataclass
class Slot:
    time: str  # "10:25" — мітка, на який час людина записалась
    user_id: int | None = None  # None — людина без Telegram (додана адміном)
    name: str | None = None
    status: SlotStatus = SlotStatus.EMPTY
    skips: int = 0  # скільки разів людину вже пропускали
    called_at: float | None = None  # коли покликали
    confirmed: bool = False  # натиснув «🏃 Йду»
    done_at: float | None = None  # коли відмітили «Готово» (для середнього часу)
    nudged: bool = False  # уже нагадували «ти ще в кабінеті?»
    auto_done: bool = False  # «Готово» поставлено автоматично, бо наступний зайшов

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
        self.done_at = None
        self.nudged = False
        self.auto_done = False

    def clear(self) -> None:
        self.assign(None, "")
        self.name = None
        self.status = SlotStatus.EMPTY

    def call(self) -> None:
        """Зробити поточним: таймери (підтвердження, нагадування) стартують заново."""
        self.status = SlotStatus.CURRENT
        self.called_at = now()
        self.confirmed = False
        self.nudged = False


@dataclass(frozen=True)
class TurnChange:
    """Що сказати в чаті після дії."""

    current: Slot | None  # хто зараз (None — більше нікого)
    upcoming: Slot | None  # кого попередити «готуйся»
    changed: bool = True  # чи змінився поточний (False — лише нагадування)
    skipped: Slot | None = None  # кого щойно пропустили
    closed: Slot | None = None  # кому поставили «Готово», бо наступний уже зайшов
    deferred: Slot | None = None  # кого покликали зарано і пропустили вперед наступного
    nudge: Slot | None = None  # кому нагадати «ти ще в кабінеті?»


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
    # Події останньої операції — їх забирає pop_turn_change().
    _last_skipped: Slot | None = field(default=None, repr=False)
    _last_closed: Slot | None = field(default=None, repr=False)
    _last_deferred: Slot | None = field(default=None, repr=False)
    _pending_nudge: Slot | None = field(default=None, repr=False)

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

    def people_ahead(self, slot: Slot) -> int:
        """Скільки людей (поточний + ті, хто чекає) стоїть перед цим слотом."""
        before = self.slots[: self._index(slot)]
        return sum(s.status in (SlotStatus.CURRENT, SlotStatus.WAITING) for s in before)

    def avg_turn(self) -> float:
        """Середній час на людину (від виклику до «Готово»), у секундах.

        Рахується за останніми здачами; поки даних немає — береться крок розкладу.
        """
        durations = [
            s.done_at - s.called_at
            for s in self.slots
            if s.status is SlotStatus.DONE
            and not s.auto_done
            and s.done_at is not None
            and s.called_at is not None
        ][-AVG_WINDOW:]
        return sum(durations) / len(durations) if durations else self.step * 60.0

    def can_return(self, slot: Slot) -> bool:
        """Чи може людина повернутися в чергу кнопкою «🙋 Я тут» / «✋»."""
        return slot.status is SlotStatus.SKIPPED or (
            slot.status is SlotStatus.DONE and slot.auto_done
        )

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

    @staticmethod
    def _swap_people(a: Slot, b: Slot) -> None:
        """Поміняти людей місцями. Статуси (хто поточний, хто чекає) лишаються на слотах."""
        for attr in ("user_id", "name", "skips"):
            first, second = getattr(a, attr), getattr(b, attr)
            setattr(a, attr, second)
            setattr(b, attr, first)

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
        """Що змінилось з минулого виклику (None — нічого, мовчимо).

        Сервіс викликає це після кожної операції й пише в чат за результатом.
        """
        cur = self.current
        key = (cur.time, cur.user_id) if cur else None
        changed = key != self._last_called
        events = {
            "skipped": self._last_skipped,
            "closed": self._last_closed,
            "deferred": self._last_deferred,
            "nudge": self._pending_nudge,
        }
        self._last_skipped = self._last_closed = self._last_deferred = None
        self._pending_nudge = None
        if not changed and all(e is None for e in events.values()):
            return None
        self._last_called = key
        return TurnChange(
            current=cur,
            upcoming=self.upcoming if cur else None,
            changed=changed,
            **events,
        )

    @property
    def is_finished(self) -> bool:
        """Черга запущена, але кликати більше нікого."""
        return self.status is QueueStatus.RUNNING and self.current is None

    # ------------------------------------------------------------------
    # Таймери: автопропуск і нагадування
    # ------------------------------------------------------------------

    @property
    def confirm_deadline(self) -> float | None:
        """Коли пропустити поточного, якщо він не натисне «Йду» (None — не треба).

        Якщо людину покликали раніше за її час (черга йде швидше за розклад),
        відлік починається з часу її слота: прийти на свій час — не порушення.
        """
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
        start = max(cur.called_at, slot_moment(cur.time, cur.called_at))
        return start + self.wait_minutes * 60

    @property
    def nudge_at(self) -> float | None:
        """Коли нагадати поточному «ти ще в кабінеті?» (None — не треба).

        Удвічі довше за середню здачу, але не раніше ніж через 10 хв.
        Поки чекаємо «Йду», нагадування не потрібне — там свій таймер.
        """
        cur = self.current
        if (
            self.status is not QueueStatus.RUNNING
            or cur is None
            or cur.nudged
            or cur.called_at is None
            or self.confirm_deadline is not None
        ):
            return None
        return cur.called_at + max(NUDGE_MIN_SECONDS, 2 * self.avg_turn())

    @property
    def next_timer_at(self) -> float | None:
        moments = [m for m in (self.confirm_deadline, self.nudge_at) if m is not None]
        return min(moments) if moments else None

    def expire(self) -> Slot | None:
        """Автопропуск: якщо час на підтвердження вийшов — пропустити поточного."""
        deadline = self.confirm_deadline
        if deadline is None or now() < deadline:
            return None
        return self.skip()

    def tick(self) -> None:
        """Викликається таймером: автопропуск або нагадування, якщо їхній час настав."""
        if self.expire() is not None:
            return
        nudge = self.nudge_at
        if nudge is not None and now() >= nudge:
            cur = self.current
            cur.nudged = True
            self._pending_nudge = cur

    # ------------------------------------------------------------------
    # Дії студентів
    # ------------------------------------------------------------------

    def join(self, user_id: int, name: str, time: str | None = None) -> Slot:
        self._ensure_not_closed()
        existing = self.find_user(user_id)
        if existing is not None:
            if self.can_return(existing):
                raise QueueError("Тебе пропустили — щоб повернутися, натисни «🙋 Я тут» або /here.")
            raise QueueError(f"Ти вже в черзі на {existing.time}.")

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

    def sign_up(self, user_id: int, name: str) -> tuple[bool, Slot]:
        """Кнопка «✋»: записатися, а якщо тебе пропустили — повернутися.

        Повертає (повернувся?, слот).
        """
        existing = self.find_user(user_id)
        if existing is not None and self.can_return(existing):
            return True, self.here(user_id)
        return False, self.join(user_id, name)

    def leave(self, user_id: int) -> Slot:
        self._ensure_not_closed()
        slot = self._require_user(user_id)
        if slot.status is SlotStatus.DONE:
            raise QueueError("Твою здачу вже відмічено — виписуватися не треба 🙂")
        freed = Slot(time=slot.time, user_id=slot.user_id, name=slot.name)
        slot.clear()
        self._advance()
        return freed

    def confirm(self, user_id: int) -> Slot:
        """Поточний натиснув «🏃 Йду»."""
        self._ensure_not_closed()
        cur = self.current
        if cur is None or cur.user_id != user_id:
            raise QueueError("Зараз не твоя черга 🙂")
        cur.confirmed = True
        return cur

    def later(self, user_id: int) -> tuple[Slot, Slot]:
        """Поміняти себе місцями з наступним, хто чекає.

        Статуси лишаються на місці, міняються тільки люди. Тож якщо «пізніше»
        натиснув поточний — поточним стає той, з ким помінялись.
        Повертає (мій новий слот, слот людини, яка тепер іде раніше).
        """
        self._ensure_not_closed()
        mine = self._require_user(user_id)
        if mine.status not in (SlotStatus.WAITING, SlotStatus.CURRENT):
            raise QueueError("Посунутися можна, тільки поки ти в черзі.")
        start = self._index(mine) + 1
        other = next(
            (s for s in self.slots[start:] if s.status is SlotStatus.WAITING), None
        )
        if other is None:
            raise QueueError("Після тебе нікого немає — ти і так у кінці черги.")
        self._swap_people(mine, other)
        if mine.status is SlotStatus.CURRENT:
            mine.call()  # кличемо іншу людину — таймер з нуля
        return other, mine

    def enter(self, user_id: int, is_admin: bool) -> tuple[Entered, Slot]:
        """Наступний у черзі натиснув «🚪 Заходжу».

        Що сталося з поточним, бот вирішує сам:
          - поточний натискав «Йду» (або він без Telegram) — він був у кабінеті
            й вийшов, але забув «Готово» → ставимо «Готово» автоматично;
          - не натискав, а його час уже настав → неявка, пропуск зі штрафом;
          - не натискав, але покликали раніше за його час → без штрафу,
            наступний просто йде вперед.
        Той, хто натиснув, одразу стає поточним і вже «в кабінеті».
        Повертає (що сталося, слот попередньої людини).
        """
        self._ensure_not_closed()
        cur = self.current
        if cur is None:
            raise QueueError("Зараз нікого не кличуть.")
        if cur.user_id is not None and cur.user_id == user_id:
            cur.confirmed = True
            return Entered.CONFIRMED, cur
        nxt = self.upcoming
        if nxt is None:
            raise QueueError("Після поточного в черзі нікого немає.")
        if not is_admin:
            if nxt.user_id != user_id:
                raise QueueError(
                    "Заходити може тільки наступна людина в черзі — дочекайся «готуйся» 🙂"
                )
            waited = now() - (cur.called_at or 0)
            if waited < ENTER_GRACE_SECONDS:
                left = int(ENTER_GRACE_SECONDS - waited) + 1
                raise QueueError(f"{cur.name} покликали щойно — дай ще {left} с 🙂")

        if cur.confirmed or cur.user_id is None:
            cur.status = SlotStatus.DONE
            cur.done_at = now()
            cur.auto_done = True
            self._last_closed = cur
            self._advance()
            outcome, previous = Entered.CLOSED, cur
        elif now() < slot_moment(cur.time, cur.called_at or now()):
            # Тепер у слоті cur — той, хто заходить, а в слоті nxt — той, кого відклали.
            self._swap_people(cur, nxt)
            cur.call()
            self._last_deferred = nxt
            outcome, previous = Entered.DEFERRED, nxt
        else:
            self.skip()
            outcome, previous = Entered.SKIPPED, cur
        self.current.confirmed = True
        return outcome, previous

    def here(self, user_id: int) -> Slot:
        """Пропущений студент повернувся («🙋 Я тут» / «✋» / /here).

        Він стає в чергу не одразу, а пропускає вперед кількох людей:
        після 1-го пропуску — 1, після 2-го — 2, далі — в кінець
        (див. RETURN_PENALTY). Якщо «Готово» йому поставили автоматично
        (наступний зайшов, а він ще не здавав) — повертається без штрафу,
        одразу після поточного. Слот переїжджає в списку разом із часом-міткою.
        """
        self._ensure_not_closed()
        slot = self._require_user(user_id)
        if not self.can_return(slot):
            if slot.status is SlotStatus.DONE:
                raise QueueError("Твою здачу вже відмічено ✅")
            raise QueueError("Тебе не пропускали — просто чекай своєї черги.")

        ahead = 0 if slot.status is SlotStatus.DONE else RETURN_PENALTY.get(slot.skips)
        self.slots.remove(slot)
        slot.status = SlotStatus.WAITING
        slot.called_at = slot.done_at = None
        slot.auto_done = slot.confirmed = slot.nudged = False
        cursor = self._cursor()
        future = range(cursor, len(self.slots))
        waiting = [i for i in future if self.slots[i].status is SlotStatus.WAITING]

        if ahead is None:  # третій і далі пропуск — у кінець
            occupied = [i for i in future if not self.slots[i].is_free]
            insert_at = occupied[-1] + 1 if occupied else cursor
        elif ahead == 0 or not waiting:
            insert_at = cursor
        else:
            insert_at = waiting[min(ahead, len(waiting)) - 1] + 1
        self.slots.insert(insert_at, slot)
        self._advance()
        return slot

    # ------------------------------------------------------------------
    # «Готово»
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
        slot.done_at = now()
        slot.auto_done = False
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
