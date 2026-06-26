"""
Адмін-команди.

НАРАЗІ єдина команда — /refresh (примусове оновлення кешу). Усі інші
адмін-дії (групова активація, блокування, перевипуск токена) на цьому
етапі виконуються ВРУЧНУ прямо в Google Таблиці — бот їх просто
підхоплює при черговому/примусовому оновленні кешу. Розширення цього
файлу повноцінними адмін-командами — окремий майбутній крок.

Доступ обмежений списком telegram_id з config.py (ADMIN_IDS) — без
оголошення фільтра тут, бо config ще не підключений до bot/ на цьому
етапі розробки; підключення фільтра відбудеться разом з main.py.
"""

from __future__ import annotations

import logging

from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message

from bot.texts import ADMIN_CACHE_REFRESHED, ADMIN_ONLY
from jobs.cache_refresh import refresh_cache_once
from storage.cache_store import CacheStore
from storage.sheets_client import SheetsClient

logger = logging.getLogger(__name__)

router = Router(name="admin")


@router.message(Command("getid"))
async def handle_getid(message: Message, admin_ids: set[int]) -> None:
    """
    Використання: перешліть повідомлення з каналу, натисніть Reply на нього
    і напишіть /getid — бот поверне chat_id і message_id.
    """
    if message.from_user.id not in admin_ids:
        await message.answer(ADMIN_ONLY)
        return

    # шукаємо forward_origin або в reply, або в поточному повідомленні
    target = message.reply_to_message or message
    fwd = target.forward_origin

    if fwd is None:
        await message.answer(
            "Як користуватись:\n"
            "1. Перешліть повідомлення з каналу боту\n"
            "2. Натисніть Reply (відповісти) на те переслане повідомлення\n"
            "3. Напишіть /getid"
        )
        return

    from aiogram.types import MessageOriginChannel
    if isinstance(fwd, MessageOriginChannel):
        chat_id = fwd.chat.id
        message_id = fwd.message_id
        await message.answer(
            f"✅ Дані для таблиці:\n\n"
            f"chat_id: `{chat_id}`\n"
            f"message_id: `{message_id}`",
            parse_mode="Markdown"
        )
    else:
        await message.answer(
            f"Тип пересилання: {type(fwd).__name__}\n"
            f"Переслати потрібно саме з каналу, не від користувача."
        )


@router.message(Command("getfileid"))
async def handle_getfileid(message: Message, admin_ids: set[int]) -> None:
    """
    Використання: перешліть відео з каналу боту, натисніть Reply
    і напишіть /getfileid — бот поверне file_id для таблиці.

    file_id зберігається в колонках media_1_file_id / media_2_file_id
    у вкладці Stages і використовується для send_media_group (справжній album).
    """
    if message.from_user.id not in admin_ids:
        await message.answer(ADMIN_ONLY)
        return

    target = message.reply_to_message or message

    # debug: логуємо що саме прийшло
    logger.info(
        "getfileid: target type=%s, has_video=%s, has_doc=%s, has_video_note=%s, forward_origin=%s",
        type(target).__name__,
        bool(target.video),
        bool(target.document),
        bool(target.video_note),
        type(target.forward_origin).__name__ if target.forward_origin else None,
    )

    # витягуємо file_id залежно від типу медіа
    file_id = None
    media_type = None

    if target.video:
        file_id = target.video.file_id
        media_type = "video"
    elif target.video_note:
        file_id = target.video_note.file_id
        media_type = "video_note (кружечок)"
    elif target.document:
        file_id = target.document.file_id
        media_type = "document"
    elif target.photo:
        file_id = target.photo[-1].file_id
        media_type = "photo"

    if file_id is None:
        await message.answer(
            "Як користуватись:\n"
            "1. Перешліть відео з каналу боту\n"
            "2. Натисніть Reply на те переслане відео\n"
            "3. Напишіть /getfileid"
        )
        return

    await message.answer(
        f"✅ file_id для таблиці ({media_type}):\n\n"
        f"{file_id}"
    )


@router.message(Command("refresh"))
async def handle_refresh(message: Message, cache: CacheStore, sheets: SheetsClient, admin_ids: set[int]) -> None:
    if message.from_user.id not in admin_ids:
        await message.answer(ADMIN_ONLY)
        return

    await refresh_cache_once(cache, sheets)
    await message.answer(ADMIN_CACHE_REFRESHED)
    logger.info("Кеш примусово оновлено адміном tg_id=%s", message.from_user.id)