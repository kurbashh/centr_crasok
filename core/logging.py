"""
core/logging.py — Централизованная настройка логирования.

Вызвать один раз при старте приложения: setup_logging().
После этого во всех модулях достаточно logging.getLogger(__name__).
"""

import logging
import sys


def setup_logging(level: int = logging.INFO) -> None:
    """Настраивает root-логгер для всего приложения."""
    logging.basicConfig(
        level=level,
        format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        stream=sys.stdout,
    )
    # Убираем излишний шум от сторонних библиотек
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("openai").setLevel(logging.WARNING)