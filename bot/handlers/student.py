"""Дії студентів: кнопки під чергою, команди і відповідь «Закінчив»."""

from __future__ import annotations

import re

from aiogram import Bot, F, Router
from aiogram.filters import Command, CommandObject
from aiogram.types import CallbackQuery, Message, User

from bot.handlers.helpers import display_name, key_of, ok, require_admin
from bot.parsing import normalize_time
from bot.permissions import AdminCache
from bot.render import parse_callback
from bot.service import QueueService
from bot.storage import QueueKey

router = Router(name="student")
router.message.filter(F.chat.type.in_({"group", "supergroup"}))

# «Закінчив» у відповідь на повідомлення бота.
DONE_RE = re.compile(
    r"^\s*(закінчи(в|ла|ли)|закінчено|готово|все|всьо|здав|здала|done|finished)\W*\s*$",
    re.IGNORECASE,
)


async def _finish(service: QueueService, admins: AdminCache, key: QueueKey, event, user: User, queue_id=None):
    # Адмінів питаємо в Telegram тільки якщо «закінчив» натиснув не поточний —
    # у звичайному випадку зайвого запиту немає.
    queue = service.get(key, queue_id)
    is_current = queue.current is not None and queue.current.user_id == user.id
    is_admin = False if is_current else await admins.is_admin(event)
    return await service.apply(key, lambda q: q.finish(user.id, is_admin), queue_id)


# ---------------------------------------------------------------------------
# Кнопки
# ---------------------------------------------------------------------------


@router.callback_query(F.data.startswith("q:"))
async def on_button(cb: CallbackQuery, service: QueueService, admins: AdminCache):
    parsed = parse_callback(cb.data)
    if parsed is None or not isinstance(cb.message, Message):
        await cb.answer("Ця кнопка застаріла.")
        return
    action, queue_id = parsed
    key = key_of(cb.message)
    user = cb.from_user

    match action:
        case "join":
            name = display_name(service, user)
            slot = await service.apply(key, lambda q: q.join(user.id, name), queue_id)
            await cb.answer(f"✋ Записав тебе на {slot.time}")
        case "leave":
            slot = await service.apply(key, lambda q: q.leave(user.id), queue_id)
            await cb.answer(f"🚪 Слот {slot.time} звільнено")
        case "coming":
            await service.apply(key, lambda q: q.confirm(user.id), queue_id)
            await cb.answer("🏃 Окей, чекаємо тебе!")
        case "done":
            await _finish(service, admins, key, cb, user, queue_id)
            await cb.answer("✅ Записав, що здав. Молодець!")
        case "absent":
            # Адмінів питаємо тільки якщо натиснув не наступний у черзі.
            queue = service.get(key, queue_id)
            is_next = queue.upcoming is not None and queue.upcoming.user_id == user.id
            is_admin = False if is_next else await admins.is_admin(cb)
            slot = await service.apply(key, lambda q: q.absent(user.id, is_admin), queue_id)
            await cb.answer(f"⏭ {slot.name} пропущено")
        case "later":
            mine, _ = await service.apply(key, lambda q: q.later(user.id), queue_id)
            await cb.answer(f"⏬ Тепер ти на {mine.time}")


# ---------------------------------------------------------------------------
# Команди
# ---------------------------------------------------------------------------


@router.message(Command("join"))
async def cmd_join(message: Message, command: CommandObject, service: QueueService):
    user = message.from_user
    time = normalize_time(command.args) if command.args else None
    name = display_name(service, user)
    await service.apply(key_of(message), lambda q: q.join(user.id, name, time))
    await ok(message)


@router.message(Command("leave"))
async def cmd_leave(message: Message, service: QueueService):
    user = message.from_user
    await service.apply(key_of(message), lambda q: q.leave(user.id))
    await ok(message)


@router.message(Command("later"))
async def cmd_later(message: Message, service: QueueService):
    user = message.from_user
    await service.apply(key_of(message), lambda q: q.later(user.id))
    await ok(message)


@router.message(Command("here"))
async def cmd_here(message: Message, service: QueueService):
    user = message.from_user
    await service.apply(key_of(message), lambda q: q.here(user.id))
    await ok(message)


@router.message(Command("done"))
async def cmd_done(message: Message, command: CommandObject, service: QueueService, admins: AdminCache):
    user = message.from_user
    key = key_of(message)
    if command.args:  # /done 10:45 — адмін відмічає конкретний слот
        await require_admin(message, admins)
        time = normalize_time(command.args)
        await service.apply(key, lambda q: q.finish(user.id, True, time))
    else:
        await _finish(service, admins, key, message, user)
    await ok(message)


# ---------------------------------------------------------------------------
# Відповідь «Закінчив» на повідомлення бота
# ---------------------------------------------------------------------------


@router.message(F.reply_to_message.from_user, F.text.regexp(DONE_RE))
async def reply_done(message: Message, bot: Bot, service: QueueService, admins: AdminCache):
    if message.reply_to_message.from_user.id != bot.id:
        return  # «закінчив» у відповідь людині, а не боту
    key = key_of(message)
    if service.storage.get(key) is None:
        return  # черги тут немає — мовчимо
    await _finish(service, admins, key, message, message.from_user)
    await ok(message)

