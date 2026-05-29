"""
bot/handlers/calc.py — Пошаговый калькулятор расхода краски (/calc).

Сценарий (FSM):
  /calc
    → Шаг 1: Какой тип поверхности? (inline-кнопки)
    → Шаг 2: Введите площадь в кв.м
    → Шаг 3: Сколько слоёв? (inline-кнопки, дефолт 2)
    → Результат + кнопка «Добавить ещё помещение» или «Готово»

Почему FSM, а не просто regex:
  — Пользователь через /calc хочет точный расчёт, не быстрый ответ.
  — FSM даёт чёткий UX: вопрос → ответ, без путаницы.
  — Промежуточные данные хранятся в FSMContext (in-memory, не Redis).
"""

from __future__ import annotations

import logging

from aiogram import Router
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from services.calculator_service import calculate, format_calc_hint

logger = logging.getLogger(__name__)
calc_router = Router(name="calc")


# ─────────────────────────────────────────────────────────────────────────────
# FSM States
# ─────────────────────────────────────────────────────────────────────────────

class CalcStates(StatesGroup):
    waiting_surface = State()   # Ждём выбор типа поверхности
    waiting_area    = State()   # Ждём ввод площади
    waiting_layers  = State()   # Ждём выбор числа слоёв


# ─────────────────────────────────────────────────────────────────────────────
# Клавиатуры
# ─────────────────────────────────────────────────────────────────────────────

_KB_SURFACE = InlineKeyboardMarkup(inline_keyboard=[
    [
        InlineKeyboardButton(text="🏠 Стены",   callback_data="surf_стены"),
        InlineKeyboardButton(text="⬜ Потолок", callback_data="surf_потолок"),
    ],
    [
        InlineKeyboardButton(text="🏗 Фасад",   callback_data="surf_фасад"),
        InlineKeyboardButton(text="🪵 Дерево",  callback_data="surf_дерево"),
    ],
    [
        InlineKeyboardButton(text="🔩 Металл",  callback_data="surf_металл"),
    ],
])

_KB_LAYERS = InlineKeyboardMarkup(inline_keyboard=[
    [
        InlineKeyboardButton(text="1 слой",  callback_data="layers_1"),
        InlineKeyboardButton(text="2 слоя ✓", callback_data="layers_2"),
        InlineKeyboardButton(text="3 слоя",  callback_data="layers_3"),
    ],
])

_KB_AFTER_CALC = InlineKeyboardMarkup(inline_keyboard=[
    [
        InlineKeyboardButton(text="➕ Добавить помещение", callback_data="calc_more"),
        InlineKeyboardButton(text="✅ Готово",              callback_data="calc_done"),
    ],
])


# ─────────────────────────────────────────────────────────────────────────────
# Шаг 0: старт /calc
# ─────────────────────────────────────────────────────────────────────────────

@calc_router.message(Command("calc"))
async def cmd_calc(message: Message, state: FSMContext) -> None:
    """Запускает пошаговый калькулятор."""
    await state.clear()
    await state.update_data(rooms=[])  # накапливаем несколько помещений

    await message.answer(
        "🖌 *Калькулятор расхода краски*\n\n"
        "Шаг 1 из 3 — Выберите тип поверхности:",
        parse_mode="Markdown",
        reply_markup=_KB_SURFACE,
    )
    await state.set_state(CalcStates.waiting_surface)


# ─────────────────────────────────────────────────────────────────────────────
# Шаг 1: выбор поверхности
# ─────────────────────────────────────────────────────────────────────────────

@calc_router.callback_query(CalcStates.waiting_surface, lambda c: c.data.startswith("surf_"))
async def cb_surface(callback: CallbackQuery, state: FSMContext) -> None:
    surface = callback.data.replace("surf_", "")
    await state.update_data(surface=surface)

    await callback.message.edit_text(   # type: ignore[union-attr]
        f"✅ Поверхность: *{surface}*\n\n"
        "Шаг 2 из 3 — Введите площадь в кв.м\n"
        "_Например: 25 или 3.5х4_",
        parse_mode="Markdown",
        reply_markup=None,
    )
    await callback.answer()
    await state.set_state(CalcStates.waiting_area)


# ─────────────────────────────────────────────────────────────────────────────
# Шаг 2: ввод площади
# ─────────────────────────────────────────────────────────────────────────────

@calc_router.message(CalcStates.waiting_area)
async def msg_area(message: Message, state: FSMContext) -> None:
    text = (message.text or "").strip()
    area = _parse_area_input(text)

    if area is None or area <= 0:
        await message.answer(
            "Не удалось распознать площадь. Введите число, например: *25* или *3.5х4*",
            parse_mode="Markdown",
        )
        return

    if area > 10_000:
        await message.answer(
            "Площадь больше 10 000 кв.м — уточните, возможно опечатка?"
        )
        return

    await state.update_data(area=area)

    await message.answer(
        f"✅ Площадь: *{area} кв.м*\n\n"
        "Шаг 3 из 3 — Сколько слоёв краски планируете?",
        parse_mode="Markdown",
        reply_markup=_KB_LAYERS,
    )
    await state.set_state(CalcStates.waiting_layers)


# ─────────────────────────────────────────────────────────────────────────────
# Шаг 3: выбор числа слоёв → результат
# ─────────────────────────────────────────────────────────────────────────────

@calc_router.callback_query(CalcStates.waiting_layers, lambda c: c.data.startswith("layers_"))
async def cb_layers(callback: CallbackQuery, state: FSMContext) -> None:
    layers = int(callback.data.replace("layers_", ""))
    data = await state.get_data()

    surface: str = data.get("surface", "стены")
    area: float  = data.get("area", 0.0)
    rooms: list  = data.get("rooms", [])

    # Рассчитываем текущее помещение
    result = calculate(area_m2=area, surface_type=surface, layers=layers)
    rooms.append(result)
    await state.update_data(rooms=rooms)

    calc_text = format_calc_hint(result)

    # Если несколько помещений — показываем итог по всем
    summary = ""
    if len(rooms) > 1:
        total_paint_min = sum(r.paint_min for r in rooms)
        total_paint_max = sum(r.paint_max for r in rooms)
        total_primer_min = sum(r.primer_min for r in rooms)
        total_primer_max = sum(r.primer_max for r in rooms)
        summary = (
            f"\n\n📊 *Итого по всем помещениям ({len(rooms)} шт.):*\n"
            f"— Краска: {total_paint_min}–{total_paint_max} л\n"
            f"— Грунтовка: {total_primer_min}–{total_primer_max} л"
        )

    text = (
        f"{calc_text}{summary}\n\n"
        "Точный расход зависит от выбранной марки краски. "
        "Наши консультанты помогут подобрать оптимальный вариант 🎨"
    )

    await callback.message.edit_text(  # type: ignore[union-attr]
        text,
        parse_mode="Markdown",
        reply_markup=_KB_AFTER_CALC,
    )
    await callback.answer()


# ─────────────────────────────────────────────────────────────────────────────
# После расчёта: добавить ещё помещение или завершить
# ─────────────────────────────────────────────────────────────────────────────

@calc_router.callback_query(lambda c: c.data == "calc_more")
async def cb_calc_more(callback: CallbackQuery, state: FSMContext) -> None:
    """Возвращает к шагу 1 для нового помещения."""
    await callback.message.edit_reply_markup(reply_markup=None)  # type: ignore[union-attr]
    await callback.answer()

    await callback.message.answer(  # type: ignore[union-attr]
        "Добавляем следующее помещение.\n\nШаг 1 из 3 — Выберите тип поверхности:",
        reply_markup=_KB_SURFACE,
    )
    await state.set_state(CalcStates.waiting_surface)


@calc_router.callback_query(lambda c: c.data == "calc_done")
async def cb_calc_done(callback: CallbackQuery, state: FSMContext) -> None:
    """Завершает сессию калькулятора."""
    await state.clear()
    await callback.message.edit_reply_markup(reply_markup=None)  # type: ignore[union-attr]
    await callback.answer("Расчёт завершён!", show_alert=False)

    await callback.message.answer(  # type: ignore[union-attr]
        "Если возникнут вопросы по выбору краски — просто напишите 🎨\n"
        "Шоу-рум: ул. Кабдолова 1/8, ежедневно 10:00–20:00"
    )


# ─────────────────────────────────────────────────────────────────────────────
# Вспомогательная функция
# ─────────────────────────────────────────────────────────────────────────────

def _parse_area_input(text: str) -> float | None:
    """
    Парсит ввод площади от пользователя.
    Поддерживает: "25", "25.5", "25,5", "3х5", "3x5", "3 на 5"
    """
    import re

    text = text.strip()

    # Формат "AxB" (размеры комнаты)
    match_room = re.match(
        r"^(\d+(?:[.,]\d+)?)\s*[хxХX×*на]\s*(\d+(?:[.,]\d+)?)$",
        text, re.IGNORECASE
    )
    if match_room:
        a = float(match_room.group(1).replace(",", "."))
        b = float(match_room.group(2).replace(",", "."))
        return round(a * b, 1)

    # Просто число
    match_num = re.match(r"^(\d+(?:[.,]\d+)?)\s*(?:кв\.?м?|м2|м²)?$", text, re.IGNORECASE)
    if match_num:
        return float(match_num.group(1).replace(",", "."))

    return None