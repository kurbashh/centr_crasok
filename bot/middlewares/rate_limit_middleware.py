"""
bot/middlewares/rate_limit_middleware.py — Middleware защиты от спама.

Архитектура:
  — Outer middleware: запускается ДО фильтров и handlers.
    Это важно — мы хотим отсекать спам ещё до любой бизнес-логики.
  — Делегирует проверку в RateLimiterService (без aiogram-зависимостей).
  — При превышении лимита: отправляет предупреждение и прерывает цепочку
    (не вызывает handler). При повторном спаме — молчит (anti-flood silence).

Два режима ответа на rate limit:
  1. Первое превышение → отправляем вежливое сообщение с таймером.
  2. Повторные превышения подряд → молчим (не засоряем чат).
     Telegram сам по себе ограничивает отправку: если слать много,
     начнёт возвращать 429.

Почему outer, а не inner middleware:
  — Inner выполняется ПОСЛЕ фильтров (filters уже потратили ресурсы).
  — Outer перехватывает вообще все апдейты — экономим максимум.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.types import Message, TelegramObject

from services.rate_limiter import rate_limiter

logger = logging.getLogger(__name__)

# После скольких подряд заблокированных запросов — молчать
_SILENCE_AFTER = 2


class RateLimitMiddleware(BaseMiddleware):
    """
    Outer middleware: проверяет rate limit ДО передачи в handler.

    Если лимит превышен:
      — Первые _SILENCE_AFTER нарушения → отвечаем пользователю.
      — Дальнейший флуд → игнорируем молча (silence mode).
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
        # Обрабатываем только Message (не callback_query, не inline и т.д.)
        if not isinstance(event, Message):
            return await handler(event, data)

        # Команды /start, /help, /contacts, /about — не ограничиваем
        # Они лёгкие, статичные, не идут в AI
        if event.text and event.text.startswith("/"):
            return await handler(event, data)

        user_id: int = event.from_user.id  # type: ignore[union-attr]

        result = await rate_limiter.check(user_id)

        if result.allowed:
            # Сбрасываем счётчик подряд идущих блокировок
            self._consecutive_blocks.pop(user_id, None)
            return await handler(event, data)

        # ── Лимит превышен ────────────────────────────────────────────────
        blocks = self._consecutive_blocks.get(user_id, 0) + 1
        self._consecutive_blocks[user_id] = blocks

        logger.warning(
            "BLOCKED | user_id=%d | block_streak=%d | retry=%.1fs",
            user_id, blocks, result.retry_after,
        )

        if blocks <= _SILENCE_AFTER:
            # Формируем человекочитаемое сообщение
            wait_sec = int(result.retry_after) + 1  # +1 секунда запаса
            await self._send_limit_message(event, wait_sec, result.requests_made)

        # Прерываем цепочку — handler НЕ вызывается
        return None

    @staticmethod
    async def _send_limit_message(
        message: Message,
        wait_sec: int,
        requests_made: int,
    ) -> None:
        """Отправляет вежливое предупреждение о превышении лимита."""
        if wait_sec <= 5:
            text = (
                "Пожалуйста, чуть помедленнее — я ещё обрабатываю предыдущие вопросы.\n"
                f"Подождите {wait_sec} сек. и повторите."
            )
        elif wait_sec <= 30:
            text = (
                f"Вы отправили много сообщений за короткое время.\n"
                f"Подождите {wait_sec} секунд, затем продолжим."
            )
        else:
            minutes = (wait_sec + 59) // 60
            text = (
                f"Превышен лимит сообщений.\n"
                f"Попробуйте через {minutes} мин. — я всегда здесь."
            )

        try:
            await message.answer(text)
        except Exception as exc:
            logger.warning("Не удалось отправить rate-limit сообщение: %s", exc)