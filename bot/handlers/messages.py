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
import io
import tempfile
import os

from aiogram import Router
from aiogram.enums import ChatAction, ParseMode
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import FSInputFile, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.filters import BaseFilter
from aiogram.types import Message

import services.ai_service as ai_service_mod
from services.context_service import context_service

logger = logging.getLogger(__name__)
messages_router = Router(name="messages")

# ─────────────────────────────────────────────────────────────────────────────
# Тексты системных сообщений (минималистичный стиль)
# ─────────────────────────────────────────────────────────────────────────────

_MSG_NON_TEXT: str = (
    "Я работаю только с текстовыми вопросами. Пожалуйста, напишите свой вопрос."
)

_MSG_RATE_LIMIT: str = (
    "AI-сервис временно перегружен. Пожалуйста, повторите запрос через несколько секунд."
)

_MSG_ERROR: str = (
    "Произошла ошибка при обработке запроса. Попробуйте позже или напишите: info@centr-krasok.kz"
)

_MSG_EMPTY: str = (
    "Пожалуйста, напишите ваш вопрос о краске, грунтовке или материалах для отделки."
)

# Интервал повторной отправки «typing» в секундах (Telegram сбрасывает его через 5 с)
_TYPING_INTERVAL: float = 4.0

# Клавиатура обратной связи
feedback_kb = InlineKeyboardMarkup(inline_keyboard=[[
    InlineKeyboardButton(text="✅ Помогло", callback_data="fb_good"),
    InlineKeyboardButton(text="❌ Не помогло", callback_data="fb_bad"),
]])


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
        # Проверяем, инициализирован ли AI-сервис (инициализация делается в main.py)
        if ai_service_mod.ai_service is None:
            logger.error("AI service is not initialized")
            await message.answer(_MSG_ERROR)
            return

        reply = await ai_service_mod.ai_service.generate_response(
            user_id=user_id,
            history=history,
        )

        # ── 4. Сохраняем ответ ассистента ────────────────────────────────
        await context_service.add_assistant_message(user_id, reply)

        # ── 5. Отправляем ответ ───────────────────────────────────────────
# Try Markdown, then plain text, then split into chunks or send as a text file.
        try:
            await message.answer(reply, parse_mode=ParseMode.MARKDOWN, reply_markup=feedback_kb)
        except TelegramBadRequest as exc_markdown:
            logger.warning("Telegram parse error, retrying without parse_mode: %s", exc_markdown)
            try:
                # Override bot default parse_mode by explicitly passing None
                await message.answer(reply, parse_mode=None, reply_markup=feedback_kb)
            except TelegramBadRequest as exc_plain:
                # Split into chunks (Telegram 4096 char limit) or send as file
                if "message is too long" in str(exc_plain).lower():
                    logger.warning("Message too long, splitting into chunks: %s", exc_plain)
                    chunk_size = 4000
                    chunks = [reply[i:i+chunk_size] for i in range(0, len(reply), chunk_size)]
                    for i, chunk in enumerate(chunks, 1):
                        try:
                            # Добавляем кнопки только к последнему фрагменту
                            kb = feedback_kb if i == len(chunks) else None
                            await message.answer(chunk, parse_mode=None, reply_markup=kb)
                        except Exception as e:
                            logger.error("Failed to send chunk %d: %s", i, e)
                else:
                    # Other parse errors: send as file
                    logger.exception("Failed sending reply as text; sending as file. | %s", exc_plain)
                    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".txt", prefix="reply_", mode="w", encoding="utf-8")
                    try:
                        tmp.write(reply)
                        tmp.close()
                        # Use FSInputFile for local files on all platforms
                        await message.answer_document(document=FSInputFile(tmp.name), caption="Ответ (файл)", reply_markup=feedback_kb)
                    finally:
                        try:
                            os.unlink(tmp.name)
                        except Exception:
                            logger.debug("Failed to delete temp file %s", tmp.name)

    except Exception as e:
            # Специальная обработка ошибок AI-сервиса для понятных UX-сообщений
            if isinstance(e, ai_service_mod.AIRateLimitError):
                await message.answer(_MSG_RATE_LIMIT)
                logger.exception("AI RateLimit | user_id=%d: %s", user_id, e)
            elif isinstance(e, ai_service_mod.AIConnectionError):
                await message.answer(_MSG_RATE_LIMIT)
                logger.exception("AI ConnectionError | user_id=%d: %s", user_id, e)
            else:
                await message.answer(_MSG_ERROR)
                logger.exception("Ошибка при обработке запроса AI | user_id=%d: %s", user_id, e)

    finally:
            # ── Гарантированно останавливаем typing ──────────────────────────
            typing_task.cancel()