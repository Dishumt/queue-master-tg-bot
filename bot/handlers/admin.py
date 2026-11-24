"""Команди адмінів групи: створити, запустити, керувати, закрити."""

from __future__ import annotations

from aiogram import F, Router
from aiogram.filters import Command, CommandObject
from aiogram.types import Message

from bot.handlers.helpers import key_of, ok, require_admin
from bot.parsing import NEW_USAGE, clean_name, parse_break, parse_new_args, parse_number, parse_wait
from bot.permissions import AdminCache
from bot.queue_logic import Queue, QueueError
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
        wait_minutes=args.wait,
        break_every=args.break_every,
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
    await service.apply(key_of(message), lambda q: q.skip())
    # Повідомлення «пропущено + наступний» надішле сервіс.


@router.message(Command("wait"))
async def cmd_wait(message: Message, command: CommandObject, service: QueueService, admins: AdminCache):
    await require_admin(message, admins)
    if not command.args:
        queue = service.get(key_of(message))
        raise QueueError(
            f"Зараз чекаю на «🏃 Йду» {queue.wait_minutes} хв.\n"
            "Змінити: /wait 5 (0 — вимкнути автопропуск)"
        )
    minutes = parse_wait(command.args)
    await service.apply(key_of(message), lambda q: q.set_wait(minutes))
    await ok(message)


@router.message(Command("breaks"))
async def cmd_breaks(message: Message, command: CommandObject, service: QueueService, admins: AdminCache):
    await require_admin(message, admins)
    if not command.args:
        raise QueueError("Формат: /breaks 5 — перерва після кожних 5 здач (0 — без перерв)")
    every = parse_break(command.args)
    await service.apply(key_of(message), lambda q: q.set_breaks(every))
    await ok(message)


@router.message(Command("add"))
async def cmd_add(message: Message, command: CommandObject, service: QueueService, admins: AdminCache):
    await require_admin(message, admins)
    if not command.args:
        raise QueueError("Формат: /add Прізвище Ім'я")
    name = clean_name(command.args)
    await service.apply(key_of(message), lambda q: q.add(name))
    await ok(message)


@router.message(Command("remove"))
async def cmd_remove(message: Message, command: CommandObject, service: QueueService, admins: AdminCache):
    await require_admin(message, admins)
    if not command.args:
        raise QueueError("Формат: /remove 3 (номер зі списку)")
    number = parse_number(command.args)
    await service.apply(key_of(message), lambda q: q.remove(number))
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
