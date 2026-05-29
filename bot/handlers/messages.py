"""
bot/handlers/messages.py — Обработчики входящих сообщений.

Форматирование ответов: telegramify-markdown (entities-подход)
═══════════════════════════════════════════════════════════════
Gemini возвращает стандартный Markdown. Telegram его не понимает напрямую.

Три варианта и почему мы выбрали третий:

  parse_mode=MARKDOWN   — legacy, ломается на VOC_free, centr_krasok.kz,
                          8*12, незакрытых backtick'ах → E303 в логах.

  parse_mode=HTML       — надёжнее, но LLM всё равно пишет Markdown.
                          Нужна конвертация, и незакрытый <tag> тоже упадёт.

  entities (текущий)    — telegramify_markdown.convert() парсит Markdown
                          в (plain_text, list[AiogramEntity]) локально.
                          Telegram получает уже готовые entity-офсеты.
                          parse_mode не передаётся вообще.
                          Невалидный Markdown → graceful деградация в plain text.
                          E303 физически невозможен.

Установка зависимости:
  pip install telegramify-markdown
"""

from __future__ import annotations

import asyncio
import logging
import os
import tempfile

from aiogram import Router
from aiogram.enums import ChatAction
from aiogram.exceptions import TelegramBadRequest, TelegramAPIError
from aiogram.filters import BaseFilter
from aiogram.types import (
    FSInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    MessageEntity as AiogramEntity,
)
from telegramify_markdown import convert as md_to_entities

import services.ai_service as ai_service_mod
from core.errors import AppError, ErrorCode, AIServiceError
from core.logging import log_flow, RequestContext
from services.context_service import context_service

logger = logging.getLogger(__name__)
messages_router = Router(name="messages")

_CHUNK_SIZE: int = 4000        # Telegram limit 4096, берём с запасом
_TYPING_INTERVAL: float = 4.0  # Telegram сбрасывает typing через 5 с

feedback_kb = InlineKeyboardMarkup(inline_keyboard=[[
    InlineKeyboardButton(text="✅ Помогло", callback_data="fb_good"),
    InlineKeyboardButton(text="❌ Не помогло", callback_data="fb_bad"),
]])


# ──────────────────────────────────────────────────────────────────────────────
# Фильтр
# ──────────────────────────────────────────────────────────────────────────────

class IsNonText(BaseFilter):
    async def __call__(self, message: Message) -> bool:
        return not bool(message.text)


# ──────────────────────────────────────────────────────────────────────────────
# Форматирование: Markdown → Telegram entities
# ──────────────────────────────────────────────────────────────────────────────

def _prepare_reply(text: str) -> tuple[str, list[AiogramEntity]]:
    """
    Конвертирует Markdown-текст от Gemini в (plain_text, aiogram_entities).

    telegramify_markdown парсит Markdown локально через Rust-биндинги
    (pyromark/pulldown-cmark). Возвращает plain text + список своих
    telegramify_markdown.entity.MessageEntity.

    ВАЖНО: telegramify возвращает свой тип MessageEntity, а aiogram ожидает
    aiogram.types.MessageEntity. Конвертируем через .to_dict() → AiogramEntity(**d).

    При невалидном Markdown деградирует до plain text — никаких исключений.
    """
    try:
        plain_text, tg_entities = md_to_entities(text)

        # Конвертируем telegramify.MessageEntity → aiogram.MessageEntity
        # .to_dict() возвращает только непустые поля: {'type': 'bold', 'offset': 0, 'length': 6}
        aiogram_entities = [AiogramEntity(**e.to_dict()) for e in tg_entities]

        logger.debug(
            "md_to_entities | in=%d chars → out=%d chars | entities=%d",
            len(text), len(plain_text), len(aiogram_entities),
        )
        return plain_text, aiogram_entities
    except Exception as exc:
        # Защитный fallback — на случай совсем экзотического ввода
        logger.warning(
            "md_to_entities failed, falling back to plain text | %s: %s",
            type(exc).__name__, exc,
        )
        return text, []


# ──────────────────────────────────────────────────────────────────────────────
# Отправка ответа
# ──────────────────────────────────────────────────────────────────────────────

async def _send_reply(message: Message, raw_markdown: str) -> None:
    """
    Конвертирует Markdown → entities и отправляет сообщение.

    Fallback-цепочка (на случай проблем с самим текстом):
      1. Entities (основной путь, parse_mode не нужен)
      2. Plain text без entities (если Telegram отверг entities)
      3. Разбивка на чанки (если сообщение слишком длинное)
      4. Файл (если ничего не помогло)
    """
    user_id = message.from_user.id  # type: ignore[union-attr]
    plain_text, entities = _prepare_reply(raw_markdown)

    # ── Попытка 1: entities (без parse_mode) ──────────────────────────────────
    try:
        await message.answer(
            text=plain_text,
            entities=entities or None,
            reply_markup=feedback_kb,
        )
        logger.debug(
            "Reply sent | user_id=%d | mode=entities | len=%d | entities=%d",
            user_id, len(plain_text), len(entities),
        )
        return
    except TelegramBadRequest as exc:
        exc_lower = str(exc).lower()

        # ── Попытка 2: plain text без entities ────────────────────────────────
        if "entities" in exc_lower or "parse" in exc_lower:
            logger.warning(
                "Entities rejected, retrying plain | user_id=%d | %s",
                user_id, exc,
            )
            try:
                await message.answer(plain_text, reply_markup=feedback_kb)
                logger.debug("Reply sent | user_id=%d | mode=plain", user_id)
                return
            except TelegramBadRequest as exc2:
                exc_lower = str(exc2).lower()

        # ── Попытка 3: чанки ──────────────────────────────────────────────────
        if "message is too long" in exc_lower:
            logger.warning(
                "Message too long (%d chars), splitting | user_id=%d",
                len(plain_text), user_id,
            )
            await _send_chunks(message, plain_text, user_id)
            return

        # ── Попытка 4: файл ───────────────────────────────────────────────────
        logger.error(
            "[E302] All text methods failed, sending as file | user_id=%d | %s",
            user_id, exc,
        )
        await _send_as_file(message, plain_text, user_id)


async def _send_chunks(message: Message, text: str, user_id: int) -> None:
    """Разбивает длинный plain-text на чанки по _CHUNK_SIZE символов."""
    chunks = [text[i:i + _CHUNK_SIZE] for i in range(0, len(text), _CHUNK_SIZE)]
    total = len(chunks)
    logger.info("Sending %d chunks | user_id=%d | total_len=%d", total, user_id, len(text))

    for idx, chunk in enumerate(chunks, 1):
        kb = feedback_kb if idx == total else None
        try:
            await message.answer(chunk, reply_markup=kb)
        except Exception as e:
            logger.error(
                "[E302] Chunk %d/%d failed | user_id=%d | %s",
                idx, total, user_id, e,
            )


async def _send_as_file(message: Message, text: str, user_id: int) -> None:
    """Последний резерв: отправляет текст .txt-файлом."""
    tmp = tempfile.NamedTemporaryFile(
        delete=False, suffix=".txt", prefix="reply_", mode="w", encoding="utf-8"
    )
    try:
        tmp.write(text)
        tmp.close()
        await message.answer_document(
            document=FSInputFile(tmp.name),
            caption="Ответ прикреплён файлом",
            reply_markup=feedback_kb,
        )
        logger.info("Reply sent as file | user_id=%d | size=%d bytes", user_id, len(text.encode()))
    except Exception as e:
        logger.error("[E302] File send failed | user_id=%d | %s", user_id, e)
    finally:
        try:
            os.unlink(tmp.name)
        except Exception:
            pass


async def _send_error_to_user(message: Message, error: AppError) -> None:
    """Отправляет user_message из ERROR_CATALOG пользователю."""
    user_msg = error.user_message
    if not user_msg:
        return
    try:
        await message.answer(user_msg)
    except TelegramAPIError as e:
        logger.error(
            "[E302] Failed to send error message | user_id=%s | %s",
            error.user_id, e,
        )


# ──────────────────────────────────────────────────────────────────────────────
# Вспомогательная корутина typing
# ──────────────────────────────────────────────────────────────────────────────

async def _keep_typing(chat_id: int, bot) -> None:
    while True:
        try:
            await bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)
        except Exception as exc:
            logger.debug("Typing action failed (non-critical) | chat_id=%d | %s", chat_id, exc)
        await asyncio.sleep(_TYPING_INTERVAL)


# ──────────────────────────────────────────────────────────────────────────────
# Handlers
# ──────────────────────────────────────────────────────────────────────────────

@messages_router.message(IsNonText())
async def handle_non_text(message: Message) -> None:
    content_type = message.content_type.value if message.content_type else "unknown"
    user_id = message.from_user.id  # type: ignore[union-attr]
    logger.info("[E101] NON_TEXT_MESSAGE | user_id=%d | content_type=%s", user_id, content_type)
    err = AppError(
        code=ErrorCode.NON_TEXT_MESSAGE,
        user_id=user_id,
        extra={"content_type": content_type},
    )
    await _send_error_to_user(message, err)


@messages_router.message()
async def handle_text(message: Message) -> None:
    user_id: int = message.from_user.id  # type: ignore[union-attr]
    user_text: str = (message.text or "").strip()

    ctx_tokens = RequestContext.set(user_id=user_id, chat_id=message.chat.id)
    try:
        await _process_message(message, user_id, user_text)
    finally:
        RequestContext.reset(ctx_tokens)


async def _process_message(message: Message, user_id: int, user_text: str) -> None:
    if not user_text:
        logger.debug("[E100] EMPTY_MESSAGE | user_id=%d", user_id)
        err = AppError(code=ErrorCode.EMPTY_MESSAGE, user_id=user_id)
        await _send_error_to_user(message, err)
        return

    logger.info("→ handle_text | user_id=%d | text=%.80s", user_id, user_text)

    if ai_service_mod.ai_service is None:
        err = AppError(code=ErrorCode.AI_NOT_INITIALIZED, user_id=user_id)
        err.log(logger)
        await _send_error_to_user(message, err)
        return

    typing_task: asyncio.Task = asyncio.create_task(
        _keep_typing(chat_id=message.chat.id, bot=message.bot)
    )

    try:
        # [1] Сохраняем вопрос
        try:
            await context_service.add_user_message(user_id, user_text)
            logger.debug("Step 1/4: User message saved | user_id=%d", user_id)
        except Exception as e:
            logger.warning(
                "[E500] Context save failed, proceeding | user_id=%d | %s", user_id, e
            )

        # [2] Получаем историю
        history = await context_service.get_history(user_id)
        logger.debug("Step 2/4: History retrieved | user_id=%d | msgs=%d", user_id, len(history))

        # [3] Генерируем ответ
        logger.debug("Step 3/4: Calling AI service | user_id=%d", user_id)
        reply = await ai_service_mod.ai_service.generate_response(
            user_id=user_id,
            history=history,
        )

        # [4] Сохраняем ответ
        try:
            await context_service.add_assistant_message(user_id, reply)
            logger.debug("Step 4/4: Assistant message saved | user_id=%d", user_id)
        except Exception as e:
            logger.warning(
                "[E500] Context save (assistant) failed | user_id=%d | %s", user_id, e
            )

        # [5] Отправляем (entities-режим, без parse_mode)
        await _send_reply(message, reply)
        logger.info("✓ handle_text complete | user_id=%d | reply_len=%d", user_id, len(reply))

    except AppError as err:
        err.log(logger)
        await _send_error_to_user(message, err)

    except Exception as exc:
        err = AppError(
            code=ErrorCode.INTERNAL_UNKNOWN,
            user_id=user_id,
            extra={"detail": f"{type(exc).__name__}: {exc}"},
            cause=exc,
        )
        err.log(logger)
        await _send_error_to_user(message, err)

    finally:
        typing_task.cancel()
        logger.debug("Typing cancelled | user_id=%d", user_id)