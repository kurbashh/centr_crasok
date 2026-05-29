"""
main.py — Точка входа приложения.

Запуск: python main.py
Отвечает только за инициализацию Bot и запуск polling.
Вся сборка роутеров и middleware — в bot/__init__.py.
"""

from __future__ import annotations

import asyncio
import logging

from aiogram import Bot
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode

from bot import create_dispatcher
from core.config import settings
from core.logging import setup_logging

# ПРАВИЛЬНЫЙ ИМПОРТ: Забираем только функцию инициализации, 
# чтобы избежать циклических зависимостей
from services.ai_service import init_ai_service


async def main() -> None:
    setup_logging()
    logger = logging.getLogger(__name__)
    logger.info("Запуск бота «Центр Красок #1»…")

    bot = Bot(
        token=settings.bot_token.get_secret_value(),
        default=DefaultBotProperties(parse_mode=ParseMode.MARKDOWN),
    )

    # --- ВАЖНО: Инициализируем AI-сервис ДО запуска диспетчера ---
    await init_ai_service()

    dp = create_dispatcher()

    # Сброс webhook и накопленных обновлений перед стартом
    await bot.delete_webhook(drop_pending_updates=True)
    logger.info("Polling запущен. Ожидание сообщений…")

    try:
        await dp.start_polling(bot, allowed_updates=["message"])
    finally:
        await bot.session.close()
        logger.info("Бот остановлен.")


if __name__ == "__main__":
    asyncio.run(main())