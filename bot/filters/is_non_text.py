"""
bot/filters/is_non_text.py — Фильтр нетекстовых сообщений.

Срабатывает, если сообщение не содержит текста:
стикер, фото, голосовое, видео, документ и т.д.
"""

from aiogram.filters import BaseFilter
from aiogram.types import Message


class IsNonText(BaseFilter):
    """Возвращает True, если у входящего Message нет текстового поля."""

    async def __call__(self, message: Message) -> bool:
        return not bool(message.text)