"""Дрібниці, спільні для всіх обробників."""

from __future__ import annotations

from aiogram.exceptions import TelegramAPIError
from aiogram.types import Message, ReactionTypeEmoji, User

from bot.parsing import clean_name
from bot.permissions import AdminCache
from bot.queue_logic import QueueError
from bot.service import QueueService
from bot.storage import QueueKey


def key_of(message: Message) -> QueueKey:
    """Черга прив'язана до чату і топіка (0 — якщо топіків немає).

    `is_topic_message` важливий: у звичайних групах відповіді теж мають
    message_thread_id, але це не топік.
    """
    thread = message.message_thread_id if message.is_topic_message else None
    return message.chat.id, thread or 0


def display_name(service: QueueService, user: User) -> str:
    """Ім'я з /name, а якщо не задано — ім'я з Telegram."""
    return service.storage.get_name(user.id) or clean_name(user.full_name)


async def require_admin(event, admins: AdminCache) -> None:
    if not await admins.is_admin(event):
        raise QueueError("Це команда для адмінів групи.")


async def ok(message: Message) -> None:
    """Тиха відповідь «прийнято» — реакція замість нового повідомлення в чаті."""
    try:
        await message.react([ReactionTypeEmoji(emoji="👍")])
    except TelegramAPIError:
        pass
