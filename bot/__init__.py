"""
bot/__init__.py — Фабрика Dispatcher.

Централизованное место для регистрации всех роутеров и middleware.
bot.py импортирует только create_dispatcher() и не знает деталей сборки.

Порядок middleware (важен!):
  1. RateLimitMiddleware (outer) — первый барьер, отсекает спам до всего
  2. TypingMiddleware    (inner) — запускается прямо перед handler-ом

FSM:
  Используем MemoryStorage — данные живут в RAM (достаточно для /calc).
  При необходимости масштабирования замените на RedisStorage.
"""

from aiogram import Dispatcher
from aiogram.fsm.storage.memory import MemoryStorage

from bot.handlers.commands import commands_router
from bot.handlers.messages import messages_router
from bot.handlers.calc import calc_router
from bot.middlewares.typing_middleware import TypingMiddleware
from bot.middlewares.rate_limit_middleware import RateLimitMiddleware


def create_dispatcher() -> Dispatcher:
    """Собирает и возвращает настроенный Dispatcher."""
    # FSM storage — in-memory (для /calc пошагового диалога)
    dp = Dispatcher(storage=MemoryStorage())

    # ── Middleware ────────────────────────────────────────────────────────────
    dp.message.outer_middleware(RateLimitMiddleware())
    dp.message.middleware(TypingMiddleware())

    # ── Роутеры ──────────────────────────────────────────────────────────────
    # Порядок важен: команды и калькулятор — первыми, общие сообщения — последними
    dp.include_router(commands_router)
    dp.include_router(calc_router)
    dp.include_router(messages_router)

    return dp