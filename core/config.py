"""
core/config.py — Конфигурация приложения (pydantic-settings).

AI-движок: google-genai (официальный SDK Google, GA с мая 2025).
Документация: https://ai.google.dev/gemini-api/docs/libraries

Принцип разделения полей:
  ├── БЕЗ дефолта, тип SecretStr  → секрет, ОБЯЗАТЕЛЕН, только из .env
  ├── БЕЗ дефолта, обычный тип   → настройка среды, дефолт живёт в .env.example
  └── НЕТ захардкоженных дефолтов в коде — все значения явно в .env
"""

from pathlib import Path

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",  # лишние переменные в .env не вызывают ошибку
    )

    # ── ОБЯЗАТЕЛЬНЫЕ секреты ──────────────────────────────────────────────────
    # Нет дефолта → pydantic бросит ValidationError при старте, если не заданы

    bot_token: SecretStr        # BOT_TOKEN
    gemini_api_key: SecretStr   # GEMINI_API_KEY

    # ── ОПЦИОНАЛЬНЫЕ настройки среды ─────────────────────────────────────────
    # Нет дефолта в коде → дефолты прописаны в .env.example, не здесь

    gemini_model: str           # GEMINI_MODEL
    knowledge_base_path: Path   # KNOWLEDGE_BASE_PATH
    context_window_pairs: int   # CONTEXT_WINDOW_PAIRS
    ai_temperature: float       # AI_TEMPERATURE
    ai_max_tokens: int          # AI_MAX_TOKENS

    # ── УВЕДОМЛЕНИЯ АДМИНИСТРАТОРА ────────────────────────────────────────────
    # Telegram chat_id владельца/администратора для получения алертов.
    # Если не задан — уведомления отключены (None).
    # Как узнать свой chat_id: написать @userinfobot в Telegram.
    admin_chat_id: int | None = None   # ADMIN_CHAT_ID


# Singleton — импортируй везде как: from core.config import settings
settings = Settings()