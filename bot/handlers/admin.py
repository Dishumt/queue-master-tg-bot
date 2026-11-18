"""Команди адмінів групи: створити, запустити, керувати, закрити."""

from __future__ import annotations

from aiogram import F, Router
from aiogram.filters import Command, CommandObject
from aiogram.types import Message

from bot.handlers.helpers import key_of, ok, require_admin
from bot.parsing import NEW_USAGE, clean_name, normalize_time, parse_new_args
from bot.permissions import AdminCache
from bot.queue_logic import Queue, QueueError, Slot
from bot.render import mention
from bot.service import QueueService

router = Router(name="admin")
router.message.filter(F.chat.type.in_({"group", "supergroup"}))


@router.message(Command("new"))
async def cmd_new(message: Message, command: CommandObject, service: QueueService, admins: AdminCache):
    await require_admin(message, admins)
    if not command.args:
        raise QueueError(NEW_USAGE)
    args = parse_new_args(command.args)
    chat_id, thread_id = key_of(message)
    queue = Queue(
        chat_id=chat_id,
        thread_id=thread_id,
        title=args.title,
        step=args.step,
        slots=[Slot(t) for t in args.slot_times()],
    )
    await service.create(queue, created_by=message.from_user.id if message.from_user else 0)


@router.message(Command("go"))
async def cmd_go(message: Message, service: QueueService, admins: AdminCache):
    await require_admin(message, admins)
    await service.apply(key_of(message), lambda q: q.start())
    # Відповіддю буде повідомлення «твоя черга» від сервісу.


@router.message(Command("skip"))
async def cmd_skip(message: Message, service: QueueService, admins: AdminCache):
    await require_admin(message, admins)
    slot = await service.apply(key_of(message), lambda q: q.skip())
    await message.answer(f"⏭ {mention(slot)} пропущено. Якщо прийде — хай напише /here")


@router.message(Command("add"))
async def cmd_add(message: Message, command: CommandObject, service: QueueService, admins: AdminCache):
    await require_admin(message, admins)
    time, _, name = (command.args or "").partition(" ")
    if not time or not name.strip():
        raise QueueError("Формат: /add 10:45 Прізвище Ім'я")
    time, name = normalize_time(time), clean_name(name)
    await service.apply(key_of(message), lambda q: q.add(time, name))
    await ok(message)


@router.message(Command("remove"))
async def cmd_remove(message: Message, command: CommandObject, service: QueueService, admins: AdminCache):
    await require_admin(message, admins)
    if not command.args:
        raise QueueError("Формат: /remove 10:45")
    time = normalize_time(command.args)
    await service.apply(key_of(message), lambda q: q.remove(time))
    await ok(message)


@router.message(Command("repost"))
async def cmd_repost(message: Message, service: QueueService, admins: AdminCache):
    await require_admin(message, admins)
    await service.repost(key_of(message))


@router.message(Command("close"))
async def cmd_close(message: Message, service: QueueService, admins: AdminCache):
    await require_admin(message, admins)
    key = key_of(message)
    queue = service.get(key)
    await service.apply(key, lambda q: q.close())
    await service.unpin(queue)
    await message.answer("🔒 Чергу закрито.")
