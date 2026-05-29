"""
main.py — Точка входа приложения.

Запуск: python main.py

Отвечает только за:
  — Настройку логирования.
  — Инициализацию Bot и AIService.
  — Запуск polling.

Вся сборка роутеров и middleware — в bot/__init__.py.
"""

from __future__ import annotations

import asyncio
import logging
import sys

from aiogram import Bot
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode

from bot import create_dispatcher
from core.config import settings
from core.logging import setup_logging
from services.ai_service import init_ai_service

logger = logging.getLogger(__name__)


async def main() -> None:
    setup_logging()

    logger.info("=" * 60)
    logger.info("Starting bot «Центр Красок #1»")
    logger.info("=" * 60)

    # ── Инициализация Bot ─────────────────────────────────────────────────────
    bot = Bot(
        token=settings.bot_token.get_secret_value(),
        default=DefaultBotProperties(parse_mode=ParseMode.MARKDOWN),
    )
    bot_info = await bot.get_me()
    logger.info(
        "Bot connected | id=%d | username=@%s | name=%s",
        bot_info.id,
        bot_info.username,
        bot_info.full_name,
    )

    # ── Инициализация AI-сервиса ──────────────────────────────────────────────
    try:
        await init_ai_service()
    except Exception as exc:
        logger.critical(
            "FATAL: Failed to initialize AI service | %s: %s",
            type(exc).__name__, exc,
            exc_info=True,
        )
        sys.exit(1)

    # ── Сборка диспетчера ─────────────────────────────────────────────────────
    dp = create_dispatcher()
    logger.info("Dispatcher configured with all routers and middlewares")

    # ── Сброс накопленных обновлений ──────────────────────────────────────────
    await bot.delete_webhook(drop_pending_updates=True)
    logger.info("Webhook cleared, pending updates dropped")

    # ── Polling ───────────────────────────────────────────────────────────────
    logger.info("Polling started. Waiting for messages...")
    try:
        await dp.start_polling(
            bot,
            allowed_updates=["message", "callback_query"],
        )
    except (KeyboardInterrupt, SystemExit):
        logger.info("Received shutdown signal")
    except Exception as exc:
        logger.critical("Polling crashed | %s: %s", type(exc).__name__, exc, exc_info=True)
        raise
    finally:
        await bot.session.close()
        logger.info("Bot session closed. Shutdown complete.")


if __name__ == "__main__":
    asyncio.run(main())