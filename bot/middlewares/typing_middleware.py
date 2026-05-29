"""
bot/middlewares/typing_middleware.py — Middleware «эффект набора текста».

Автоматически отправляет ChatAction.TYPING перед каждым обработчиком
текстового сообщения, чтобы пользователь видел активность бота.
Handlers больше не нужно делать это вручную.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.enums import ChatAction
from aiogram.types import Message, TelegramObject

logger = logging.getLogger(__name__)


class TypingMiddleware(BaseMiddleware):
    """
    Inner middleware: запускается непосредственно перед handler-ом.
    Отправляет «typing» только для текстовых сообщений.
    """

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        if isinstance(event, Message) and event.text:
            try:
                await event.bot.send_chat_action(  # type: ignore[union-attr]
                    chat_id=event.chat.id,
                    action=ChatAction.TYPING,
                )
            except Exception as exc:
                # Не ломаем основной flow из-за второстепенного действия
                logger.warning("Не удалось отправить typing action: %s", exc)

        return await handler(event, data)