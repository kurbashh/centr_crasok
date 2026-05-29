"""
core/logging.py — Централизованная настройка структурированного логирования.

Возможности:
  — Единый формат для всего приложения.
  — RequestContext — позволяет прокидывать user_id/chat_id через стек вызовов
    без явной передачи в каждую функцию (через contextvars).
  — log_flow() — декоратор/контекстный менеджер для логирования входа/выхода
    из операции с автоматическим замером времени.
  — Цветной вывод в stderr для dev-режима.
  — Подавление шума от внешних библиотек.

Использование:
    from core.logging import setup_logging, RequestContext, log_flow

    # Старт приложения
    setup_logging()

    # Установка контекста в middleware (один раз на запрос)
    RequestContext.set(user_id=123, chat_id=456)

    # Лог с контекстом
    logger.info("Processing message")  # → "user_id=123 | Processing message"

    # Замер времени операции
    async with log_flow(logger, "generate_ai_response"):
        reply = await ai_service.generate(...)
"""

from __future__ import annotations

import logging
import sys
import time
import asyncio
from contextlib import asynccontextmanager, contextmanager
from contextvars import ContextVar
from typing import AsyncGenerator, Generator


# ──────────────────────────────────────────────────────────────────────────────
# Контекст запроса (propagated через contextvars)
# ──────────────────────────────────────────────────────────────────────────────

_ctx_user_id: ContextVar[int | None] = ContextVar("user_id", default=None)
_ctx_chat_id: ContextVar[int | None] = ContextVar("chat_id", default=None)
_ctx_request_id: ContextVar[str | None] = ContextVar("request_id", default=None)


class RequestContext:
    """
    Хранит контекст текущего запроса в contextvars.
    Не требует передачи user_id по всем функциям.

    Пример установки в middleware:
        token = RequestContext.set(user_id=msg.from_user.id, chat_id=msg.chat.id)
        try:
            await handler(event, data)
        finally:
            RequestContext.reset(token)
    """

    @classmethod
    def set(
        cls,
        user_id: int | None = None,
        chat_id: int | None = None,
        request_id: str | None = None,
    ) -> dict:
        """Устанавливает контекст. Возвращает токены для сброса."""
        tokens = {
            "user_id": _ctx_user_id.set(user_id) if user_id is not None else None,
            "chat_id": _ctx_chat_id.set(chat_id) if chat_id is not None else None,
            "request_id": _ctx_request_id.set(request_id) if request_id is not None else None,
        }
        return tokens

    @classmethod
    def reset(cls, tokens: dict) -> None:
        """Сбрасывает контекст до предыдущих значений."""
        for var, token in [
            (_ctx_user_id, tokens.get("user_id")),
            (_ctx_chat_id, tokens.get("chat_id")),
            (_ctx_request_id, tokens.get("request_id")),
        ]:
            if token is not None:
                var.reset(token)

    @classmethod
    def get_user_id(cls) -> int | None:
        return _ctx_user_id.get()

    @classmethod
    def get_chat_id(cls) -> int | None:
        return _ctx_chat_id.get()

    @classmethod
    def get_request_id(cls) -> str | None:
        return _ctx_request_id.get()

    @classmethod
    def as_prefix(cls) -> str:
        """Возвращает строку-префикс для логов: 'user_id=123 | '"""
        parts = []
        if uid := _ctx_user_id.get():
            parts.append(f"user_id={uid}")
        if cid := _ctx_chat_id.get():
            parts.append(f"chat_id={cid}")
        return " | ".join(parts) + " | " if parts else ""


# ──────────────────────────────────────────────────────────────────────────────
# Форматтер с контекстом запроса
# ──────────────────────────────────────────────────────────────────────────────

class ContextFormatter(logging.Formatter):
    """
    Форматтер, автоматически добавляющий контекст запроса (user_id/chat_id)
    к каждой строке лога.

    Формат:
        2024-01-15 12:34:56 | INFO     | bot.handlers.messages | user_id=123 | Processing message
    """

    # ANSI-цвета для dev-режима (терминал)
    _COLORS = {
        logging.DEBUG:    "\033[36m",   # cyan
        logging.INFO:     "\033[32m",   # green
        logging.WARNING:  "\033[33m",   # yellow
        logging.ERROR:    "\033[31m",   # red
        logging.CRITICAL: "\033[35m",   # magenta
    }
    _RESET = "\033[0m"

    def __init__(self, use_colors: bool = False, **kwargs):
        super().__init__(**kwargs)
        self._use_colors = use_colors

    def format(self, record: logging.LogRecord) -> str:
        # Добавляем контекст запроса в сообщение
        ctx_prefix = RequestContext.as_prefix()
        if ctx_prefix:
            record.msg = ctx_prefix + str(record.msg)

        formatted = super().format(record)

        if self._use_colors:
            color = self._COLORS.get(record.levelno, "")
            level_start = formatted.find(record.levelname)
            level_end = level_start + len(record.levelname)
            formatted = (
                formatted[:level_start]
                + color
                + formatted[level_start:level_end]
                + self._RESET
                + formatted[level_end:]
            )

        return formatted


# ──────────────────────────────────────────────────────────────────────────────
# Утилиты для логирования потока выполнения
# ──────────────────────────────────────────────────────────────────────────────

@asynccontextmanager
async def log_flow(
    logger: logging.Logger,
    operation: str,
    level: int = logging.DEBUG,
    **extra,
) -> AsyncGenerator[None, None]:
    """
    Async контекстный менеджер: логирует начало и конец операции + время выполнения.

    Использование:
        async with log_flow(logger, "gemini_generate", model="gemini-2.5-flash"):
            response = await client.generate(...)

    Вывод:
        DEBUG | → gemini_generate | model=gemini-2.5-flash
        DEBUG | ✓ gemini_generate | 1234ms | model=gemini-2.5-flash
        # При ошибке:
        ERROR | ✗ gemini_generate | 567ms | error=<type> | model=gemini-2.5-flash
    """
    extra_str = " | ".join(f"{k}={v}" for k, v in extra.items())
    prefix = f" | {extra_str}" if extra_str else ""

    logger.log(level, "→ %s%s", operation, prefix)
    start = time.perf_counter()

    try:
        yield
        elapsed_ms = int((time.perf_counter() - start) * 1000)
        logger.log(level, "✓ %s | %dms%s", operation, elapsed_ms, prefix)

    except Exception as exc:
        elapsed_ms = int((time.perf_counter() - start) * 1000)
        logger.error(
            "✗ %s | %dms | error=%s(%s)%s",
            operation,
            elapsed_ms,
            type(exc).__name__,
            str(exc)[:120],
            prefix,
        )
        raise


@contextmanager
def log_flow_sync(
    logger: logging.Logger,
    operation: str,
    level: int = logging.DEBUG,
    **extra,
) -> Generator[None, None, None]:
    """Синхронная версия log_flow для не-async контекстов."""
    extra_str = " | ".join(f"{k}={v}" for k, v in extra.items())
    prefix = f" | {extra_str}" if extra_str else ""

    logger.log(level, "→ %s%s", operation, prefix)
    start = time.perf_counter()

    try:
        yield
        elapsed_ms = int((time.perf_counter() - start) * 1000)
        logger.log(level, "✓ %s | %dms%s", operation, elapsed_ms, prefix)

    except Exception as exc:
        elapsed_ms = int((time.perf_counter() - start) * 1000)
        logger.error(
            "✗ %s | %dms | error=%s%s",
            operation,
            elapsed_ms,
            type(exc).__name__,
            prefix,
        )
        raise


# ──────────────────────────────────────────────────────────────────────────────
# Инициализация
# ──────────────────────────────────────────────────────────────────────────────

def setup_logging(
    level: int = logging.INFO,
    use_colors: bool | None = None,
) -> None:
    """
    Настраивает root-логгер для всего приложения.

    Вызвать один раз при старте приложения.
    После этого везде достаточно logging.getLogger(__name__).

    Args:
        level:       Уровень логирования (default: INFO).
        use_colors:  Цветной вывод. None = авто (True если tty).
    """
    # Авто-определение цветного режима
    if use_colors is None:
        use_colors = sys.stdout.isatty()

    fmt = "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s"
    datefmt = "%Y-%m-%d %H:%M:%S"

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(
        ContextFormatter(use_colors=use_colors, fmt=fmt, datefmt=datefmt)
    )

    root = logging.getLogger()
    root.setLevel(level)
    root.handlers.clear()
    root.addHandler(handler)

    # ── Подавление шума внешних библиотек ────────────────────────────────────
    _noisy_loggers = {
        "httpx":           logging.WARNING,
        "httpcore":        logging.WARNING,
        "openai":          logging.WARNING,
        "google":          logging.WARNING,
        "google.genai":    logging.WARNING,
        "aiogram":         logging.WARNING,
        "asyncio":         logging.WARNING,
        "urllib3":         logging.WARNING,
    }
    for name, lvl in _noisy_loggers.items():
        logging.getLogger(name).setLevel(lvl)

    # ── Стартовое сообщение ───────────────────────────────────────────────────
    logger = logging.getLogger(__name__)
    logger.info(
        "Logging configured | level=%s | colors=%s",
        logging.getLevelName(level),
        use_colors,
    )