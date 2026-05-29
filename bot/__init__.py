"""
bot/__init__.py — Фабрика Dispatcher.

Централизованное место для регистрации всех роутеров и middleware.
bot.py импортирует только create_dispatcher() и не знает деталей сборки.
"""

from aiogram import Dispatcher

from bot.handlers.messages import messages_router
from bot.middlewares.typing_middleware import TypingMiddleware


def create_dispatcher() -> Dispatcher:
    """Собирает и возвращает настроенный Dispatcher."""
    dp = Dispatcher()

    # ── Middleware ────────────────────────────────────────────────────────────
    # inner middleware — выполняется прямо перед handler-ом
    dp.message.middleware(TypingMiddleware())

    # ── Роутеры ──────────────────────────────────────────────────────────────
    dp.include_router(messages_router)

    return dp