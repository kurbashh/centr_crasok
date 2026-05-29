"""
services/ai_service.py — AI-сервис на базе Google Gemini.

Библиотека: google-genai (новый официальный SDK)
  ✓  from google import genai
  ✓  from google.genai import types
  ✗  google-generativeai  ← старый, устаревший пакет

Ключевые архитектурные решения:
  - Инициализация через classmethod create() — один раз при старте бота.
  - knowledge_base.md читается один раз и вшивается в system_instruction.
  - История конвертируется в types.Content[] с ролями "user" / "model".
  - Нативный async через client.aio.models.generate_content().
  - Три кастомных исключения для точечной обработки в handler-е.
"""

from __future__ import annotations

import logging
from pathlib import Path

from google import genai
from google.genai import types

from core.config import settings

logger = logging.getLogger(__name__)


# ──────────────────────────────────────────────────────────────────────────────
# Кастомные исключения
# ──────────────────────────────────────────────────────────────────────────────

class AIServiceError(Exception):
    """Базовое исключение AI-сервиса. Перехватывается в handler-е."""

class AIRateLimitError(AIServiceError):
    """Превышен лимит запросов к Gemini API (HTTP 429 / RESOURCE_EXHAUSTED)."""

class AIConnectionError(AIServiceError):
    """Ошибка соединения с Gemini API."""

class AIResponseError(AIServiceError):
    """Непредвиденная ошибка при генерации ответа."""


# ──────────────────────────────────────────────────────────────────────────────
# Логика сервиса
# ──────────────────────────────────────────────────────────────────────────────

class AIService:
    def __init__(self, client: genai.Client, system_instruction: str):
        self.client = client
        self.system_instruction = system_instruction
        self.model_name = "gemini-2.5-flash"  # Укажите нужную модель

    @classmethod
    async def create(cls) -> 'AIService':
        """Асинхронная фабрика для создания и настройки сервиса."""
        logger.info("Инициализация AIService и чтение базы знаний...")
        
        # Читаем базу знаний
        kb_path = Path("data/knowledge_base.md")
        base_knowledge = kb_path.read_text(encoding="utf-8") if kb_path.exists() else "Вы — помощник компании."
        
        # Добавляем четкую инструкцию по отклонению вопросов не связанных с компанией
        off_topic_guard = """

---

## ВАЖНОЕ ПРАВИЛО: Защита от вопросов вне компетенции

Ты помощник компании Центр Красок #1. Отвечай ТОЛЬКО на вопросы, связанные с лакокрасочными материалами, грунтовкой, штукатуркой, колеровкой и услугами компании.

ЗАПРЕЩЕНО отвечать на:
- Программирование, IT, веб-разработка
- Кулинария и рецепты
- Экономика, политика, новости
- Медицина и здоровье
- Образование, домашние задания
- Ремонт и строительство в целом (только краска/материалы компании)
- Любые другие темы

Если вопрос не связан с компанией или вашими услугами, ответь вежливо:
"Спасибо за вопрос. Я специализируюсь только на лакокрасочных и декоративных материалах. Эта тема выходит за рамки моей компетенции. Если есть вопросы о краске, штукатурке или других материалах от нас, помогу с удовольствием."

ДОПУСТИМЫЕ ВОПРОСЫ:
- Выбор краски для конкретного помещения
- Расчет расхода материалов
- Колеровка и оттенки
- Декоративные штукатурки и их эффекты
- Технические свойства материалов (VOC, сушка, адгезия)
- Бренды и доступные материалы
- Услуги компании и контакты
"""
        
        system_instruction = base_knowledge + off_topic_guard
        
        # Инициализируем клиент
        client = genai.Client(api_key=settings.gemini_api_key.get_secret_value())
        
        return cls(client=client, system_instruction=system_instruction)

    async def generate_response(self, user_id: int, history: list) -> str:
        """Генерация ответа через нейросеть."""
        try:
            # Преобразуем историю в формат types.Content для google-genai
            contents = []
            for msg in history:
                # Адаптируйте под структуру словарей из вашего context_service
                role = "user" if msg["role"] == "user" else "model"
                contents.append(
                    types.Content(role=role, parts=[types.Part.from_text(text=msg["content"])])
                )
            
            # Добавляем системный промпт через конфигурацию
            config = types.GenerateContentConfig(
                system_instruction=self.system_instruction,
                temperature=0.7,
            )

            # Выполняем асинхронный вызов
            response = await self.client.aio.models.generate_content(
                model=self.model_name,
                contents=contents,
                config=config
            )
            
            if response.text:
                return response.text
            return "Извините, не удалось сгенерировать ответ."

        except AIServiceError:
            # Перепробрасываем наши же исключения без изменений
            raise

        except Exception as exc:
            exc_str = str(exc).lower()

            # Rate limit — детектируем по коду и тексту ошибки,
            # так как google-genai может не всегда бросать специализированный тип
            if any(k in exc_str for k in ("429", "quota", "resource_exhausted", "rate")):
                logger.warning("RateLimit | user_id=%s | %s", user_id, exc)
                raise AIRateLimitError(
                    "Превышен лимит запросов Gemini API."
                ) from exc

            # Сетевые ошибки
            if any(k in exc_str for k in ("connect", "timeout", "network", "ssl", "unavailable")):
                logger.error("ConnectionError | user_id=%s | %s", user_id, exc)
                raise AIConnectionError(
                    "Нет соединения с Gemini API."
                ) from exc

            # Всё остальное
            logger.exception("UnknownError | user_id=%s", user_id)
            raise AIResponseError(f"Непредвиденная ошибка Gemini API: {exc}") from exc


# ──────────────────────────────────────────────────────────────────────────────
# Singleton — инициализируется в bot/main.py через await init_ai_service()
# ──────────────────────────────────────────────────────────────────────────────

ai_service: AIService | None = None

async def init_ai_service() -> None:
    """Создает экземпляр AIService при запуске программы в main.py."""
    global ai_service
    ai_service = await AIService.create()
    logger.info("AI Service успешно запущен и готов к работе.")