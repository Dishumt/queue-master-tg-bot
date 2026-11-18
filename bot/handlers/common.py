"""Довідка і /name — працюють і в групі, і в особистих повідомленнях."""

from __future__ import annotations

from aiogram import F, Router
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import Message

from bot.handlers.helpers import key_of, ok
from bot.parsing import clean_name
from bot.queue_logic import QueueError
from bot.service import QueueService

router = Router(name="common")

HELP_TEXT = """\
<b>Queue Master</b> — черга на здачу лаб 📋

<b>Студентам</b>
✋ <b>Записатися</b> (кнопка) — перший вільний слот
/join 10:45 — записатися на конкретний час
/leave — виписатися
/done або відповідь «Закінчив» на повідомлення бота — я здав, кличте наступного
/later — пропустити вперед того, хто після тебе
/here — мене пропустили, але я прийшов
/name Прізвище Ім'я — як тебе показувати в черзі

<b>Адмінам групи</b>
/new Лаба 3 10:25-13:00 5 перерва 12:00-12:20 — нова черга
/go — почати приймати
/skip — поточний не прийшов
/done 10:45 — відмітити конкретний слот (прийняли поза чергою)
/add 10:45 Прізвище Ім'я — вписати людину без Telegram
/remove 10:45 — звільнити слот
/repost — переслати чергу вниз чату
/close — закрити чергу
"""


@router.message(CommandStart(), F.chat.type == "private")
async def cmd_start_private(message: Message):
    await message.answer(
        "Привіт! Я веду чергу на здачу лаб у груповому чаті.\n"
        "Додай мене в групу, і адмін створить чергу командою /new.\n\n" + HELP_TEXT
    )


@router.message(Command("help"))
async def cmd_help(message: Message):
    await message.answer(HELP_TEXT)


@router.message(Command("name"))
async def cmd_name(message: Message, command: CommandObject, service: QueueService):
    if not command.args:
        current = service.storage.get_name(message.from_user.id)
        raise QueueError(
            f"Зараз тебе показую як: <b>{current or message.from_user.full_name}</b>\n"
            "Змінити: /name Прізвище Ім'я"
        )
    name = clean_name(command.args)
    user_id = message.from_user.id
    await service.storage.set_name(user_id, name)

    # Якщо людина вже в черзі цього чату — оновлюємо ім'я і там.
    if message.chat.type != "private":
        key = key_of(message)
        queue = service.storage.get(key)
        if queue is not None and queue.find_user(user_id) is not None:

            def rename(q):
                q.find_user(user_id).name = name

            await service.apply(key, rename)
    await ok(message)
