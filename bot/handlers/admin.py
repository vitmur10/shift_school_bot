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


@router.message(Command("refresh"))
async def handle_refresh(message: Message, cache: CacheStore, sheets: SheetsClient, admin_ids: set[int]) -> None:
    if message.from_user.id not in admin_ids:
        await message.answer(ADMIN_ONLY)
        return

    await refresh_cache_once(cache, sheets)
    await message.answer(ADMIN_CACHE_REFRESHED)
    logger.info("Кеш примусово оновлено адміном tg_id=%s", message.from_user.id)