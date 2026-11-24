"""Дії студентів: кнопки під чергою, команди і відповідь «Закінчив»."""

from __future__ import annotations

import re

from aiogram import Bot, F, Router
from aiogram.filters import Command, CommandObject
from aiogram.types import CallbackQuery, Message, User

from bot.handlers.helpers import display_name, key_of, ok, require_admin
from bot.parsing import parse_number
from bot.permissions import AdminCache
from bot.queue_logic import Entered
from bot.render import BTN_DONE, BTN_LEAVE, parse_callback, people, render_position
from bot.service import QueueService
from bot.storage import QueueKey

router = Router(name="student")
router.message.filter(F.chat.type.in_({"group", "supergroup"}))

# «Закінчив(ла)» у відповідь на повідомлення бота.
DONE_RE = re.compile(
    r"^\s*(закінчи(в|ла|ли)|закінчено|готово|все|всьо|здав|здала|здано|done|finished)\W*\s*$",
    re.IGNORECASE,
)

ENTER_ANSWERS = {
    Entered.CONFIRMED: "🏃 Чекаємо! Після здачі натисни «{done}».",
    Entered.CLOSED: "🚪 Успіхів! Попередньому поставлено «{done}».",
    Entered.SKIPPED: "🚪 Успіхів! Попередній не прийшов — його пропущено.",
}


async def _finish(service: QueueService, admins: AdminCache, key: QueueKey, event, user: User, queue_id=None):
    # Адмінів питаємо в Telegram тільки якщо «Готово» натиснув не поточний —
    # у звичайному випадку зайвого запиту немає.
    queue = service.get(key, queue_id)
    is_current = queue.current is not None and queue.current.user_id == user.id
    is_admin = False if is_current else await admins.is_admin(event)
    return await service.apply(key, lambda q: q.finish(user.id, is_admin), queue_id)


async def _enter(service: QueueService, admins: AdminCache, key: QueueKey, event, user: User, queue_id=None):
    # Адмінів питаємо тільки якщо натиснув не наступний у черзі.
    queue = service.get(key, queue_id)
    is_next = queue.upcoming is not None and queue.upcoming.user_id == user.id
    is_current = queue.current is not None and queue.current.user_id == user.id
    is_admin = False if is_next or is_current else await admins.is_admin(event)
    outcome, _ = await service.apply(key, lambda q: q.enter(user.id, is_admin), queue_id)
    return outcome


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
            returned, slot = await service.apply(key, lambda q: q.sign_up(user.id, name), queue_id)
            if returned:
                ahead = service.get(key, queue_id).people_ahead(slot)
                await cb.answer(f"🙋 З поверненням! Перед тобою {people(ahead)}.")
            else:
                await cb.answer(f"✋ Тебе записано. Перед тобою {people(service.get(key, queue_id).people_ahead(slot))}.")
        case "here":
            slot = await service.apply(key, lambda q: q.here(user.id), queue_id)
            ahead = service.get(key, queue_id).people_ahead(slot)
            await cb.answer(f"🙋 З поверненням! Перед тобою {people(ahead)}.")
        case "leave":
            if not service.confirm_leave(queue_id, user.id):
                await cb.answer(f"Точно виписатися? Натисни «{BTN_LEAVE}» ще раз протягом 5 с.")
                return
            await service.apply(key, lambda q: q.leave(user.id), queue_id)
            await cb.answer("Тебе виписано з черги")
        case "where":
            await cb.answer(render_position(service.get(key, queue_id), user.id), show_alert=True)
        case "coming":
            await service.apply(key, lambda q: q.confirm(user.id), queue_id)
            await cb.answer(
                f"🏃 Чекаємо! Як вийдеш — натисни «{BTN_DONE}», інакше наступні не дізнаються.",
                show_alert=True,
            )
        case "done":
            await _finish(service, admins, key, cb, user, queue_id)
            await cb.answer("✅ Здачу відмічено. Молодець!")
        case "enter":
            outcome = await _enter(service, admins, key, cb, user, queue_id)
            await cb.answer(ENTER_ANSWERS[outcome].format(done=BTN_DONE))
        case "later":
            mine = await service.apply(key, lambda q: q.later(user.id), queue_id)
            await cb.answer(f"⏬ Тепер перед тобою {people(service.get(key, queue_id).people_ahead(mine))}.")


# ---------------------------------------------------------------------------
# Команди
# ---------------------------------------------------------------------------


@router.message(Command("join"))
async def cmd_join(message: Message, service: QueueService):
    user = message.from_user
    name = display_name(service, user)
    await service.apply(key_of(message), lambda q: q.sign_up(user.id, name))
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
    if command.args:  # /done 3 — адмін відмічає людину за номером
        await require_admin(message, admins)
        number = parse_number(command.args)
        await service.apply(key, lambda q: q.finish(user.id, True, number))
    else:
        await _finish(service, admins, key, message, user)
    await ok(message)


@router.message(Command("enter"))
async def cmd_enter(message: Message, service: QueueService, admins: AdminCache):
    await _enter(service, admins, key_of(message), message, message.from_user)
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
