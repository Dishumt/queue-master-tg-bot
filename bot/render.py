"""Як черга виглядає в Telegram: текст повідомлень і кнопки.

Тільки перетворення «черга → текст/клавіатура», без відправки повідомлень.
Формат тексту — HTML (parse_mode="HTML").
"""

from __future__ import annotations

from html import escape

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from bot.parsing import parse_time
from bot.queue_logic import Queue, QueueStatus, Slot, SlotStatus, TurnChange

# Дані кнопок: "q:<дія>:<id черги>". id потрібен, щоб кнопки старого
# повідомлення не чіпали нову чергу в тому самому чаті.
CB_PREFIX = "q"
ACTIONS = ("join", "leave", "coming", "done", "later", "absent")


def mention(slot: Slot) -> str:
    """Тег людини. Без user_id (доданий адміном) — просто жирне ім'я."""
    name = escape(slot.name or "?")
    if slot.user_id is None:
        return f"<b>{name}</b>"
    return f'<a href="tg://user?id={slot.user_id}">{name}</a>'


# ---------------------------------------------------------------------------
# Закріплене повідомлення
# ---------------------------------------------------------------------------


def _slot_line(slot: Slot, upcoming: Slot | None) -> str:
    time = f"<code>{slot.time}</code>"
    name = escape(slot.name or "")
    returned = " ↩️" if slot.skips else ""  # повернувся після пропуску
    match slot.status:
        case SlotStatus.EMPTY:
            return f"{time}  ·  —"
        case SlotStatus.DONE:
            return f"{time} ✅ {name}"
        case SlotStatus.CURRENT if slot.confirmed or slot.user_id is None:
            return f"{time} ▶️ <b>{name}</b>{returned}  ← здає"
        case SlotStatus.CURRENT:
            return f"{time} 📣 <b>{name}</b>{returned}  ← кличемо…"
        case SlotStatus.SKIPPED:
            return f"{time} ⏭ <s>{name}</s>  (пропущено, /here)"
        case SlotStatus.WAITING if slot is upcoming:
            return f"{time} ⏳ {name}{returned}  ← готується"
        case _:
            return f"{time} ▫️ {name}{returned}"


def render_queue(queue: Queue) -> str:
    done = sum(s.status is SlotStatus.DONE for s in queue.slots)
    total = sum(not s.is_free for s in queue.slots)

    match queue.status:
        case QueueStatus.OPEN:
            status = f"📝 Запис відкрито · записалось {total}"
        case QueueStatus.RUNNING:
            status = f"🟢 Приймають · здали {done} з {total}"
        case _:
            status = f"🔒 Черга закрита · здали {done} з {total}"

    # «Готується» показуємо тільки коли хтось уже здає.
    upcoming = queue.upcoming if queue.current else None
    lines = [f"📋 <b>{escape(queue.title)}</b>", status, ""]
    # Слоти йдуть у порядку виклику, і той, хто повернувся, стоїть «не за часом».
    # Тому перерви беремо з розкладу (відсортованих часів), а не з сусідніх
    # рядків, і малюємо лінію, коли список «перетинає» перерву, рухаючись уперед.
    schedule = sorted(parse_time(s.time) for s in queue.slots)
    break_ends = [b for a, b in zip(schedule, schedule[1:]) if b - a > queue.step]
    latest: int | None = None
    for slot in queue.slots:
        minutes = parse_time(slot.time)
        if latest is not None and any(latest < end <= minutes for end in break_ends):
            lines.append("<i>── перерва ──</i>")
        latest = minutes if latest is None else max(latest, minutes)
        lines.append(_slot_line(slot, upcoming))

    if queue.status is not QueueStatus.CLOSED:
        wait = (
            f"Покликали — натисни 🏃 за {queue.wait_minutes} хв, інакше пропуск.\n"
            if queue.wait_minutes
            else ""
        )
        lines += [
            "",
            f"<i>{wait}"
            "Записатися на конкретний час: /join 10:45\n"
            "Закінчив — кнопка ✅ або відповідь «Закінчив» на повідомлення бота.</i>",
        ]
    return "\n".join(lines)


def _btn(queue: Queue, text: str, action: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, callback_data=f"{CB_PREFIX}:{action}:{queue.id}")


def _turn_rows(queue: Queue) -> list[list[InlineKeyboardButton]]:
    return [
        [_btn(queue, "🏃 Йду", "coming"), _btn(queue, "✅ Я закінчив", "done")],
        [_btn(queue, "⏬ Пізніше", "later"), _btn(queue, "🙅 Його немає", "absent")],
    ]


def queue_keyboard(queue: Queue) -> InlineKeyboardMarkup | None:
    if queue.status is QueueStatus.CLOSED:
        return None
    rows = [[_btn(queue, "✋ Записатися", "join"), _btn(queue, "🚪 Виписатися", "leave")]]
    if queue.status is QueueStatus.RUNNING:
        rows += _turn_rows(queue)
    return InlineKeyboardMarkup(inline_keyboard=rows)


# ---------------------------------------------------------------------------
# Повідомлення «твоя черга»
# ---------------------------------------------------------------------------


def render_turn(queue: Queue, change: TurnChange) -> str | None:
    """Текст після зміни поточного. None — нічого не писати."""
    lines = []
    if change.skipped is not None:
        lines.append(
            f"⏭ {mention(change.skipped)} не відгукнувся — пропускаю. "
            "Прийдеш — напиши /here"
        )
    if change.current is not None:
        cur = change.current
        lines.append(f"▶️ {mention(cur)}, твоя черга! ({cur.time})")
        if queue.confirm_deadline is not None:
            lines.append(f"Натисни «🏃 Йду» протягом {queue.wait_minutes} хв, інакше пропущу.")
        if change.upcoming is not None:
            lines.append(f"⏳ {mention(change.upcoming)}, готуйся — ти наступний.")
    elif queue.status is QueueStatus.RUNNING:
        lines.append("🎉 Більше нікого в черзі! Можна дописатися — бот покличе.")
    return "\n".join(lines) or None


def turn_keyboard(queue: Queue) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=_turn_rows(queue))


def parse_callback(data: str | None) -> tuple[str, int] | None:
    """'q:join:12' -> ('join', 12). Чужі або биті дані -> None."""
    parts = (data or "").split(":")
    if len(parts) != 3 or parts[0] != CB_PREFIX or parts[1] not in ACTIONS:
        return None
    try:
        return parts[1], int(parts[2])
    except ValueError:
        return None
