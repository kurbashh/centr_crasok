"""
bot/handlers/messages.py — Обработчики входящих сообщений.

Ответственность (только Telegram-слой):
  1. Маршрутизировать по типу контента (текст / всё остальное).
  2. Координировать вызовы ContextService и AIService.
  3. Показывать «typing» во время генерации ответа.
  4. Обрабатывать ошибки и возвращать понятные сообщения пользователю.

Handler НЕ содержит бизнес-логики — только оркестрация сервисов.
"""

from __future__ import annotations

import asyncio
import logging

from aiogram import Router
from aiogram.enums import ChatAction, ParseMode
from aiogram.filters import BaseFilter
from aiogram.types import Message

from services.ai_service import ai_service
from services.context_service import context_service

logger = logging.getLogger(__name__)
messages_router = Router(name="messages")

# ---------------------------------------------------------------------------
# Тексты UX-ответов
# ---------------------------------------------------------------------------

_MSG_NON_TEXT = (
    "🖼 Я понимаю только текстовые вопросы о компании.\n"
    "Напишите свой вопрос — и я с удовольствием отвечу!"
)
_MSG_RATE_LIMIT = (
    "⚠️ AI-сервис временно перегружен запросами.\n"
    "Пожалуйста, повторите вопрос через несколько секунд."
)
_MSG_ERROR = (
    "❌ Извините, произошёл сбой при обработке запроса. Попробуйте позже.\n"
    "Если проблема не исчезает — напишите нам: info@centr-krasok.kz"
)
_MSG_EMPTY = "Пожалуйста, введите ваш вопрос."

# Интервал повторной отправки «typing» в секундах (Telegram сбрасывает его через 5 с)
_TYPING_INTERVAL: float = 4.0


# ---------------------------------------------------------------------------
# Фильтр нетекстовых сообщений
# ---------------------------------------------------------------------------

class IsNonText(BaseFilter):
    """True, если у входящего сообщения нет текстового содержимого."""

    async def __call__(self, message: Message) -> bool:
        return not bool(message.text)


# ---------------------------------------------------------------------------
# Вспомогательная корутина: циклический typing-индикатор
# ---------------------------------------------------------------------------

async def _keep_typing(chat_id: int, bot) -> None:
    """
    Периодически отправляет ChatAction.TYPING, пока не будет отменена.
    Запускается как asyncio.Task и отменяется после получения ответа от AI.
    """
    while True:
        try:
            await bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)
        except Exception as exc:
            logger.debug("typing action failed: %s", exc)
        await asyncio.sleep(_TYPING_INTERVAL)


# ---------------------------------------------------------------------------
# Обработчик нетекстовых сообщений
# ---------------------------------------------------------------------------

@messages_router.message(IsNonText())
async def handle_non_text(message: Message) -> None:
    """Вежливо отклоняет стикеры, фото, голосовые и прочее."""
    await message.answer(_MSG_NON_TEXT)


# ---------------------------------------------------------------------------
# Основной обработчик — текстовые сообщения
# ---------------------------------------------------------------------------

@messages_router.message()
async def handle_text(message: Message) -> None:
    """
    Основной flow:
      1. Валидация текста.
      2. Старт циклического typing-индикатора.
      3. Сохранение вопроса в ContextService.
      4. Получение истории и вызов AIService.
      5. Сохранение ответа в ContextService.
      6. Отправка ответа пользователю.
      7. Гарантированная остановка typing-задачи.
    """
    user_id: int = message.from_user.id   # type: ignore[union-attr]
    user_text: str = (message.text or "").strip()

    if not user_text:
        await message.answer(_MSG_EMPTY)
        return

    logger.info("MSG | user_id=%d | %.80s", user_id, user_text)

    # ── Запускаем циклический typing-индикатор ────────────────────────────
    typing_task: asyncio.Task = asyncio.create_task(
        _keep_typing(chat_id=message.chat.id, bot=message.bot)
    )

    try:
        # ── 1. Сохраняем вопрос пользователя ─────────────────────────────
        await context_service.add_user_message(user_id, user_text)

        # ── 2. Получаем историю (уже включает текущий вопрос) ─────────────
        history = await context_service.get_history(user_id)

        # ── 3. Генерируем ответ ───────────────────────────────────────────
        reply = await ai_service.generate_response(
            user_id=user_id,
            history=history,
        )

        # ── 4. Сохраняем ответ ассистента ────────────────────────────────
        await context_service.add_assistant_message(user_id, reply)

        # ── 5. Отправляем ответ ───────────────────────────────────────────
        await message.answer(reply, parse_mode=ParseMode.MARKDOWN)

    except Exception as e:
            await message.answer(_MSG_ERROR)
            logger.exception("Ошибка при обработке запроса AI | user_id=%d: %s", user_id, e)

    finally:
            # ── Гарантированно останавливаем typing ──────────────────────────
            typing_task.cancel()