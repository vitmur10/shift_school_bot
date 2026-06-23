"""Створення Dispatcher, підключення routers і middlewares."""

from __future__ import annotations

from aiogram import Dispatcher
from aiogram.fsm.storage.memory import MemoryStorage

from bot.handlers import admin, fallback, stages, start
from bot.middlewares.access_middleware import AccessControlMiddleware
from bot.middlewares.cache_middleware import CacheMiddleware
from storage.cache_store import CacheStore
from storage.sheets_client import SheetsClient
from storage.write_queue import WriteQueue


def build_dispatcher(
    cache: CacheStore,
    queue: WriteQueue,
    sheets: SheetsClient,
    admin_ids: set[int],
) -> Dispatcher:
    dp = Dispatcher(storage=MemoryStorage())

    cache_mw = CacheMiddleware(cache, queue, sheets, admin_ids)
    dp.message.middleware(cache_mw)
    dp.callback_query.middleware(cache_mw)

    # access-контроль вмикаємо лише на router'і, що видає контент
    stages.router.callback_query.middleware(AccessControlMiddleware())

    # порядок важливий: start/admin/stages -- специфічні, fallback -- завжди останній
    dp.include_router(start.router)
    dp.include_router(admin.router)
    dp.include_router(stages.router)
    dp.include_router(fallback.router)

    return dp