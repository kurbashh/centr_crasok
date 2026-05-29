"""
services/context_service.py — Сервис управления контекстом диалога.

Ответственность:
  - Хранить историю сообщений для каждого user_id (in-memory).
  - Ограничивать глубину истории последними N парами (user + assistant).
  - Предоставлять чистый async-интерфейс, не зависящий от aiogram.

Архитектурная заметка:
  При необходимости масштабирования замените _store на Redis-адаптер —
  публичный интерфейс класса останется неизменным.
"""

from __future__ import annotations

import asyncio
import logging
from collections import deque
from typing import TypeAlias

from core.config import settings

logger = logging.getLogger(__name__)

# Тип одного сообщения в истории
ChatMessage: TypeAlias = dict[str, str]  # {"role": "user"|"assistant", "content": "..."}


class ContextService:
    """
    In-memory хранилище истории диалогов в разрезе user_id.

    Потокобезопасность: asyncio.Lock на каждого пользователя исключает
    race condition при одновременных запросах (например, при форвардинге).
    """

    def __init__(self, window_pairs: int | None = None) -> None:
        # Максимум хранимых сообщений = пар * 2 (user + assistant)
        self._maxlen: int = (window_pairs or settings.context_window_pairs) * 2

        # user_id -> deque с ограниченным размером
        self._store: dict[int, deque[ChatMessage]] = {}

        # user_id -> Lock для безопасного доступа
        self._locks: dict[int, asyncio.Lock] = {}

    # ── Приватные хелперы ──────────────────────────────────────────────────

    def _ensure_user(self, user_id: int) -> None:
        """Инициализирует хранилище и блокировку для нового пользователя."""
        if user_id not in self._store:
            self._store[user_id] = deque(maxlen=self._maxlen)
            self._locks[user_id] = asyncio.Lock()

    def _lock(self, user_id: int) -> asyncio.Lock:
        self._ensure_user(user_id)
        return self._locks[user_id]

    # ── Публичный async-интерфейс ──────────────────────────────────────────

    async def get_history(self, user_id: int) -> list[ChatMessage]:
        """Возвращает текущую историю пользователя как список."""
        async with self._lock(user_id):
            return list(self._store[user_id])

    async def add_user_message(self, user_id: int, text: str) -> None:
        """Добавляет сообщение пользователя в историю."""
        async with self._lock(user_id):
            self._store[user_id].append({"role": "user", "content": text})
        logger.debug("CTX | user_id=%d | +user | len=%d", user_id, len(self._store[user_id]))

    async def add_assistant_message(self, user_id: int, text: str) -> None:
        """Добавляет ответ ассистента в историю."""
        async with self._lock(user_id):
            self._store[user_id].append({"role": "assistant", "content": text})
        logger.debug("CTX | user_id=%d | +assistant | len=%d", user_id, len(self._store[user_id]))

    async def clear_history(self, user_id: int) -> None:
        """Полностью сбрасывает историю пользователя."""
        async with self._lock(user_id):
            self._store[user_id].clear()
        logger.info("CTX | user_id=%d | история очищена", user_id)

    async def history_size(self, user_id: int) -> int:
        """Возвращает количество сообщений в истории пользователя."""
        async with self._lock(user_id):
            return len(self._store[user_id])


# Singleton — один экземпляр на всё приложение
context_service = ContextService()