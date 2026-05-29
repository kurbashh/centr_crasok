"""
bot/middlewares/rate_limit_middleware.py — Middleware защиты от спама.

Архитектура:
  — Outer middleware: запускается ДО фильтров и handlers, экономит ресурсы.
  — Делегирует проверку в RateLimiterService (без aiogram-зависимостей).
  — При превышении: отправляет AppError.user_message из ERROR_CATALOG.
  — Silence mode: после _SILENCE_AFTER блокировок подряд — молчим,
    чтобы не засорять чат ботом при флуде.

Логирование:
  — Каждая блокировка логируется с кодом ошибки [E400] или [E401].
  — Burst vs window блокировки различаются в логах.
  — После _SILENCE_AFTER блокировок — логируем silence с уровнем DEBUG.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.types import Message, TelegramObject

from core.errors import AppError, ErrorCode
from services.rate_limiter import rate_limiter, RateLimitResult

logger = logging.getLogger(__name__)

# После скольки подряд заблокированных запросов молчать
_SILENCE_AFTER = 2


class RateLimitMiddleware(BaseMiddleware):
    """
    Outer middleware: проверяет rate limit ДО передачи в handler.

    Если лимит превышен:
      — Первые _SILENCE_AFTER нарушения → отвечаем пользователю.
      — Дальнейший флуд → игнорируем молча (silence mode).

    Команды /start, /help, /contacts, /about — не ограничиваются:
    они лёгкие, статичные, не идут в AI.
    """

    def __init__(self) -> None:
        # user_id → счётчик подряд идущих блокировок
        self._consecutive_blocks: dict[int, int] = {}
        super().__init__()

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        # Обрабатываем только Message
        if not isinstance(event, Message):
            return await handler(event, data)

        # Команды не ограничиваем
        if event.text and event.text.startswith("/"):
            return await handler(event, data)

        user_id: int = event.from_user.id  # type: ignore[union-attr]
        result = await rate_limiter.check(user_id)

        if result.allowed:
            # Сбрасываем счётчик подряд идущих блокировок при успешном запросе
            if user_id in self._consecutive_blocks:
                prev_blocks = self._consecutive_blocks.pop(user_id)
                logger.debug(
                    "Rate limit cleared after %d consecutive blocks | user_id=%d",
                    prev_blocks, user_id,
                )
            return await handler(event, data)

        # ── Лимит превышен ────────────────────────────────────────────────────
        blocks = self._consecutive_blocks.get(user_id, 0) + 1
        self._consecutive_blocks[user_id] = blocks

        # Определяем тип блокировки (burst vs window)
        error_code, log_extra = _build_block_context(result, user_id)

        if blocks <= _SILENCE_AFTER:
            err = AppError(
                code=error_code,
                user_id=user_id,
                extra=log_extra,
            )
            err.log(logger)
            await _send_rate_limit_message(event, err)
        else:
            # Silence mode — не спамим пользователя, но логируем
            logger.debug(
                "[silence] Rate limit block #%d | user_id=%d | retry_after=%.1fs",
                blocks, user_id, result.retry_after,
            )

        # Прерываем цепочку — handler НЕ вызывается
        return None


# ──────────────────────────────────────────────────────────────────────────────
# Вспомогательные функции
# ──────────────────────────────────────────────────────────────────────────────

def _build_block_context(
    result: RateLimitResult,
    user_id: int,
) -> tuple[ErrorCode, dict]:
    """
    Определяет тип блокировки (burst или window) и формирует extra-словарь
    для AppError с нужными переменными для форматирования user_message.
    """
    wait_sec = int(result.retry_after) + 1  # +1 сек запаса

    # Если retry_after маленький — это burst (короткое окно)
    if result.retry_after <= 10.0:
        return ErrorCode.RATE_LIMIT_BURST, {
            "wait_sec": wait_sec,
            "count": result.requests_made,
            "window": 5,
            "limit": 3,
        }

    # Иначе — превышение основного окна
    return ErrorCode.RATE_LIMIT_WINDOW, {
        "wait_sec": wait_sec,
        "count": result.requests_made,
        "window": 60,
        "limit": 5,
        "retry_after": result.retry_after,
    }


async def _send_rate_limit_message(event: Message, err: AppError) -> None:
    """Отправляет вежливое сообщение о превышении лимита."""
    user_msg = err.user_message
    if not user_msg:
        return

    try:
        await event.answer(user_msg, parse_mode=None)
    except Exception as exc:
        logger.warning(
            "Failed to send rate limit message | user_id=%s | %s",
            err.user_id, exc,
        )