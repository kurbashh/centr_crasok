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

from services.ai_service import AIService
import services.ai_service


async def main() -> None:
    setup_logging()
    logger = logging.getLogger(__name__)
    logger.info("Запуск бота «Центр Красок #1»…")

    bot = Bot(
        token=settings.bot_token.get_secret_value(),
        default=DefaultBotProperties(parse_mode=ParseMode.MARKDOWN),
    )

    dp = create_dispatcher()

    services.ai_service.ai_service = await AIService.create()

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