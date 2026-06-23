"""
Middleware: інʼєктує спільні залежності (CacheStore, WriteQueue,
SheetsClient, набір admin_ids) у кожен handler через data.

Без цього довелось би тримати ці обʼєкти в глобальних змінних модуля
або тягнути через замикання при реєстрації кожного handler'а окремо —
обидва варіанти ускладнюють тестування handlers ізольовано.

Використання в handler'і (aiogram сам підставляє іменовані параметри
з data, якщо вони збігаються з ключами, прокинутими тут):

    async def my_handler(message: Message, cache: CacheStore, queue: WriteQueue):
        ...

    async def admin_handler(message: Message, sheets: SheetsClient, admin_ids: set[int]):
        ...
"""

from __future__ import annotations

from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware
from aiogram.types import TelegramObject

from storage.cache_store import CacheStore
from storage.sheets_client import SheetsClient
from storage.write_queue import WriteQueue


class CacheMiddleware(BaseMiddleware):
    def __init__(
        self,
        cache: CacheStore,
        queue: WriteQueue,
        sheets: SheetsClient | None = None,
        admin_ids: set[int] | None = None,
    ) -> None:
        self._cache = cache
        self._queue = queue
        self._sheets = sheets
        self._admin_ids = admin_ids or set()

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        data["cache"] = self._cache
        data["queue"] = self._queue
        data["sheets"] = self._sheets
        data["admin_ids"] = self._admin_ids
        return await handler(event, data)