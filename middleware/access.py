from typing import Callable, Any, Awaitable
from aiogram import BaseMiddleware
from aiogram.types import Message

from config import settings
from database import UsersRepository, db_connect


class AccessMiddleware(BaseMiddleware):
    async def __call__(
        self,
        handler: Callable[[Message, dict], Awaitable[Any]],
        event: Message,
        data: dict,
    ) -> Any:
        user_id = event.from_user.id

        if user_id == settings.admin_id:
            return await handler(event, data)

        async with db_connect() as db:
            exists = await UsersRepository(db).exists(user_id)

        if not exists:
            await event.answer("Нет доступа.")
            return

        return await handler(event, data)
