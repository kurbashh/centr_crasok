"""
bot/handlers/commands.py — Обработчики команд (/start, /help, /contacts, /about).

Команды предоставляют информацию в минималистичном стиле без эмодзи перед пунктами
и без рекомендации нажимать на другие команды (текстовый интерфейс без меню).
"""

from __future__ import annotations

import logging

from aiogram import Bot, Router
from aiogram.filters import Command
from aiogram.types import Message, CallbackQuery
from aiogram.exceptions import TelegramAPIError

from core.config import settings
from services.context_service import context_service

logger = logging.getLogger(__name__)
commands_router = Router(name="commands")


# ─────────────────────────────────────────────────────────────────────────────
# Тексты команд (минималистичный стиль, без перегрузки эмодзи)
# ─────────────────────────────────────────────────────────────────────────────

_MSG_START: str = (
    "Сәлем! Привет! 👋\n\n"
    "Я консультант Центра Красок #1 — говорю на казахском и русском.\n\n"
    "Помогу с:\n"
    "— Выбором краски для любой поверхности\n"
    "— Расчётом расхода материалов\n"
    "— Подбором оттенка (45 000+ цветов по RAL, NCS, Pantone)\n"
    "— Декоративными штукатурками и спецпокрытиями\n\n"
    "Просто напишите свой вопрос о красках и отделке 🎨\n\n"
    "Для точного расчёта расхода краски по комнатам: /calc"
)

_MSG_HELP: str = (
    "Как лучше задать вопрос:\n\n"
    "Упоминайте в вопросе:\n"
    "— Размер помещения или площадь (кв.м)\n"
    "— Тип поверхности (гипсокартон, кирпич, дерево)\n"
    "— Назначение (интерьер, фасад, ванная)\n"
    "— Желаемый эффект (матовый, глянец, декоративный)\n"
    "— Особые требования (экологичность, быстрая сушка)\n\n"
    "Пример хорошего вопроса:\n"
    "\"Делаю ремонт в квартире 60 кв.м. Какую краску выбрать для гостиной? "
    "Хочу матовое покрытие, долговечное.\"\n\n"
    "Специализируюсь только на краске, грунтовке, штукатурке и декоративных материалах.\n\n"
    "/calc — пошаговый расчёт расхода краски по комнатам"
)

_MSG_CONTACTS: str = (
    "Адреса Центра Красок #1:\n\n"
    "Алматы — Шоу-рум 1 (главный)\n"
    "ул. Кабдолова 1/8, блок 1, линия D, бутик 14\n"
    "+7 778 061 5000, +7 701 877 5000, +7 701 974 5000\n"
    "ежедневно 10:00–20:00\n\n"
    "Алматы — Шоу-рум 2\n"
    "ул. Кабдолова 1/8, блок 1, линия D, бутик 21\n"
    "+7 778 800 4442\n"
    "ежедневно 10:00–20:00\n\n"
    "Астана\n"
    "ул. Мангилик Ел, 29/2\n"
    "+7 701 943 5000\n"
    "ежедневно 10:00–20:00\n\n"
    "info@centr-krasok.kz • https://centr-krasok.kz"
)

_MSG_ABOUT: str = (
    "Центр Красок #1 — дистрибьютор лакокрасочных и декоративных материалов в Казахстане.\n\n"
    "Что мы делаем:\n"
    "— Компьютерная колеровка (45 000+ оттенков)\n"
    "— Создание выкрасов для тестирования\n"
    "— Расчет расхода материалов\n"
    "— Консультации технологов\n"
    "— Доставка по Казахстану\n\n"
    "Работаем с брендами: AkzoNobel (Dulux, Marshall), Hammerite, Pinotex, Oikos, "
    "Tikkurila, Caparol, San Marco, Rust-Oleum, Little Greene, Argile и другие.\n\n"
    "За годы работы реализовали тысячи проектов на разных типах поверхностей."
)


# ─────────────────────────────────────────────────────────────────────────────
# Вспомогательная функция: отправка алерта администратору
# ─────────────────────────────────────────────────────────────────────────────

def _escape_markdown(text: str) -> str:
    """Экранирует специальные символы Markdown v1 для безопасной отправки."""
    for char in ("*", "_", "`", "["):
        text = text.replace(char, f"\\{char}")
    return text


async def _notify_admin_negative_feedback(
    bot: Bot,
    callback: CallbackQuery,
) -> None:
    """
    Отправляет мгновенный алерт владельцу при негативном фидбэке.

    Что включает алерт:
      — Идентификатор пользователя и его имя/username
      — Текст вопроса пользователя (извлеченный из истории)
      — Текст ответа бота, на который пожаловались
      — Временна́я метка

    Если ADMIN_CHAT_ID не задан в .env — уведомление молча пропускается.
    """
    admin_id = settings.admin_chat_id
    if not admin_id:
        return  # Уведомления отключены

    user = callback.from_user
    msg = callback.message

    # Формируем строку с идентификатором пользователя
    user_mention = f"@{_escape_markdown(user.username)}" if user.username else f"id={user.id}"
    user_full_name = _escape_markdown(user.full_name or "—")

    # Извлекаем текст ответа бота (то сообщение, под которым кнопки)
    bot_reply_text = ""
    if msg and msg.text:
        # Обрезаем до 300 символов, чтобы алерт не был огромным
        bot_reply_text = msg.text[:300]
        if len(msg.text) > 300:
            bot_reply_text += "…"

    # Пытаемся восстановить вопрос пользователя из истории контекста
    user_question = ""
    try:
        history = await context_service.get_history(user.id)
        if history:
            bot_text = msg.text if (msg and msg.text) else ""
            found_idx = -1
            if bot_text:
                # Ищем последнее совпадение ответа ассистента в истории
                for idx in range(len(history) - 1, -1, -1):
                    msg_item = history[idx]
                    if msg_item.get("role") == "assistant" and msg_item.get("content") in bot_text:
                        found_idx = idx
                        break
            
            # Нашли ответ ассистента -> ищем ближайший вопрос пользователя до него
            if found_idx > 0:
                for idx in range(found_idx - 1, -1, -1):
                    if history[idx].get("role") == "user":
                        user_question = history[idx].get("content", "")
                        break
            
            # Резервный вариант: если точное совпадение не найдено, берем самый последний вопрос пользователя
            if not user_question:
                for msg_item in reversed(history):
                    if msg_item.get("role") == "user":
                        user_question = msg_item.get("content", "")
                        break
    except Exception as exc:
        logger.warning(
            "Failed to retrieve user question from history for admin alert | user_id=%d | %s: %s",
            user.id, type(exc).__name__, exc,
        )

    # Собираем алерт
    alert_lines = [
        "🔴 *Негативный фидбэк*",
        "",
        f"👤 Пользователь: {user_mention} ({user_full_name})",
        f"🆔 user\\_id: `{user.id}`",
    ]

    # Добавляем вопрос пользователя, если нашли его
    if user_question:
        short_question = user_question[:300]
        if len(user_question) > 300:
            short_question += "…"
        escaped_question = _escape_markdown(short_question)
        alert_lines += [
            "",
            "❓ *Вопрос пользователя:*",
            f"_{escaped_question}_",
        ]

    # Добавляем ответ бота
    if bot_reply_text:
        escaped_reply = _escape_markdown(bot_reply_text)
        alert_lines += [
            "",
            "💬 *Ответ бота, который не помог:*",
            f"_{escaped_reply}_",
        ]

    alert_text = "\n".join(alert_lines)

    try:
        await bot.send_message(
            chat_id=admin_id,
            text=alert_text,
            parse_mode="Markdown",
        )
        logger.info(
            "ADMIN_ALERT sent | admin_id=%d | from user_id=%d",
            admin_id, user.id,
        )
    except TelegramAPIError as e:
        # Не даём ошибке уведомления сломать основной flow
        logger.error(
            "Failed to send admin alert | admin_id=%d | %s",
            admin_id, e,
        )


# ─────────────────────────────────────────────────────────────────────────────
# Обработчики команд с корректным error handling
# ─────────────────────────────────────────────────────────────────────────────

@commands_router.message(Command("start"))
async def cmd_start(message: Message) -> None:
    """Отправляет приветственное сообщение с примерами вопросов."""
    try:
        await message.answer(_MSG_START, parse_mode="HTML")
        logger.info("START command executed | user_id=%d", message.from_user.id)
    except TelegramAPIError as e:
        logger.error(
            "Failed to send START message | user_id=%d: %s",
            message.from_user.id,
            e,
            exc_info=True,
        )


@commands_router.callback_query(lambda c: c.data.startswith("fb_"))
async def handle_feedback(callback: CallbackQuery) -> None:
    """
    Обрабатывает нажатия на кнопки фидбека.

    fb_good → подтверждение пользователю, тишина.
    fb_bad  → подтверждение пользователю + мгновенный алерт администратору.
    """
    if callback.data == "fb_good":
        await callback.answer("Рад помочь! 🎨", show_alert=False)
        logger.info(
            "POSITIVE_FEEDBACK | user_id=%d | msg_id=%d",
            callback.from_user.id,
            callback.message.message_id if callback.message else 0,
        )
    else:
        # Сначала отвечаем пользователю — это быстро и не блокируется
        await callback.answer("Спасибо за отзыв, передам специалистам.", show_alert=False)
        logger.info(
            "NEGATIVE_FEEDBACK | user_id=%d | msg_id=%d",
            callback.from_user.id,
            callback.message.message_id if callback.message else 0,
        )

        # Затем отправляем алерт владельцу
        if callback.bot:
            await _notify_admin_negative_feedback(bot=callback.bot, callback=callback)

    # Убираем кнопки после нажатия (независимо от типа фидбэка)
    if callback.message:
        try:
            await callback.message.edit_reply_markup(reply_markup=None)
        except TelegramAPIError as e:
            logger.warning("Could not remove feedback buttons: %s", e)


@commands_router.message(Command("help"))
async def cmd_help(message: Message) -> None:
    """Отправляет советы по формулированию вопросов."""
    try:
        await message.answer(_MSG_HELP, parse_mode="HTML")
        logger.info("HELP command executed | user_id=%d", message.from_user.id)
    except TelegramAPIError as e:
        logger.error(
            "Failed to send HELP message | user_id=%d: %s",
            message.from_user.id,
            e,
            exc_info=True,
        )


@commands_router.message(Command("contacts"))
async def cmd_contacts(message: Message) -> None:
    """Отправляет контактную информацию и адреса."""
    try:
        await message.answer(_MSG_CONTACTS, parse_mode="HTML")
        logger.info("CONTACTS command executed | user_id=%d", message.from_user.id)
    except TelegramAPIError as e:
        logger.error(
            "Failed to send CONTACTS message | user_id=%d: %s",
            message.from_user.id,
            e,
            exc_info=True,
        )


@commands_router.message(Command("about"))
async def cmd_about(message: Message) -> None:
    """Отправляет информацию о компании."""
    try:
        await message.answer(_MSG_ABOUT, parse_mode="HTML")
        logger.info("ABOUT command executed | user_id=%d", message.from_user.id)
    except TelegramAPIError as e:
        logger.error(
            "Failed to send ABOUT message | user_id=%d: %s",
            message.from_user.id,
            e,
            exc_info=True,
        )