"""
bot/__init__.py — Фабрика Dispatcher.

Централизованное место для регистрации всех роутеров и middleware.
bot.py импортирует только create_dispatcher() и не знает деталей сборки.

Порядок middleware (важен!):
  1. RateLimitMiddleware (outer) — первый барьер, отсекает спам до всего
  2. TypingMiddleware    (inner) — запускается прямо перед handler-ом
"""

from aiogram import Dispatcher

from bot.handlers.commands import commands_router
from bot.handlers.messages import messages_router
from bot.middlewares.typing_middleware import TypingMiddleware
from bot.middlewares.rate_limit_middleware import RateLimitMiddleware


def create_dispatcher() -> Dispatcher:
    """Собирает и возвращает настроенный Dispatcher."""
    dp = Dispatcher()

    # ── Middleware ────────────────────────────────────────────────────────────
    # OUTER middleware — выполняется самым первым, до фильтров и handlers.
    # Именно здесь должна быть защита от спама: мы не тратим ресурсы
    # на фильтрацию и AI, если пользователь превысил лимит.
    dp.message.outer_middleware(RateLimitMiddleware())

    # INNER middleware — выполняется прямо перед handler-ом.
    # Typing-индикатор нужен только для сообщений, прошедших rate limit.
    dp.message.middleware(TypingMiddleware())

    # ── Роутеры ──────────────────────────────────────────────────────────────
    # Команды (выше приоритет — обрабатываются первыми)
    dp.include_router(commands_router)
    # Обычные текстовые сообщения
    dp.include_router(messages_router)

    return dp