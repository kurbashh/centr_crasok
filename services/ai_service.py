"""
services/ai_service.py — AI-сервис на базе Google Gemini.

Библиотека: google-genai (официальный SDK, GA с мая 2025)
  ✓  from google import genai
  ✓  from google.genai import types
  ✗  google-generativeai  ← старый, устаревший пакет

Ключевые архитектурные решения:
  — Инициализация через classmethod create() — один раз при старте бота.
  — knowledge_base.md читается один раз и вшивается в system_instruction.
  — История конвертируется в types.Content[] с ролями "user" / "model".
  — Нативный async через client.aio.models.generate_content().
  — Все исключения — из core.errors (с кодами, user_message, log_level).
  — log_flow() замеряет время каждого вызова к Gemini API.

Матрица ошибок:
  HTTP 429 / RESOURCE_EXHAUSTED → AIRateLimitError    [E201]
  connect/timeout/ssl/network   → AIConnectionError   [E301]
  timeout                       → AITimeoutError      [E300]
  safety block                  → AISafetyBlockError  [E203]
  empty response                → AIEmptyResponseError[E202]
  всё остальное                 → AIResponseError     [E299]
"""

from __future__ import annotations

import logging
from pathlib import Path

from google import genai
from google.genai import types

from core.config import settings
from core.errors import (
    AIRateLimitError,
    AIConnectionError,
    AITimeoutError,
    AIEmptyResponseError,
    AISafetyBlockError,
    AIResponseError,
    AppError,
)
from core.logging import log_flow

logger = logging.getLogger(__name__)


# ──────────────────────────────────────────────────────────────────────────────
# Логика сервиса
# ──────────────────────────────────────────────────────────────────────────────

class AIService:
    """
    Инкапсулирует взаимодействие с Gemini API.

    Инициализируется через await AIService.create() один раз при старте.
    Все публичные методы — async.
    """

    def __init__(self, client: genai.Client, system_instruction: str) -> None:
        self._client = client
        self._system_instruction = system_instruction
        self._model_name = settings.gemini_model
        logger.info(
            "AIService ready | model=%s | system_prompt=%d chars",
            self._model_name,
            len(system_instruction),
        )

    # ── Фабрика ───────────────────────────────────────────────────────────────

    @classmethod
    async def create(cls) -> "AIService":
        """
        Асинхронная фабрика: читает базу знаний, инициализирует клиент.
        Вызывается один раз из main.py через await init_ai_service().
        """
        async with log_flow(logger, "AIService.create", level=logging.INFO):
            # Читаем базу знаний
            kb_path = Path(settings.knowledge_base_path)
            if kb_path.exists():
                base_knowledge = kb_path.read_text(encoding="utf-8")
                logger.info(
                    "Knowledge base loaded | path=%s | size=%d chars",
                    kb_path,
                    len(base_knowledge),
                )
            else:
                base_knowledge = "Вы — помощник компании Центр Красок #1."
                logger.warning(
                    "Knowledge base not found at %s — using fallback prompt", kb_path
                )

            system_instruction = _build_system_prompt(base_knowledge)

            client = genai.Client(
                api_key=settings.gemini_api_key.get_secret_value()
            )

        return cls(client=client, system_instruction=system_instruction)

    # ── Основной метод ────────────────────────────────────────────────────────

    async def generate_response(self, user_id: int, history: list) -> str:
        """
        Генерирует ответ на основе истории диалога.

        Args:
            user_id: ID пользователя (для логов и ошибок).
            history: Список сообщений [{"role": "user"|"assistant", "content": "..."}].

        Returns:
            Текст ответа от Gemini.

        Raises:
            AIRateLimitError:     Превышен лимит запросов (429).
            AIConnectionError:    Нет соединения с API.
            AITimeoutError:       Timeout запроса.
            AIEmptyResponseError: Gemini вернул пустой ответ.
            AISafetyBlockError:   Запрос заблокирован safety-фильтрами.
            AIResponseError:      Непредвиденная ошибка.
        """
        contents = _history_to_contents(history)
        config = types.GenerateContentConfig(
            system_instruction=self._system_instruction,
            temperature=settings.ai_temperature,
            max_output_tokens=settings.ai_max_tokens,
        )

        logger.info(
            "Sending request to Gemini | user_id=%d | model=%s | history_msgs=%d",
            user_id,
            self._model_name,
            len(history),
        )

        try:
            async with log_flow(
                logger,
                "gemini_generate_content",
                level=logging.INFO,
                model=self._model_name,
                user_id=user_id,
                history_len=len(history),
            ):
                response = await self._client.aio.models.generate_content(
                    model=self._model_name,
                    contents=contents,
                    config=config,
                )

        except AppError:
            # Наши собственные исключения пропускаем без изменений
            raise

        except Exception as exc:
            raise _classify_exception(exc, user_id) from exc

        # ── Разбор ответа ─────────────────────────────────────────────────────
        return _extract_text(response, user_id)


# ──────────────────────────────────────────────────────────────────────────────
# Вспомогательные функции (приватные)
# ──────────────────────────────────────────────────────────────────────────────

def _build_system_prompt(base_knowledge: str) -> str:
    """Добавляет off-topic guard к базе знаний."""
    off_topic_guard = """

---

## ВАЖНОЕ ПРАВИЛО: Защита от вопросов вне компетенции

Ты помощник компании Центр Красок #1. Отвечай ТОЛЬКО на вопросы, связанные
с лакокрасочными материалами, грунтовкой, штукатуркой, колеровкой и услугами компании.

ЗАПРЕЩЕНО отвечать на вопросы о программировании, кулинарии, медицине, политике,
образовании и любых темах, не связанных с красками и отделочными материалами.

Если вопрос выходит за рамки компетенции, вежливо ответь:
«Я специализируюсь только на лакокрасочных и декоративных материалах. \
Если есть вопросы о краске или штукатурке — помогу с удовольствием.»

ДОПУСТИМЫЕ ТЕМЫ: выбор краски, расчёт расхода, колеровка, декоративные штукатурки,
свойства материалов (VOC, адгезия, сушка), бренды компании, услуги, контакты.
"""
    return base_knowledge + off_topic_guard


def _history_to_contents(history: list) -> list[types.Content]:
    """Конвертирует историю диалога в формат Gemini types.Content."""
    contents = []
    for msg in history:
        role = "user" if msg["role"] == "user" else "model"
        contents.append(
            types.Content(
                role=role,
                parts=[types.Part.from_text(text=msg["content"])]
            )
        )
    return contents


def _classify_exception(exc: Exception, user_id: int) -> AppError:
    """
    Переводит «сырые» исключения Gemini SDK в типизированные AppError.

    Порядок проверок важен: более специфичные — первыми.
    """
    exc_str = str(exc).lower()
    exc_type = type(exc).__name__

    logger.debug(
        "Classifying exception | user_id=%d | type=%s | msg=%.200s",
        user_id, exc_type, exc_str,
    )

    # ── 1. Rate limit (429 / RESOURCE_EXHAUSTED) ──────────────────────────
    if any(k in exc_str for k in ("429", "quota", "resource_exhausted", "rateerror", "rate_limit")):
        logger.warning(
            "[E201] AI_RATE_LIMIT | user_id=%d | %s: %.200s",
            user_id, exc_type, exc_str,
        )
        return AIRateLimitError(user_id=user_id, detail=str(exc), cause=exc)

    # ── 2. Timeout ────────────────────────────────────────────────────────
    if any(k in exc_str for k in ("timeout", "timed out", "deadline")):
        logger.warning(
            "[E300] NETWORK_TIMEOUT | user_id=%d | %s: %.200s",
            user_id, exc_type, exc_str,
        )
        return AITimeoutError(user_id=user_id, timeout=0, cause=exc)

    # ── 3. Сетевые / инфраструктурные ────────────────────────────────────
    if any(k in exc_str for k in ("connect", "network", "ssl", "unavailable", "unreachable")):
        logger.error(
            "[E301] NETWORK_CONNECTION | user_id=%d | %s: %.200s",
            user_id, exc_type, exc_str,
        )
        return AIConnectionError(user_id=user_id, detail=str(exc), cause=exc)

    # ── 4. Safety block ───────────────────────────────────────────────────
    if any(k in exc_str for k in ("safety", "blocked", "finish_reason: safety", "harm_category")):
        logger.warning(
            "[E203] AI_SAFETY_BLOCK | user_id=%d | %s: %.200s",
            user_id, exc_type, exc_str,
        )
        return AISafetyBlockError(user_id=user_id, detail=str(exc))

    # ── 5. Всё остальное ──────────────────────────────────────────────────
    logger.error(
        "[E299] AI_UNKNOWN | user_id=%d | Unclassified exception | %s: %.300s",
        user_id, exc_type, exc_str,
        exc_info=exc,
    )
    return AIResponseError(user_id=user_id, detail=f"{exc_type}: {exc}", cause=exc)


def _extract_text(response, user_id: int) -> str:
    """
    Извлекает текст из ответа Gemini.
    Выбрасывает AIEmptyResponseError если ответ пустой.
    """
    # Проверяем finish_reason для раннего обнаружения safety-блока
    try:
        candidate = response.candidates[0] if response.candidates else None
        finish_reason = getattr(candidate, "finish_reason", None)
        if finish_reason and str(finish_reason).upper() in ("SAFETY", "RECITATION", "BLOCKED"):
            logger.warning(
                "[E203] AI_SAFETY_BLOCK | user_id=%d | finish_reason=%s",
                user_id, finish_reason,
            )
            raise AISafetyBlockError(
                user_id=user_id,
                detail=f"finish_reason={finish_reason}",
            )
    except (AttributeError, IndexError):
        pass

    text = getattr(response, "text", None)

    if not text or not text.strip():
        logger.warning(
            "[E202] AI_EMPTY_RESPONSE | user_id=%d | response has no text content",
            user_id,
        )
        raise AIEmptyResponseError(
            user_id=user_id,
            detail="response.text is empty or None",
        )

    logger.info(
        "Response generated | user_id=%d | response_len=%d chars",
        user_id,
        len(text),
    )
    return text


# ──────────────────────────────────────────────────────────────────────────────
# Singleton — инициализируется в main.py через await init_ai_service()
# ──────────────────────────────────────────────────────────────────────────────

ai_service: AIService | None = None


async def init_ai_service() -> None:
    """
    Создаёт и сохраняет singleton AIService.
    Вызывается один раз из main.py перед запуском диспетчера.
    """
    global ai_service

    logger.info("Initializing AI service...")
    ai_service = await AIService.create()
    logger.info(
        "AI service initialized successfully | model=%s",
        settings.gemini_model,
    )