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

<b>Студентам</b> (усе є кнопками під чергою)
✋ <b>Записатися</b> — найближче вільне місце; якщо тебе пропустили — повернутися
<b>Кнопка з номером</b> або /join 10 — зайняти місце 10 чи пересісти на нього
📍 <b>Де я?</b> — скільки людей перед тобою і скільки орієнтовно чекати
🏃 <b>Йду</b> — тебе покликали, ти йдеш. Без цього за кілька хвилин пропущу
✅ <b>Готово</b> (або /done, або відповідь «Закінчив» на повідомлення бота) — здано, кличте наступного
🚪 <b>Заходжу</b> — для наступного в черзі: попередній вийшов або не прийшов, а ти заходиш
⏬ <b>Пізніше</b> — пропустити вперед того, хто після тебе
🙋 <b>Я тут</b> (або /here) — тебе пропустили, але ти вже прийшов. Штраф: 1-й раз уперед іде 1 людина, 2-й — 2, далі — в кінець
❌ <b>Виписатися</b> — двічі поспіль, щоб не натиснути випадково
/name Прізвище Ім'я — як тебе показувати в черзі

<b>Адмінам групи</b>
/new Лаба 3 — нова черга
/new Лаба 3 місць 30 перерва кожні 5 чекати 3 — з налаштуваннями
/breaks 5 — перерва після кожних 5 здач (0 — без перерв). Закінчити її: «✅ Готово» або /done
/wait 5 — скільки хвилин чекати на «🏃 Йду» (0 — без автопропуску)
/go — почати приймати або закінчити перерву раніше
/skip — поточний не прийшов
/done 3 — відмітити місце 3 (прийняли поза чергою)
/add 10 Прізвище Ім'я — вписати людину без Telegram
/remove 3 — звільнити місце 3
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
