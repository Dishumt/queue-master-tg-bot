"""Наскрізний тест: справжній Dispatcher + обробники, але фейковий Telegram.

FakeSession підміняє HTTP-запити до Telegram: записує, які методи викликав
бот, і повертає правдоподібні відповіді. Так можна перевірити весь шлях
«апдейт → фільтри → обробник → сервіс → повідомлення» без токена.
"""

from __future__ import annotations

import itertools
from datetime import datetime

import pytest
from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.base import BaseSession
from aiogram.methods import (
    AnswerCallbackQuery,
    EditMessageText,
    GetChatAdministrators,
    GetMe,
    PinChatMessage,
    SendMessage,
    SetMessageReaction,
    TelegramMethod,
)
from aiogram.types import CallbackQuery, Chat, ChatMemberOwner, Message, Update, User

from bot.handlers import build_router
from bot.permissions import AdminCache
from bot.service import QueueService
from bot.storage import Storage

BOT_ID = 42
CHAT_ID = -100123
ADMIN = User(id=1, is_bot=False, first_name="Староста")
OLENA = User(id=2, is_bot=False, first_name="Олена")
ANDRII = User(id=3, is_bot=False, first_name="Андрій")
BOT_USER = User(id=BOT_ID, is_bot=True, first_name="Queue", username="queue_bot")
CHAT = Chat(id=CHAT_ID, type="supergroup", title="КН-21")

# Роутери aiogram можна підключити лише один раз, тому Dispatcher спільний,
# а сервіс і сховище в нього підставляються свіжі для кожного тесту.
DP = Dispatcher()
DP.include_router(build_router())


class FakeSession(BaseSession):
    def __init__(self) -> None:
        super().__init__()
        self.calls: list[TelegramMethod] = []
        self._ids = itertools.count(1000)

    async def make_request(self, bot, method, timeout=None):
        self.calls.append(method)
        match method:
            case GetMe():
                return BOT_USER
            case SendMessage():
                return Message(
                    message_id=next(self._ids),
                    date=datetime.now(),
                    chat=CHAT,
                    from_user=BOT_USER,
                    text=method.text,
                )
            case GetChatAdministrators():
                return [ChatMemberOwner(user=ADMIN, is_anonymous=False)]
            case _:
                return True

    async def close(self):
        pass

    async def stream_content(self, *args, **kwargs):  # pragma: no cover
        raise NotImplementedError

    def sent(self, kind=SendMessage):
        return [c for c in self.calls if isinstance(c, kind)]

    def texts(self) -> list[str]:
        return [c.text for c in self.sent(SendMessage)]


@pytest.fixture
async def env(tmp_path):
    session = FakeSession()
    bot = Bot(f"{BOT_ID}:TEST", session=session, default=DefaultBotProperties(parse_mode="HTML"))
    storage = Storage(str(tmp_path / "q.db"))
    await storage.open()
    service = QueueService(bot, storage)
    service.refresh_interval = 0
    dp = DP
    dp["service"] = service
    dp["admins"] = AdminCache(bot)

    ids = itertools.count(1)

    async def send(user: User, text: str, reply_to: Message | None = None) -> Message:
        msg = Message(
            message_id=next(ids),
            date=datetime.now(),
            chat=CHAT,
            from_user=user,
            text=text,
            reply_to_message=reply_to,
        )
        await dp.feed_update(bot, Update(update_id=next(ids), message=msg))
        await service.wait_background()
        return msg

    async def press(user: User, data: str, on_message_id: int) -> None:
        cb = CallbackQuery(
            id=str(next(ids)),
            from_user=user,
            chat_instance="x",
            data=data,
            message=Message(message_id=on_message_id, date=datetime.now(), chat=CHAT, from_user=BOT_USER, text="..."),
        )
        await dp.feed_update(bot, Update(update_id=next(ids), callback_query=cb))
        await service.wait_background()

    class Env:
        pass

    e = Env()
    e.session, e.service, e.storage, e.send, e.press = session, service, storage, send, press
    yield e
    await storage.close()


def last_answer(session: FakeSession) -> AnswerCallbackQuery:
    return session.sent(AnswerCallbackQuery)[-1]


async def test_full_lab_session(env):
    # Студент не може створити чергу.
    await env.send(OLENA, "/new Лаба 3 10:25-10:40")
    assert "для адмінів" in env.session.texts()[-1]

    # Адмін створює — бот публікує й закріплює повідомлення черги.
    await env.send(ADMIN, "/new Лаба 3 10:25-10:40")
    queue = env.storage.get((CHAT_ID, 0))
    assert queue is not None and queue.message_id is not None
    assert "Лаба 3" in env.session.texts()[-1]
    assert env.session.sent(PinChatMessage)

    # Олена записується кнопкою, Андрій — командою на конкретний час.
    await env.press(OLENA, f"q:join:{queue.id}", queue.message_id)
    assert "10:25" in last_answer(env.session).text
    await env.send(ANDRII, "/join 10:35")
    assert env.session.sent(SetMessageReaction)  # 👍 на команду
    assert env.session.sent(EditMessageText)  # закріплене повідомлення оновилось

    # Старт: бот тегає Олену і попереджає Андрія.
    await env.send(ADMIN, "/go")
    call = env.session.texts()[-1]
    assert f"tg://user?id={OLENA.id}" in call and "твоя черга" in call
    assert f"tg://user?id={ANDRII.id}" in call and "готуйся" in call

    # Андрій не може «закінчити» за Олену.
    await env.press(ANDRII, f"q:done:{queue.id}", queue.message_id)
    answer = last_answer(env.session)
    assert answer.show_alert and "Олена" in answer.text

    # Олена відповідає «Закінчив» на повідомлення бота — кличуть Андрія.
    bot_msg = Message(message_id=5000, date=datetime.now(), chat=CHAT, from_user=BOT_USER, text="...")
    await env.send(OLENA, "Закінчив!", reply_to=bot_msg)
    assert f"tg://user?id={ANDRII.id}" in env.session.texts()[-1]
    assert queue.current.user_id == ANDRII.id

    # «Закінчив» у відповідь людині ігнорується.
    before = len(env.session.calls)
    await env.send(ANDRII, "закінчив", reply_to=Message(message_id=1, date=datetime.now(), chat=CHAT, from_user=OLENA, text="hi"))
    assert len(env.session.calls) == before
    assert queue.current.user_id == ANDRII.id

    # Андрій натискає кнопку — черга закінчилась.
    await env.press(ANDRII, f"q:done:{queue.id}", queue.message_id)
    assert "Усі записані здали" in env.session.texts()[-1]

    # Закриття.
    await env.send(ADMIN, "/close")
    assert env.storage.get((CHAT_ID, 0)) is None
    assert "закрито" in env.session.texts()[-1]

    # Кнопки старого повідомлення більше не працюють.
    await env.press(OLENA, f"q:join:{queue.id}", queue.message_id)
    assert "неактивна" in last_answer(env.session).text


async def test_errors_are_shown_to_user(env):
    await env.send(OLENA, "/join")
    assert "немає активної черги" in env.session.texts()[-1]
    await env.send(ADMIN, "/new Лаба")
    assert "проміжку" in env.session.texts()[-1]
    await env.send(ADMIN, "/new Лаба 10:00-10:10")
    await env.send(ADMIN, "/new Ще одна 10:00-10:10")
    assert "уже є активна черга" in env.session.texts()[-1]


async def test_name_command_renames_in_queue(env):
    await env.send(ADMIN, "/new Лаба 10:00-10:10")
    await env.send(OLENA, "/join")
    await env.send(OLENA, "/name Коваленко Олена")
    queue = env.storage.get((CHAT_ID, 0))
    assert queue.find_user(OLENA.id).name == "Коваленко Олена"
    edits = env.session.sent(EditMessageText)
    assert "Коваленко Олена" in edits[-1].text
