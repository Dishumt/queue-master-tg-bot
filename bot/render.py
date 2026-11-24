"""Як черга виглядає в Telegram: текст повідомлень і кнопки.

Тільки перетворення «черга → текст/клавіатура», без відправки повідомлень.
Формат тексту — HTML (parse_mode="HTML").

Тексти — без граматичного роду («далі ти», «не відгукується»), бо в групі
і хлопці, і дівчата.
"""

from __future__ import annotations

import math
import re
from html import escape

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from bot.queue_logic import (
    RETURN_PENALTY,
    Queue,
    QueueStatus,
    Slot,
    SlotStatus,
    TurnChange,
)

# Дані кнопок: "q:<дія>:<id черги>". id потрібен, щоб кнопки старого
# повідомлення не чіпали нову чергу в тому самому чаті.
CB_PREFIX = "q"
ACTIONS = ("join", "leave", "where", "coming", "done", "later", "enter", "here")

# Підписи кнопок — в одному місці, щоб тексти підказок їм відповідали.
BTN_JOIN = "✋ Записатися"
BTN_LEAVE = "❌ Виписатися"
BTN_WHERE = "📍 Де я?"
BTN_COMING = "🏃 Йду"
BTN_DONE = "✅ Готово"
BTN_LATER = "⏬ Пізніше"
BTN_ENTER = "🚪 Заходжу"
BTN_HERE = "🙋 Я тут"


def mention(slot: Slot) -> str:
    """Тег людини. Без user_id (доданий адміном) — просто жирне ім'я."""
    name = escape(slot.name or "?")
    if slot.user_id is None:
        return f"<b>{name}</b>"
    return f'<a href="tg://user?id={slot.user_id}">{name}</a>'


def plural(n: int, one: str, few: str, many: str) -> str:
    """plural(2, "людина", "людини", "людей") -> "людини"."""
    if n % 10 == 1 and n % 100 != 11:
        return one
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return few
    return many


def people(n: int) -> str:
    return f"{n} {plural(n, 'людина', 'людини', 'людей')}"


# ---------------------------------------------------------------------------
# Закріплене повідомлення
# ---------------------------------------------------------------------------


def _slot_line(slot: Slot, ready: Slot | None) -> str:
    number = slot.place
    name = escape(slot.name)
    returned = " ↩️" if slot.skips else ""  # повернувся після пропуску
    match slot.status:
        case SlotStatus.DONE:
            return f"{number}. ✅ {name}"
        case SlotStatus.CURRENT if slot.confirmed or slot.user_id is None:
            return f"{number}. ▶️ <b>{name}</b>{returned}  ← здає"
        case SlotStatus.CURRENT:
            return f"{number}. 📣 <b>{name}</b>{returned}  ← кличемо…"
        case SlotStatus.SKIPPED:
            return f"{number}. ⏭ <s>{name}</s>  (пропущено)"
        case SlotStatus.WAITING if slot is ready:
            return f"{number}. ⏳ {name}{returned}  ← готується"
        case _:
            return f"{number}. ▫️ {name}{returned}"


def render_queue(queue: Queue) -> str:
    done = sum(s.status is SlotStatus.DONE for s in queue.slots)
    total = len(queue.slots)

    match queue.status:
        case QueueStatus.OPEN:
            status = f"📝 Запис відкрито · зайнято {total} з {queue.places} місць"
        case QueueStatus.RUNNING if queue.on_break:
            status = f"☕ Перерва · здали {done} з {total}"
        case QueueStatus.RUNNING:
            status = f"🟢 Приймають · здали {done} з {total}"
        case _:
            status = f"🔒 Черга закрита · здали {done} з {total}"

    lines = [f"📋 <b>{escape(queue.title)}</b>", status, ""]
    # «Готується» показуємо тільки коли чергу запущено.
    ready = queue.upcoming if queue.status is QueueStatus.RUNNING else None
    last_waiting = queue.waiting[-1] if queue.waiting else None
    turns = queue.turns
    for slot in queue.slots:
        lines.append(_slot_line(slot, ready))
        if slot.status in (SlotStatus.CURRENT, SlotStatus.WAITING):
            turns += 1
            # Лінія там, де буде перерва (після останнього її не буває).
            if queue.break_every and turns >= queue.break_every and slot is not last_waiting:
                if last_waiting is not None and queue.status is not QueueStatus.CLOSED:
                    lines.append("<i>── ☕ перерва ──</i>")
                turns = 0

    if queue.status is not QueueStatus.CLOSED:
        wait = (
            f"Покликали — натисни «{BTN_COMING}» за {queue.wait_minutes} хв, інакше пропуск.\n"
            if queue.wait_minutes
            else ""
        )
        pick = (
            "Обери місце кнопкою з номером. Порожні місця пропускаються."
            if queue.status is QueueStatus.OPEN
            else "Записатися на місце: /join 10. Порожні місця пропускаються."
        )
        lines += [
            "",
            f"<i>{pick}\n{wait}"
            f"Після здачі — «{BTN_DONE}». Попередня людина вийшла, а ти під дверима — «{BTN_ENTER}».</i>",
        ]
    return "\n".join(lines)


def _btn(queue: Queue, text: str, action: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, callback_data=f"{CB_PREFIX}:{action}:{queue.id}")


def _turn_rows(queue: Queue) -> list[list[InlineKeyboardButton]]:
    return [
        [_btn(queue, BTN_COMING, "coming"), _btn(queue, BTN_DONE, "done")],
        [_btn(queue, BTN_LATER, "later"), _btn(queue, BTN_ENTER, "enter")],
    ]


def queue_keyboard(queue: Queue) -> InlineKeyboardMarkup | None:
    if queue.status is QueueStatus.CLOSED:
        return None
    rows = []
    if queue.status is QueueStatus.OPEN:
        # Кнопки з вільними місцями, по 6 у рядку.
        free = [_btn(queue, str(p), f"p{p}") for p in queue.free_places]
        rows += [free[i : i + 6] for i in range(0, len(free), 6)]
    rows += [[_btn(queue, BTN_JOIN, "join"), _btn(queue, BTN_WHERE, "where")]]
    if queue.status is QueueStatus.RUNNING:
        rows += _turn_rows(queue)
    # «Виписатися» окремо внизу — щоб не натиснути випадково замість «Записатися».
    rows.append([_btn(queue, BTN_LEAVE, "leave")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


# ---------------------------------------------------------------------------
# Повідомлення «твоя черга»
# ---------------------------------------------------------------------------


def render_turn(queue: Queue, change: TurnChange) -> str | None:
    """Текст після зміни в черзі. None — нічого не писати."""
    lines = []
    if change.closed is not None:
        lines.append(
            f"✅ {mention(change.closed)} — «{BTN_DONE}» поставлено автоматично: "
            f"наступна людина вже зайшла. Якщо ще не здано — натисни «{BTN_HERE}»."
        )
    if change.skipped is not None:
        lines.append(
            f"⏭ {mention(change.skipped)} не відгукується — пропускаю. "
            f"Прийдеш — натисни «{BTN_HERE}»."
        )

    if change.changed:
        cur = change.current
        if cur is not None:
            if cur.confirmed:
                lines.append(f"▶️ Зараз здає: {mention(cur)}")
            else:
                lines.append(f"▶️ {mention(cur)}, твоя черга!")
                if queue.confirm_deadline is not None:
                    lines.append(
                        f"Натисни «{BTN_COMING}» протягом {queue.wait_minutes} хв, інакше пропущу."
                    )
            if change.upcoming is not None:
                lines.append(f"⏳ {mention(change.upcoming)}, готуйся — далі ти.")
        elif queue.on_break:
            lines.append("☕ Перерва. Нікого не кличу.")
            if change.upcoming is not None:
                lines.append(
                    f"⏳ {mention(change.upcoming)}, далі ти. "
                    f"Коли перерва закінчиться — натисни «{BTN_DONE}»."
                )
        elif queue.status is QueueStatus.RUNNING:
            lines.append("🎉 Більше нікого в черзі! Можна дописатися — бот покличе.")

    if change.nudge is not None:
        lines.append(
            f"⏰ {mention(change.nudge)}, ти ще в кабінеті? "
            f"Якщо вже все — натисни «{BTN_DONE}», бо наступні чекають."
        )
        nxt = queue.upcoming
        if nxt is not None:
            lines.append(
                f"{mention(nxt)}, якщо бачиш, що кабінет вільний, — натисни «{BTN_ENTER}»."
            )
    return "\n".join(lines) or None


def turn_keyboard(queue: Queue, change: TurnChange) -> InlineKeyboardMarkup:
    rows = _turn_rows(queue)
    last = [_btn(queue, BTN_WHERE, "where")]
    if change.skipped is not None or change.closed is not None:
        last.append(_btn(queue, BTN_HERE, "here"))
    rows.append(last)
    return InlineKeyboardMarkup(inline_keyboard=rows)


# ---------------------------------------------------------------------------
# «📍 Де я?» — спливаюче вікно тільки для того, хто натиснув (до 200 символів)
# ---------------------------------------------------------------------------


def render_position(queue: Queue, user_id: int) -> str:
    slot = queue.find_user(user_id)
    if slot is None:
        return f"Тебе немає в цій черзі. Натисни «{BTN_JOIN}»."
    match slot.status:
        case SlotStatus.DONE if slot.auto_done:
            return (
                f"«{BTN_DONE}» тобі поставили автоматично. "
                f"Якщо ще не здано — натисни «{BTN_JOIN}» і станеш одразу після поточного."
            )
        case SlotStatus.DONE:
            return "Твою здачу вже відмічено ✅"
        case SlotStatus.SKIPPED:
            ahead = RETURN_PENALTY.get(slot.skips)
            penalty = f"перед тобою стане ще {people(ahead)}" if ahead else "станеш у кінець черги"
            return f"Тебе пропустили. Натисни «{BTN_JOIN}», щоб повернутися: {penalty}."
        case SlotStatus.CURRENT if slot.confirmed:
            return f"Зараз твоя черга — успіхів! Після здачі натисни «{BTN_DONE}»."
        case SlotStatus.CURRENT:
            return f"Зараз твоя черга! Натисни «{BTN_COMING}»."

    ahead = queue.people_ahead(slot)
    if queue.status is QueueStatus.OPEN:
        return f"Твоє місце — {slot.place}. Перед тобою {people(ahead)}. Черга ще не почалась."
    minutes = math.ceil(queue.wait_seconds(slot) / 60)
    return f"Твоє місце — {slot.place}. Перед тобою {people(ahead)}, орієнтовно ~{minutes} хв."


def parse_callback(data: str | None) -> tuple[str, int] | None:
    """'q:join:12' -> ('join', 12). Чужі або биті дані -> None."""
    parts = (data or "").split(":")
    if len(parts) != 3 or parts[0] != CB_PREFIX:
        return None
    if parts[1] not in ACTIONS and not re.fullmatch(r"p[0-9]{1,2}", parts[1]):
        return None
    try:
        return parts[1], int(parts[2])
    except ValueError:
        return None
