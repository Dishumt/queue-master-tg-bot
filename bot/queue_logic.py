"""Чиста логіка черги — без Telegram і без бази даних.

Головне правило: `slots` — це список людей у порядку виклику, а «наступний» —
перший у списку зі статусом WAITING. Часу в черзі немає, є тільки порядок.
Після кожної дії викликається `_advance()`, яка за потреби кличе наступного
або оголошує перерву.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import StrEnum

MAX_PEOPLE = 50  # щоб закріплене повідомлення влізло в ліміт Telegram (4096 символів)
DEFAULT_WAIT_MINUTES = 3  # скільки чекати на «🏃 Йду» до автопропуску
ENTER_GRACE_SECONDS = 60  # через скільки після виклику наступний може натиснути «Заходжу»
NUDGE_MIN_SECONDS = 10 * 60  # «ти ще в кабінеті?» — не раніше ніж через 10 хв
DEFAULT_TURN_SECONDS = 5 * 60  # скільки триває здача, поки немає даних
AVG_WINDOW = 5  # середній час здачі рахуємо за останніми 5 людьми
# Скільки людей пропустити вперед, коли пропущений повертається:
# після 1-го пропуску — 1, після 2-го — 2, далі — в кінець черги.
RETURN_PENALTY = {1: 1, 2: 2}


def now() -> float:
    """Поточний час. Окрема функція — щоб у тестах можна було «перемотати» час."""
    return time.time()


class SlotStatus(StrEnum):
    WAITING = "waiting"  # чекає своєї черги
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


class QueueError(Exception):
    """Помилка, текст якої можна показати користувачу як є."""


@dataclass
class Slot:
    """Місце однієї людини в черзі."""

    user_id: int | None = None  # None — людина без Telegram (додана адміном)
    name: str = ""
    status: SlotStatus = SlotStatus.WAITING
    skips: int = 0  # скільки разів людину вже пропускали
    called_at: float | None = None  # коли покликали
    confirmed: bool = False  # натиснув «🏃 Йду»
    done_at: float | None = None  # коли відмітили «Готово» (для середнього часу)
    nudged: bool = False  # уже нагадували «ти ще в кабінеті?»
    auto_done: bool = False  # «Готово» поставлено автоматично, бо наступний зайшов

    def call(self) -> None:
        """Зробити поточним: таймери стартують заново."""
        self.status = SlotStatus.CURRENT
        self.called_at = now()
        self.confirmed = False
        self.nudged = False

    def back_to_waiting(self) -> None:
        self.status = SlotStatus.WAITING
        self.called_at = self.done_at = None
        self.confirmed = self.nudged = self.auto_done = False


@dataclass(frozen=True)
class TurnChange:
    """Що сказати в чаті після дії."""

    current: Slot | None  # хто зараз (None — нікого)
    upcoming: Slot | None  # хто наступний
    changed: bool = True  # чи змінився поточний (False — лише нагадування)
    skipped: Slot | None = None  # кого щойно пропустили
    closed: Slot | None = None  # кому поставили «Готово», бо наступний уже зайшов
    nudge: Slot | None = None  # кому нагадати «ти ще в кабінеті?»


@dataclass
class Queue:
    chat_id: int
    thread_id: int  # 0, якщо в групі немає топіків
    title: str
    slots: list[Slot] = field(default_factory=list)
    status: QueueStatus = QueueStatus.OPEN
    wait_minutes: int = DEFAULT_WAIT_MINUTES  # 0 — автопропуск вимкнено
    break_every: int = 0  # після скількох здач перерва (0 — без перерв)
    turns: int = 0  # скільки здач було після останньої перерви
    on_break: bool = False  # перерва: нікого не кличемо, доки не натиснуть «Готово»
    message_id: int | None = None  # id закріпленого повідомлення
    id: int | None = None  # id у базі даних
    # Хто був поточним до останньої операції — щоб зрозуміти, кого кликати.
    _last_called: Slot | None = field(default=None, repr=False)
    # Події останньої операції — їх забирає pop_turn_change().
    _last_skipped: Slot | None = field(default=None, repr=False)
    _last_closed: Slot | None = field(default=None, repr=False)
    _pending_nudge: Slot | None = field(default=None, repr=False)

    # ------------------------------------------------------------------
    # Пошук
    # ------------------------------------------------------------------

    @property
    def current(self) -> Slot | None:
        return next((s for s in self.slots if s.status is SlotStatus.CURRENT), None)

    @property
    def waiting(self) -> list[Slot]:
        return [s for s in self.slots if s.status is SlotStatus.WAITING]

    @property
    def upcoming(self) -> Slot | None:
        """Хто піде наступним."""
        return next(iter(self.waiting), None)

    def find_user(self, user_id: int) -> Slot | None:
        return next((s for s in self.slots if s.user_id == user_id), None)

    def find_number(self, number: int) -> Slot:
        """Людина за номером у списку (як у закріпленому повідомленні)."""
        if not 1 <= number <= len(self.slots):
            raise QueueError(f"Номера {number} немає в черзі.")
        return self.slots[number - 1]

    def people_ahead(self, slot: Slot) -> int:
        """Скільки людей (поточний + ті, хто чекає) стоїть перед цією людиною."""
        before = self.slots[: self._index(slot)]
        return sum(s.status in (SlotStatus.CURRENT, SlotStatus.WAITING) for s in before)

    def wait_seconds(self, slot: Slot) -> float:
        """Орієнтовно скільки ще чекати цій людині (без перерв — їхня довжина невідома)."""
        return self.people_ahead(slot) * self.avg_turn()

    def avg_turn(self) -> float:
        """Середній час на людину (від виклику до «Готово»), у секундах."""
        durations = [
            s.done_at - s.called_at
            for s in self.slots
            if s.status is SlotStatus.DONE
            and not s.auto_done
            and s.done_at is not None
            and s.called_at is not None
        ][-AVG_WINDOW:]
        return sum(durations) / len(durations) if durations else float(DEFAULT_TURN_SECONDS)

    def can_return(self, slot: Slot) -> bool:
        """Чи може людина повернутися кнопкою «🙋 Я тут» / «✋»."""
        return slot.status is SlotStatus.SKIPPED or (
            slot.status is SlotStatus.DONE and slot.auto_done
        )

    def _index(self, slot: Slot) -> int:
        return next(i for i, s in enumerate(self.slots) if s is slot)

    def _take_out(self, slot: Slot) -> None:
        # Не list.remove(): той порівнює за значенням і може прибрати тезку.
        del self.slots[self._index(slot)]

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

    def _advance(self, allow_break: bool = True) -> None:
        """Якщо зараз ніхто не здає — покликати наступного або почати перерву."""
        if self.status is not QueueStatus.RUNNING or self.current or self.on_break:
            return
        nxt = self.upcoming
        if nxt is None:
            self.turns = 0  # черга порожня — це вже відпочинок
        elif allow_break and self.break_every and self.turns >= self.break_every:
            self.turns = 0
            self.on_break = True
        else:
            nxt.call()

    def pop_turn_change(self) -> TurnChange | None:
        """Що змінилось з минулого виклику (None — нічого, мовчимо)."""
        cur = self.current
        changed = cur is not self._last_called
        events = {
            "skipped": self._last_skipped,
            "closed": self._last_closed,
            "nudge": self._pending_nudge,
        }
        self._last_skipped = self._last_closed = self._pending_nudge = None
        if not changed and all(e is None for e in events.values()):
            return None
        self._last_called = cur
        return TurnChange(current=cur, upcoming=self.upcoming, changed=changed, **events)

    # ------------------------------------------------------------------
    # Таймери: автопропуск і нагадування
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

    @property
    def nudge_at(self) -> float | None:
        """Коли нагадати поточному «ти ще в кабінеті?» (None — не треба)."""
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
        if self.status is not QueueStatus.RUNNING:
            return None
        moments = (self.confirm_deadline, self.nudge_at)
        return min((m for m in moments if m is not None), default=None)

    def expire(self) -> Slot | None:
        """Автопропуск: якщо час на підтвердження вийшов — пропустити поточного."""
        deadline = self.confirm_deadline
        if deadline is None or now() < deadline:
            return None
        return self.skip()

    def tick(self) -> None:
        """Викликається таймером: робить те, чий час настав."""
        if self.expire() is not None:
            return
        nudge = self.nudge_at
        if nudge is not None and now() >= nudge:
            self.current.nudged = True
            self._pending_nudge = self.current

    def _resume(self, user_id: int, is_admin: bool) -> Slot | None:
        """«Готово» під час перерви: вона закінчилась, кличемо наступного."""
        nxt = self.upcoming
        is_next = nxt is not None and nxt.user_id == user_id
        if not is_next and not is_admin:
            raise QueueError("Зараз перерва ☕ Закінчити її може наступна людина або адмін.")
        self._end_break()
        if is_next:
            nxt.confirmed = True  # натиснув сам — отже, вже тут
        return nxt

    def _end_break(self) -> None:
        self.on_break = False
        self._advance()

    # ------------------------------------------------------------------
    # Дії студентів
    # ------------------------------------------------------------------

    def _append(self, user_id: int | None, name: str) -> Slot:
        if len(self.slots) >= MAX_PEOPLE:
            raise QueueError(f"У черзі вже {MAX_PEOPLE} людей — більше не влізе.")
        slot = Slot(user_id=user_id, name=name)
        self.slots.append(slot)
        self._advance()
        return slot

    def join(self, user_id: int, name: str) -> Slot:
        """Стати в кінець черги."""
        self._ensure_not_closed()
        existing = self.find_user(user_id)
        if existing is not None:
            if existing.status is SlotStatus.DONE:
                raise QueueError("Твою здачу вже відмічено ✅")
            raise QueueError(f"Ти вже в черзі, номер {self._index(existing) + 1}.")
        return self._append(user_id, name)

    def sign_up(self, user_id: int, name: str) -> tuple[bool, Slot]:
        """Кнопка «✋»: записатися, а якщо тебе пропустили — повернутися.

        Повертає (повернувся?, слот).
        """
        self._ensure_not_closed()
        existing = self.find_user(user_id)
        if existing is not None and self.can_return(existing):
            return True, self.here(user_id)
        return False, self.join(user_id, name)

    def leave(self, user_id: int) -> Slot:
        self._ensure_not_closed()
        slot = self._require_user(user_id)
        if slot.status is SlotStatus.DONE:
            raise QueueError("Твою здачу вже відмічено — виписуватися не треба 🙂")
        self._take_out(slot)
        self._advance()
        return slot

    def confirm(self, user_id: int) -> Slot:
        """Поточний натиснув «🏃 Йду»."""
        self._ensure_not_closed()
        cur = self.current
        if cur is None or cur.user_id != user_id:
            raise QueueError("Зараз не твоя черга 🙂")
        cur.confirmed = True
        return cur

    def later(self, user_id: int) -> Slot:
        """Пропустити вперед одну людину. Поточний так передає чергу наступному."""
        self._ensure_not_closed()
        mine = self._require_user(user_id)
        if mine.status not in (SlotStatus.WAITING, SlotStatus.CURRENT):
            raise QueueError("Посунутися можна, тільки поки ти в черзі.")
        was_current = mine.status is SlotStatus.CURRENT
        start = 0 if was_current else self._index(mine) + 1
        other = next((s for s in self.slots[start:] if s.status is SlotStatus.WAITING), None)
        if other is None:
            raise QueueError("Після тебе нікого немає — ти і так у кінці черги.")
        self._take_out(mine)
        self.slots.insert(self._index(other) + 1, mine)
        if was_current:
            mine.back_to_waiting()
            other.call()
        return mine

    def enter(self, user_id: int, is_admin: bool) -> tuple[Entered, Slot]:
        """Наступний у черзі натиснув «🚪 Заходжу».

        Якщо попередній натискав «Йду» (або він без Telegram) — він був у
        кабінеті й забув «Готово». Якщо не натискав — не прийшов, це пропуск.
        Той, хто натиснув, одразу стає поточним. Перерви тут немає: людина вже
        в кабінеті, тому перерва буде після неї.
        Повертає (що сталося, слот попередньої людини).
        """
        self._ensure_not_closed()
        cur = self.current
        if cur is None:
            if self.on_break:
                return Entered.CONFIRMED, self._resume(user_id, is_admin)
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
                raise QueueError(f"{cur.name} — виклик був щойно, дай ще {left} с 🙂")

        if cur.confirmed or cur.user_id is None:
            self._mark_done(cur, auto=True)
            self._last_closed = cur
            outcome = Entered.CLOSED
        else:
            self._mark_skipped(cur)
            outcome = Entered.SKIPPED
        self._advance(allow_break=False)
        nxt.confirmed = True
        return outcome, cur

    def here(self, user_id: int) -> Slot:
        """Пропущений повернувся: стає в чергу зі штрафом (див. RETURN_PENALTY).

        Якщо «Готово» йому поставили автоматично — без штрафу, наступним.
        """
        self._ensure_not_closed()
        slot = self._require_user(user_id)
        if not self.can_return(slot):
            if slot.status is SlotStatus.DONE:
                raise QueueError("Твою здачу вже відмічено ✅")
            raise QueueError("Тебе не пропускали — просто чекай своєї черги.")

        ahead = 0 if slot.status is SlotStatus.DONE else RETURN_PENALTY.get(slot.skips)
        self._take_out(slot)
        slot.back_to_waiting()
        waiting = self.waiting
        if ahead is None or not waiting:
            self.slots.append(slot)
        elif ahead == 0:
            self.slots.insert(self._index(waiting[0]), slot)
        else:
            self.slots.insert(self._index(waiting[min(ahead, len(waiting)) - 1]) + 1, slot)
        self._advance()
        return slot

    # ------------------------------------------------------------------
    # «Готово» і пропуск
    # ------------------------------------------------------------------

    def _mark_done(self, slot: Slot, auto: bool = False) -> None:
        if slot.status is SlotStatus.CURRENT:
            self.turns += 1
        slot.status = SlotStatus.DONE
        slot.done_at = now()
        slot.auto_done = auto

    def _mark_skipped(self, slot: Slot) -> None:
        slot.status = SlotStatus.SKIPPED
        slot.skips += 1
        slot.called_at = None
        self._last_skipped = slot

    def finish(self, user_id: int, is_admin: bool, number: int | None = None) -> Slot | None:
        """Позначити здачу (поточного або за номером), а під час перерви — закінчити її."""
        self._ensure_not_closed()
        if number is not None:
            if not is_admin:
                raise QueueError("Відмічати за номером може тільки адмін.")
            slot = self.find_number(number)
            if slot.status is SlotStatus.DONE:
                raise QueueError(f"Номер {number} вже відмічено ✅")
        else:
            slot = self.current
            if slot is None and self.on_break:
                return self._resume(user_id, is_admin)
            if slot is None:
                if self.status is QueueStatus.OPEN:
                    raise QueueError("Черга ще не запущена.")
                raise QueueError("Зараз ніхто не здає.")
            if slot.user_id != user_id and not is_admin:
                raise QueueError(f"Зараз здає {slot.name}, а не ти.")
        self._mark_done(slot)
        self._advance()
        return slot

    def skip(self) -> Slot:
        self._ensure_not_closed()
        slot = self.current
        if slot is None:
            raise QueueError("Зараз нікого пропускати.")
        self._mark_skipped(slot)
        self._advance()
        return slot

    # ------------------------------------------------------------------
    # Дії адміна
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Запустити чергу, а під час перерви — закінчити перерву раніше."""
        self._ensure_not_closed()
        if self.on_break:
            self._end_break()
            return
        if self.status is QueueStatus.RUNNING:
            raise QueueError("Черга вже запущена.")
        if not self.waiting:
            raise QueueError("У черзі ще ніхто не записався.")
        self.status = QueueStatus.RUNNING
        self._advance()

    def set_wait(self, minutes: int) -> None:
        self._ensure_not_closed()
        self.wait_minutes = minutes

    def set_breaks(self, every: int) -> None:
        self._ensure_not_closed()
        self.break_every = every

    def add(self, name: str) -> Slot:
        """Дописати в кінець людину без Telegram."""
        self._ensure_not_closed()
        return self._append(None, name)

    def remove(self, number: int) -> Slot:
        self._ensure_not_closed()
        slot = self.find_number(number)
        self._take_out(slot)
        self._advance()
        return slot

    def close(self) -> None:
        self._ensure_not_closed()
        self.status = QueueStatus.CLOSED
        self.on_break = False
