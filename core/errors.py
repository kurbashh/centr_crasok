"""
core/errors.py — Централизованный реестр ошибок приложения.

Архитектура:
  — Все ошибки описаны в одном месте (ErrorCode enum + ERROR_CATALOG).
  — Каждая ошибка имеет: код, HTTP-аналог, пользовательское сообщение, лог-уровень.
  — AppError несёт контекст (user_id, extra) для структурированного логирования.
  — Handler-ы не формулируют текст ошибок — они вызывают AppError.from_code().

Принцип:
  «Пользователь видит понятное сообщение, разработчик видит полный контекст.»
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Any


# ──────────────────────────────────────────────────────────────────────────────
# Коды ошибок
# ──────────────────────────────────────────────────────────────────────────────

class ErrorCode(Enum):
    """
    Каталог кодов ошибок приложения.

    Группы:
      1xx — Ошибки входных данных (аналог HTTP 4xx)
      2xx — Ошибки AI-сервиса
      3xx — Ошибки инфраструктуры / сети
      4xx — Ошибки Rate Limiting
      5xx — Внутренние ошибки приложения
    """

    # ── Входные данные ────────────────────────────────────────────────────────
    EMPTY_MESSAGE        = 100  # Пользователь отправил пустую строку
    NON_TEXT_MESSAGE     = 101  # Стикер, фото, голосовое — не текст
    MESSAGE_TOO_LONG     = 102  # Сообщение превышает допустимый лимит символов

    # ── AI-сервис ─────────────────────────────────────────────────────────────
    AI_NOT_INITIALIZED   = 200  # Сервис не был инициализирован при старте
    AI_RATE_LIMIT        = 201  # Gemini API вернул 429 / RESOURCE_EXHAUSTED
    AI_EMPTY_RESPONSE    = 202  # Gemini вернул пустой ответ (блок content)
    AI_SAFETY_BLOCK      = 203  # Gemini заблокировал запрос по safety-фильтрам
    AI_CONTEXT_OVERFLOW  = 204  # История диалога слишком длинная для модели
    AI_UNKNOWN           = 299  # Непредвиденная ошибка Gemini API

    # ── Сеть / Инфраструктура ─────────────────────────────────────────────────
    NETWORK_TIMEOUT      = 300  # Timeout при запросе к внешнему сервису
    NETWORK_CONNECTION   = 301  # Нет соединения с внешним сервисом
    TELEGRAM_SEND_FAIL   = 302  # Ошибка отправки сообщения через Telegram API
    TELEGRAM_PARSE_ERROR = 303  # Ошибка разбора Markdown/HTML в Telegram

    # ── Rate Limiting ──────────────────────────────────────────────────────────
    RATE_LIMIT_BURST     = 400  # Превышен burst-лимит (слишком быстро)
    RATE_LIMIT_WINDOW    = 401  # Превышен лимит за скользящее окно

    # ── Внутренние ────────────────────────────────────────────────────────────
    CONTEXT_SAVE_FAIL    = 500  # Не удалось сохранить сообщение в истории
    INTERNAL_UNKNOWN     = 599  # Непредвиденная внутренняя ошибка


# ──────────────────────────────────────────────────────────────────────────────
# Описания ошибок
# ──────────────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class ErrorDescriptor:
    """
    Полное описание ошибки.

    Поля:
      code           — числовой код (из ErrorCode.value)
      user_message   — текст для пользователя в Telegram (понятный, без технических деталей)
      log_message    — шаблон для логов (может содержать {placeholders})
      log_level      — уровень логирования (logging.WARNING, logging.ERROR, etc.)
      recoverable    — можно ли попробовать снова (влияет на UX-подсказки)
    """
    code: int
    user_message: str
    log_message: str
    log_level: int = logging.ERROR
    recoverable: bool = True


# Каталог всех ошибок приложения
ERROR_CATALOG: dict[ErrorCode, ErrorDescriptor] = {

    # ── Входные данные ────────────────────────────────────────────────────────
    ErrorCode.EMPTY_MESSAGE: ErrorDescriptor(
        code=100,
        user_message=(
            "Пожалуйста, напишите ваш вопрос о краске, грунтовке или материалах для отделки."
        ),
        log_message="Empty message received",
        log_level=logging.DEBUG,
        recoverable=True,
    ),

    ErrorCode.NON_TEXT_MESSAGE: ErrorDescriptor(
        code=101,
        user_message=(
            "Я работаю только с текстовыми вопросами.\n"
            "Пожалуйста, напишите свой вопрос текстом."
        ),
        log_message="Non-text message received (type={content_type})",
        log_level=logging.INFO,
        recoverable=True,
    ),

    ErrorCode.MESSAGE_TOO_LONG: ErrorDescriptor(
        code=102,
        user_message=(
            "Сообщение слишком длинное. Пожалуйста, разбейте вопрос на несколько частей."
        ),
        log_message="Message too long: {length} chars (limit={limit})",
        log_level=logging.WARNING,
        recoverable=True,
    ),

    # ── AI-сервис ─────────────────────────────────────────────────────────────
    ErrorCode.AI_NOT_INITIALIZED: ErrorDescriptor(
        code=200,
        user_message=(
            "Сервис временно недоступен. Мы уже работаем над устранением проблемы.\n"
            "Попробуйте позже или обратитесь напрямую: +7 778 061 5000"
        ),
        log_message="AI service is not initialized — init_ai_service() was not called",
        log_level=logging.CRITICAL,
        recoverable=False,
    ),

    ErrorCode.AI_RATE_LIMIT: ErrorDescriptor(
        code=201,
        user_message=(
            "AI-сервис временно перегружен запросами. "
            "Пожалуйста, повторите через несколько секунд."
        ),
        log_message="Gemini API rate limit hit (429/RESOURCE_EXHAUSTED): {detail}",
        log_level=logging.WARNING,
        recoverable=True,
    ),

    ErrorCode.AI_EMPTY_RESPONSE: ErrorDescriptor(
        code=202,
        user_message=(
            "Не удалось получить ответ. Попробуйте переформулировать вопрос."
        ),
        log_message="Gemini returned empty response (no text content): {detail}",
        log_level=logging.WARNING,
        recoverable=True,
    ),

    ErrorCode.AI_SAFETY_BLOCK: ErrorDescriptor(
        code=203,
        user_message=(
            "Запрос не прошёл проверку безопасности. "
            "Пожалуйста, переформулируйте вопрос."
        ),
        log_message="Gemini blocked request by safety filters: {detail}",
        log_level=logging.WARNING,
        recoverable=True,
    ),

    ErrorCode.AI_CONTEXT_OVERFLOW: ErrorDescriptor(
        code=204,
        user_message=(
            "История диалога слишком длинная. "
            "Попробуйте начать новый диалог командой /start."
        ),
        log_message="Context overflow: history has {size} messages, model limit exceeded",
        log_level=logging.WARNING,
        recoverable=True,
    ),

    ErrorCode.AI_UNKNOWN: ErrorDescriptor(
        code=299,
        user_message=(
            "Произошла ошибка при обработке запроса. "
            "Попробуйте позже или напишите нам: info@centr-krasok.kz"
        ),
        log_message="Unexpected Gemini API error: {detail}",
        log_level=logging.ERROR,
        recoverable=False,
    ),

    # ── Сеть / Инфраструктура ─────────────────────────────────────────────────
    ErrorCode.NETWORK_TIMEOUT: ErrorDescriptor(
        code=300,
        user_message=(
            "Запрос выполняется слишком долго. Повторите попытку."
        ),
        log_message="Request timeout after {timeout}s: {detail}",
        log_level=logging.WARNING,
        recoverable=True,
    ),

    ErrorCode.NETWORK_CONNECTION: ErrorDescriptor(
        code=301,
        user_message=(
            "Нет связи с AI-сервисом. Пожалуйста, повторите через несколько секунд."
        ),
        log_message="Connection error to Gemini API: {detail}",
        log_level=logging.ERROR,
        recoverable=True,
    ),

    ErrorCode.TELEGRAM_SEND_FAIL: ErrorDescriptor(
        code=302,
        user_message=None,  # type: ignore[arg-type]  # Молчим — пользователь не получит сообщение
        log_message="Failed to send Telegram message to chat_id={chat_id}: {detail}",
        log_level=logging.ERROR,
        recoverable=False,
    ),

    ErrorCode.TELEGRAM_PARSE_ERROR: ErrorDescriptor(
        code=303,
        user_message=None,  # type: ignore[arg-type]  # Ретрай без parse_mode
        log_message="Telegram rejected parse_mode={parse_mode}: {detail}",
        log_level=logging.WARNING,
        recoverable=True,
    ),

    # ── Rate Limiting ──────────────────────────────────────────────────────────
    ErrorCode.RATE_LIMIT_BURST: ErrorDescriptor(
        code=400,
        user_message=(
            "Пожалуйста, чуть помедленнее — я ещё обрабатываю предыдущие вопросы.\n"
            "Подождите {wait_sec} сек. и повторите."
        ),
        log_message="Burst limit hit: {count} req in {window}s (limit={limit})",
        log_level=logging.WARNING,
        recoverable=True,
    ),

    ErrorCode.RATE_LIMIT_WINDOW: ErrorDescriptor(
        code=401,
        user_message=(
            "Вы отправили много сообщений за короткое время.\n"
            "Подождите {wait_sec} секунд, затем продолжим."
        ),
        log_message="Window limit hit: {count}/{limit} req in {window}s, retry_after={retry_after:.1f}s",
        log_level=logging.WARNING,
        recoverable=True,
    ),

    # ── Внутренние ────────────────────────────────────────────────────────────
    ErrorCode.CONTEXT_SAVE_FAIL: ErrorDescriptor(
        code=500,
        user_message=(
            "Произошла внутренняя ошибка. Попробуйте ещё раз."
        ),
        log_message="Failed to save message to context store: {detail}",
        log_level=logging.ERROR,
        recoverable=True,
    ),

    ErrorCode.INTERNAL_UNKNOWN: ErrorDescriptor(
        code=599,
        user_message=(
            "Произошла непредвиденная ошибка. "
            "Попробуйте позже или напишите нам: info@centr-krasok.kz"
        ),
        log_message="Unhandled exception in handler: {detail}",
        log_level=logging.CRITICAL,
        recoverable=False,
    ),
}


# ──────────────────────────────────────────────────────────────────────────────
# Базовый класс исключений приложения
# ──────────────────────────────────────────────────────────────────────────────

@dataclass
class AppError(Exception):
    """
    Базовое исключение приложения с полным контекстом для логирования.

    Использование:
        raise AppError(
            code=ErrorCode.AI_RATE_LIMIT,
            user_id=123,
            extra={"detail": str(exc), "model": "gemini-2.5-flash"},
        )

    Handler-ы перехватывают AppError и:
      1. Форматируют user_message из ERROR_CATALOG (с подстановкой extra).
      2. Логируют log_message с нужным уровнем.
    """

    code: ErrorCode
    user_id: int | None = None
    extra: dict[str, Any] = field(default_factory=dict)
    cause: BaseException | None = None  # Оригинальное исключение (для цепочки)

    def __post_init__(self) -> None:
        descriptor = self.descriptor
        super().__init__(
            f"[E{descriptor.code}] {self.code.name}"
            + (f" | user_id={self.user_id}" if self.user_id else "")
            + (f" | {self.extra}" if self.extra else "")
        )

    @property
    def descriptor(self) -> ErrorDescriptor:
        return ERROR_CATALOG[self.code]

    @property
    def user_message(self) -> str:
        """Возвращает пользовательское сообщение с подставленными значениями."""
        template = self.descriptor.user_message or ""
        try:
            return template.format(**self.extra)
        except KeyError:
            return template  # Если не все переменные переданы — возвращаем как есть

    @property
    def log_message(self) -> str:
        """Возвращает лог-сообщение с подставленными значениями."""
        template = self.descriptor.log_message
        try:
            return template.format(**self.extra)
        except KeyError:
            return f"{template} | raw_extra={self.extra}"

    @property
    def log_level(self) -> int:
        return self.descriptor.log_level

    @property
    def recoverable(self) -> bool:
        return self.descriptor.recoverable

    def log(self, logger: logging.Logger) -> None:
        """
        Логирует ошибку с правильным уровнем.
        Для CRITICAL/ERROR добавляет stack trace если есть cause.
        """
        msg = f"[E{self.descriptor.code}] {self.code.name} | {self.log_message}"
        if self.user_id:
            msg = f"user_id={self.user_id} | {msg}"

        if self.log_level >= logging.ERROR and self.cause:
            logger.log(self.log_level, msg, exc_info=self.cause)
        else:
            logger.log(self.log_level, msg)


# ──────────────────────────────────────────────────────────────────────────────
# Специализированные исключения для AI-сервиса
# ──────────────────────────────────────────────────────────────────────────────

class AIServiceError(AppError):
    """Базовый класс для ошибок AI-сервиса."""


class AIRateLimitError(AIServiceError):
    """Gemini API вернул 429 / RESOURCE_EXHAUSTED."""

    def __init__(self, user_id: int | None = None, detail: str = "", cause: BaseException | None = None):
        super().__init__(
            code=ErrorCode.AI_RATE_LIMIT,
            user_id=user_id,
            extra={"detail": detail},
            cause=cause,
        )


class AIConnectionError(AIServiceError):
    """Ошибка сетевого соединения с Gemini API."""

    def __init__(self, user_id: int | None = None, detail: str = "", cause: BaseException | None = None):
        super().__init__(
            code=ErrorCode.NETWORK_CONNECTION,
            user_id=user_id,
            extra={"detail": detail},
            cause=cause,
        )


class AITimeoutError(AIServiceError):
    """Timeout при запросе к Gemini API."""

    def __init__(self, user_id: int | None = None, timeout: float = 0, cause: BaseException | None = None):
        super().__init__(
            code=ErrorCode.NETWORK_TIMEOUT,
            user_id=user_id,
            extra={"timeout": timeout, "detail": str(cause) if cause else ""},
            cause=cause,
        )


class AIEmptyResponseError(AIServiceError):
    """Gemini вернул пустой ответ."""

    def __init__(self, user_id: int | None = None, detail: str = ""):
        super().__init__(
            code=ErrorCode.AI_EMPTY_RESPONSE,
            user_id=user_id,
            extra={"detail": detail},
        )


class AISafetyBlockError(AIServiceError):
    """Gemini заблокировал запрос по safety-фильтрам."""

    def __init__(self, user_id: int | None = None, detail: str = ""):
        super().__init__(
            code=ErrorCode.AI_SAFETY_BLOCK,
            user_id=user_id,
            extra={"detail": detail},
        )


class AIResponseError(AIServiceError):
    """Непредвиденная ошибка Gemini API."""

    def __init__(self, user_id: int | None = None, detail: str = "", cause: BaseException | None = None):
        super().__init__(
            code=ErrorCode.AI_UNKNOWN,
            user_id=user_id,
            extra={"detail": detail},
            cause=cause,
        )