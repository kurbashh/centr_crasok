"""
services/calculator_service.py — Калькулятор расхода краски.

Ответственность:
  - Извлекать площадь из произвольного текста пользователя (regex).
  - Рассчитывать расход краски и грунтовки по нормам.
  - Возвращать готовый текст-вставку для ответа AI.

Этот модуль НЕ знает про Telegram и aiogram — только чистая логика.
Встраивается в messages.py: детект происходит до вызова AI,
результат добавляется в контекст, чтобы AI дал точный ответ.

Поддерживаемые форматы площади в тексте:
  "30 кв.м", "30кв.м", "30 м2", "30м²", "30 квадратов",
  "комната 4х5", "4 на 5 метров", "40 метров погонных" — и казахские варианты.
"""

from __future__ import annotations

import re
import logging
from dataclasses import dataclass

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Нормы расхода (литров на м² за один слой)
# ─────────────────────────────────────────────────────────────────────────────

# Диапазоны: (мин, макс) литров на м² за 1 слой
_CONSUMPTION: dict[str, tuple[float, float]] = {
    "стены":   (0.10, 0.14),   # стандартные интерьерные краски
    "потолок": (0.10, 0.12),   # потолочные — чуть экономнее
    "фасад":   (0.12, 0.18),   # фасадные расходуются больше
    "дерево":  (0.10, 0.16),   # лаки, масла, пропитки
    "металл":  (0.08, 0.12),   # эмали по металлу
}

# Норма расхода грунтовки (л/м²)
_PRIMER_CONSUMPTION: tuple[float, float] = (0.08, 0.12)

# Стандартное число слоёв
_DEFAULT_LAYERS = 2

# Запас на пористые/тёмные поверхности
_POROUS_FACTOR = 1.20  # +20%


# ─────────────────────────────────────────────────────────────────────────────
# Паттерны для извлечения площади из текста
# ─────────────────────────────────────────────────────────────────────────────

# Прямое указание площади: "30 кв.м", "30м2", "30 квадратов", "30 м²"
_PATTERN_AREA = re.compile(
    r"(\d+(?:[.,]\d+)?)\s*"
    r"(?:кв\.?\s*м|м²|м2|квадрат(?:ов|а|ных метр(?:ов|а))?|шаршын метр)",
    re.IGNORECASE | re.UNICODE,
)

# Размеры комнаты: "4х5", "4 на 5", "4x5"
_PATTERN_ROOM = re.compile(
    r"(\d+(?:[.,]\d+)?)\s*[хxХX×*на]\s*(\d+(?:[.,]\d+)?)\s*(?:метр|м\b)?",
    re.IGNORECASE | re.UNICODE,
)

# Ключевые слова типа поверхности
_SURFACE_KEYWORDS: dict[str, list[str]] = {
    "потолок": ["потолок", "потолочн", "ceiling"],
    "фасад":   ["фасад", "улиц", "наружн", "exterior", "сыртқы"],
    "дерево":  ["дерево", "деревян", "дерев", "паркет", "террас", "ағаш"],
    "металл":  ["металл", "металлич", "железн", "темір"],
}
# Если ни одно не совпало → стены (самый частый случай)


# ─────────────────────────────────────────────────────────────────────────────
# Типы данных
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class AreaDetectionResult:
    """Результат извлечения площади из текста."""
    area_m2: float | None         # Площадь в кв.м (None если не найдена)
    surface_type: str             # Тип поверхности
    surface_detected: bool        # Был ли тип найден явно или подставлен дефолт
    source: str                   # "direct" | "room_size" | "not_found"


@dataclass
class CalcResult:
    """Результат расчёта расхода краски."""
    area_m2: float
    surface_type: str
    layers: int
    paint_min: float              # Минимум литров (округлено вверх)
    paint_max: float              # Максимум литров (округлено вверх)
    primer_min: float             # Грунтовка мин
    primer_max: float             # Грунтовка макс
    porous_note: bool             # Нужно ли предупреждение про пористые стены


# ─────────────────────────────────────────────────────────────────────────────
# Основные функции
# ─────────────────────────────────────────────────────────────────────────────

def detect_area(text: str) -> AreaDetectionResult:
    """
    Пытается извлечь площадь и тип поверхности из произвольного текста.

    Порядок проверки:
      1. Прямое упоминание площади ("30 кв.м")
      2. Размеры комнаты ("4х5 метров")
      3. Не найдено → area_m2 = None
    """
    surface_type, surface_detected = _detect_surface(text)

    # 1. Прямое упоминание площади
    match_area = _PATTERN_AREA.search(text)
    if match_area:
        area = float(match_area.group(1).replace(",", "."))
        logger.debug("Area detected (direct): %.1f m² | surface=%s", area, surface_type)
        return AreaDetectionResult(
            area_m2=area,
            surface_type=surface_type,
            surface_detected=surface_detected,
            source="direct",
        )

    # 2. Размеры комнаты: перемножаем
    match_room = _PATTERN_ROOM.search(text)
    if match_room:
        a = float(match_room.group(1).replace(",", "."))
        b = float(match_room.group(2).replace(",", "."))
        area = round(a * b, 1)
        logger.debug("Area detected (room %sx%s): %.1f m² | surface=%s", a, b, area, surface_type)
        return AreaDetectionResult(
            area_m2=area,
            surface_type=surface_type,
            surface_detected=surface_detected,
            source="room_size",
        )

    return AreaDetectionResult(
        area_m2=None,
        surface_type=surface_type,
        surface_detected=surface_detected,
        source="not_found",
    )


def calculate(area_m2: float, surface_type: str = "стены", layers: int = _DEFAULT_LAYERS) -> CalcResult:
    """
    Рассчитывает расход краски и грунтовки.

    Формула:
      paint = area × расход_за_слой × кол_слоёв
      primer = area × расход_грунтовки (1 слой всегда)
    """
    norms = _CONSUMPTION.get(surface_type, _CONSUMPTION["стены"])
    paint_min = _ceil_half(area_m2 * norms[0] * layers)
    paint_max = _ceil_half(area_m2 * norms[1] * layers)

    primer_min = _ceil_half(area_m2 * _PRIMER_CONSUMPTION[0])
    primer_max = _ceil_half(area_m2 * _PRIMER_CONSUMPTION[1])

    return CalcResult(
        area_m2=area_m2,
        surface_type=surface_type,
        layers=layers,
        paint_min=paint_min,
        paint_max=paint_max,
        primer_min=primer_min,
        primer_max=primer_max,
        porous_note=True,  # Всегда показываем совет про запас
    )


def format_calc_hint(result: CalcResult) -> str:
    """
    Возвращает готовый текстовый блок для вставки в ответ AI.

    Формат специально краткий — AI добавит контекст вокруг него.
    """
    lines = [
        f"📐 *Расчёт расхода для {result.surface_type} ({result.area_m2} кв.м, {result.layers} слоя):*",
        f"— Краска: {result.paint_min}–{result.paint_max} л",
        f"— Грунтовка: {result.primer_min}–{result.primer_max} л",
    ]
    if result.porous_note:
        lines.append("— Пористые или тёмные стены: возьмите с запасом +20%")
    return "\n".join(lines)


def build_calc_context(detection: AreaDetectionResult) -> str | None:
    """
    Если площадь найдена — строит строку-подсказку для добавления в промпт AI.
    AI получит точные данные и сделает расчёт частью своего ответа.

    Возвращает None, если площадь не обнаружена.
    """
    if detection.area_m2 is None:
        return None

    calc = calculate(detection.area_m2, detection.surface_type)
    hint = format_calc_hint(calc)

    surface_note = ""
    if not detection.surface_detected:
        surface_note = " (тип поверхности не указан, использовал 'стены' — уточни у пользователя)"

    return (
        f"\n\n[СИСТЕМНЫЙ РАСЧЁТ{surface_note}]\n"
        f"{hint}\n"
        f"Используй эти данные в своём ответе. Не пересчитывай самостоятельно."
    )


# ─────────────────────────────────────────────────────────────────────────────
# Вспомогательные функции
# ─────────────────────────────────────────────────────────────────────────────

def _detect_surface(text: str) -> tuple[str, bool]:
    """Определяет тип поверхности по ключевым словам. Возвращает (тип, найден_явно)."""
    text_lower = text.lower()
    for surface, keywords in _SURFACE_KEYWORDS.items():
        if any(kw in text_lower for kw in keywords):
            return surface, True
    return "стены", False


def _ceil_half(value: float) -> float:
    """Округляет до ближайших 0.5 вверх. Пример: 3.2 → 3.5, 3.7 → 4.0"""
    import math
    return math.ceil(value * 2) / 2