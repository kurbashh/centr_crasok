"""
services/history_service.py — In-memory хранилище истории диалогов.

Ответственность: хранить, возвращать и очищать историю сообщений
для каждого пользователя. Не знает ничего об AI и Telegram.

При необходимости масштабирования этот модуль можно заменить
на Redis-реализацию, не трогая остальной код.
"""

from __future__ import annotations

from collections import defaultdict, deque
from typing import TypedDict

from core.config import settings


# ---------------------------------------------------------------------------
# Тип одного сообщения в истории
# ---------------------------------------------------------------------------

class ChatMessage(TypedDict):
    role: str      # "user" | "assistant"
    content: str


# ---------------------------------------------------------------------------
# Хранилище
# ---------------------------------------------------------------------------

# maxlen = N пар * 2 (каждая пара — user + assistant)
_store: dict[int, deque[ChatMessage]] = defaultdict(
    lambda: deque(maxlen=settings.max_history_messages * 2)
)


def get(user_id: int) -> list[ChatMessage]:
    """Возвращает текущую историю пользователя."""
    return list(_store[user_id])


def append(user_id: int, role: str, content: str) -> None:
    """Добавляет одно сообщение в историю пользователя."""
    _store[user_id].append(ChatMessage(role=role, content=content))


def clear(user_id: int) -> None:
    """Полностью сбрасывает историю пользователя."""
    _store[user_id].clear()


def size(user_id: int) -> int:
    """Возвращает количество сообщений в истории пользователя."""
    return len(_store[user_id])