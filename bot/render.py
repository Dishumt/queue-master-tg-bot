"""Як черга виглядає в Telegram: текст закріпленого повідомлення і кнопки.

Тільки перетворення «черга → текст/клавіатура», без відправки повідомлень.
Формат тексту — HTML (parse_mode="HTML").
"""

from __future__ import annotations

from html import escape

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from bot.parsing import parse_time
from bot.queue_logic import Queue, QueueStatus, Slot, SlotStatus

# Дані кнопок: "q:<дія>:<id черги>". id потрібен, щоб кнопки старого
# повідомлення не чіпали нову чергу в тому самому чаті.
CB_PREFIX = "q"
ACTIONS = ("join", "leave", "done", "later")


def mention(slot: Slot) -> str:
    """Тег людини. Без user_id (доданий адміном) — просто жирне ім'я."""
    name = escape(slot.name or "?")
    if slot.user_id is None:
        return f"<b>{name}</b>"
    return f'<a href="tg://user?id={slot.user_id}">{name}</a>'


def _slot_line(slot: Slot, upcoming: Slot | None) -> str:
    time = f"<code>{slot.time}</code>"
    name = escape(slot.name or "")
    match slot.status:
        case SlotStatus.EMPTY:
            return f"{time}  ·  —"
        case SlotStatus.DONE:
            return f"{time} ✅ {name}"
        case SlotStatus.CURRENT:
            return f"{time} ▶️ <b>{name}</b>  ← здає"
        case SlotStatus.SKIPPED:
            return f"{time} ⏭ <s>{name}</s>  (пропущено)"
        case SlotStatus.WAITING if slot is upcoming:
            return f"{time} ⏳ {name}  ← готується"
        case _:
            return f"{time} ▫️ {name}"


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
    prev_minutes: int | None = None
    for slot in queue.slots:
        minutes = parse_time(slot.time)
        if prev_minutes is not None and minutes - prev_minutes > queue.step:
            lines.append("<i>── перерва ──</i>")
        prev_minutes = minutes
        lines.append(_slot_line(slot, upcoming))

    if queue.status is not QueueStatus.CLOSED:
        lines += [
            "",
            "<i>Записатися на конкретний час: /join 10:45\n"
            "Закінчив — кнопка ✅ або відповідь «Закінчив» на повідомлення бота.</i>",
        ]
    return "\n".join(lines)


def queue_keyboard(queue: Queue) -> InlineKeyboardMarkup | None:
    if queue.status is QueueStatus.CLOSED:
        return None

    def btn(text: str, action: str) -> InlineKeyboardButton:
        return InlineKeyboardButton(text=text, callback_data=f"{CB_PREFIX}:{action}:{queue.id}")

    return InlineKeyboardMarkup(
        inline_keyboard=[
            [btn("✋ Записатися", "join"), btn("🚪 Виписатися", "leave")],
            [btn("✅ Я закінчив", "done"), btn("⏬ Пізніше", "later")],
        ]
    )


def parse_callback(data: str | None) -> tuple[str, int] | None:
    """'q:join:12' -> ('join', 12). Чужі або биті дані -> None."""
    parts = (data or "").split(":")
    if len(parts) != 3 or parts[0] != CB_PREFIX or parts[1] not in ACTIONS:
        return None
    try:
        return parts[1], int(parts[2])
    except ValueError:
        return None
