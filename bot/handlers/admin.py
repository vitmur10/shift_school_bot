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
    Команда для адміна: переслати повідомлення з контент-каналу боту
    і написати /getid — бот відповість chat_id і message_id,
    які потрібно вписати в таблицю Stages.
    """
    if message.from_user.id not in admin_ids:
        await message.answer(ADMIN_ONLY)
        return

    fwd = message.forward_origin
    if fwd is None:
        await message.answer(
            "Перешліть повідомлення з каналу боту і одразу напишіть /getid"
        )
        return

    # aiogram 3.x: forward_origin може бути MessageOriginChannel
    from aiogram.types import MessageOriginChannel
    if isinstance(fwd, MessageOriginChannel):
        chat_id = fwd.chat.id
        message_id = fwd.message_id
        await message.answer(
            f"✅ Дані для таблиці:\n\n"
            f"`chat_id:` `{chat_id}`\n"
            f"`message_id:` `{message_id}`",
            parse_mode="Markdown"
        )
    else:
        await message.answer(
            f"chat_id: {getattr(fwd, 'chat', {})}\n"
            f"Тип: {type(fwd).__name__}\n\n"
            f"Переслати потрібно саме з каналу, не від користувача."
        )


@router.message(Command("refresh"))
async def handle_refresh(message: Message, cache: CacheStore, sheets: SheetsClient, admin_ids: set[int]) -> None:
    if message.from_user.id not in admin_ids:
        await message.answer(ADMIN_ONLY)
        return

    await refresh_cache_once(cache, sheets)
    await message.answer(ADMIN_CACHE_REFRESHED)
    logger.info("Кеш примусово оновлено адміном tg_id=%s", message.from_user.id)