"""
services/rate_limiter.py — Сервис ограничения частоты запросов.

Архитектура: sliding window (скользящее окно).
  — Храним список timestamp-ов каждого пользователя в deque.
  — При каждом запросе «сдвигаем» окно: удаляем устаревшие записи,
    затем проверяем — не превышен ли лимит.
  — Потокобезопасность через asyncio.Lock (один lock на пользователя).

Почему sliding window, а не fixed window:
  — Fixed window: лимит 5/мин → 5 сообщений в 00:59, 5 в 01:00 = 10 за 2 секунды.
  — Sliding window: всегда точно N запросов за последние M секунд.

Почему сервис, а не middleware:
  — Логика лимитирования не зависит от Telegram → тестируется отдельно.
  — Middleware остаётся тонким: вызов сервиса + формирование ответа.
"""

from __future__ import annotations

import asyncio
import time
import logging
from collections import deque
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


@dataclass
class RateLimitResult:
    """Результат проверки лимита."""
    allowed: bool
    remaining: int          # Сколько запросов ещё разрешено в текущем окне
    retry_after: float      # Через сколько секунд можно снова (0 если allowed)
    requests_made: int      # Сколько уже сделано в окне


class RateLimiterService:
    """
    Sliding-window rate limiter с per-user изоляцией.

    Параметры по умолчанию (можно переопределить при создании):
      max_requests  — максимум сообщений за window_seconds (default: 5)
      window_seconds — длина скользящего окна в секундах (default: 60)
      burst_max     — максимум сообщений за burst_seconds (защита от флуда)
      burst_seconds — короткое окно для burst-защиты (default: 5)

    Пример использования:
      limiter = RateLimiterService(max_requests=5, window_seconds=60)
      result = await limiter.check(user_id=123456)
      if not result.allowed:
          await message.answer(f"Подождите {result.retry_after:.0f} сек.")
    """

    def __init__(
        self,
        max_requests: int = 5,
        window_seconds: float = 60.0,
        burst_max: int = 3,
        burst_seconds: float = 5.0,
    ) -> None:
        self._max_requests = max_requests
        self._window = window_seconds
        self._burst_max = burst_max
        self._burst_window = burst_seconds

        # user_id → deque[timestamp]
        self._windows: dict[int, deque[float]] = {}
        self._burst_windows: dict[int, deque[float]] = {}

        # user_id → asyncio.Lock
        self._locks: dict[int, asyncio.Lock] = {}

    # ─── Приватные хелперы ────────────────────────────────────────────────

    def _get_lock(self, user_id: int) -> asyncio.Lock:
        if user_id not in self._locks:
            self._locks[user_id] = asyncio.Lock()
        return self._locks[user_id]

    def _get_window(self, user_id: int) -> deque[float]:
        if user_id not in self._windows:
            self._windows[user_id] = deque()
        return self._windows[user_id]

    def _get_burst_window(self, user_id: int) -> deque[float]:
        if user_id not in self._burst_windows:
            self._burst_windows[user_id] = deque()
        return self._burst_windows[user_id]

    def _cleanup(self, timestamps: deque[float], window: float, now: float) -> None:
        """Удаляет устаревшие записи из скользящего окна."""
        cutoff = now - window
        while timestamps and timestamps[0] < cutoff:
            timestamps.popleft()

    # ─── Публичный интерфейс ──────────────────────────────────────────────

    async def check(self, user_id: int) -> RateLimitResult:
        """
        Проверяет, разрешён ли следующий запрос пользователя.
        Если разрешён — записывает timestamp (атомарно).
        Если нет — возвращает время ожидания.
        """
        async with self._get_lock(user_id):
            now = time.monotonic()

            # ── Проверка burst-лимита (короткое окно) ────────────────────
            burst_ts = self._get_burst_window(user_id)
            self._cleanup(burst_ts, self._burst_window, now)

            if len(burst_ts) >= self._burst_max:
                oldest_burst = burst_ts[0]
                retry_after = (oldest_burst + self._burst_window) - now
                logger.warning(
                    "BURST_LIMIT | user_id=%d | %d req in %.1fs | retry=%.1fs",
                    user_id, len(burst_ts), self._burst_window, retry_after,
                )
                return RateLimitResult(
                    allowed=False,
                    remaining=0,
                    retry_after=max(0.0, retry_after),
                    requests_made=len(burst_ts),
                )

            # ── Проверка основного лимита (длинное окно) ─────────────────
            window_ts = self._get_window(user_id)
            self._cleanup(window_ts, self._window, now)

            if len(window_ts) >= self._max_requests:
                oldest = window_ts[0]
                retry_after = (oldest + self._window) - now
                logger.warning(
                    "RATE_LIMIT | user_id=%d | %d/%d req in %.0fs | retry=%.1fs",
                    user_id, len(window_ts), self._max_requests,
                    self._window, retry_after,
                )
                return RateLimitResult(
                    allowed=False,
                    remaining=0,
                    retry_after=max(0.0, retry_after),
                    requests_made=len(window_ts),
                )

            # ── Разрешено — записываем timestamp ─────────────────────────
            window_ts.append(now)
            burst_ts.append(now)

            remaining = self._max_requests - len(window_ts)
            logger.debug(
                "RATE_OK | user_id=%d | %d/%d | remaining=%d",
                user_id, len(window_ts), self._max_requests, remaining,
            )
            return RateLimitResult(
                allowed=True,
                remaining=remaining,
                retry_after=0.0,
                requests_made=len(window_ts),
            )

    async def reset(self, user_id: int) -> None:
        """Сбрасывает счётчики пользователя (для тестов или ручного сброса)."""
        async with self._get_lock(user_id):
            self._windows.pop(user_id, None)
            self._burst_windows.pop(user_id, None)
        logger.info("RATE_RESET | user_id=%d", user_id)

    async def get_status(self, user_id: int) -> dict:
        """Возвращает текущий статус пользователя (для /status команды или дебага)."""
        async with self._get_lock(user_id):
            now = time.monotonic()
            window_ts = self._get_window(user_id)
            burst_ts = self._get_burst_window(user_id)
            self._cleanup(window_ts, self._window, now)
            self._cleanup(burst_ts, self._burst_window, now)
            return {
                "requests_in_window": len(window_ts),
                "max_requests": self._max_requests,
                "remaining": max(0, self._max_requests - len(window_ts)),
                "burst_requests": len(burst_ts),
                "burst_max": self._burst_max,
            }


# Singleton — импортируется в middleware и при необходимости в handlers
rate_limiter = RateLimiterService(
    max_requests=5,      # 5 сообщений за 60 секунд
    window_seconds=60.0,
    burst_max=3,         # не более 3 сообщений за 5 секунд
    burst_seconds=5.0,
)