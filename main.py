"""
Точка входу: піднімає event loop, запускає FastAPI + aiogram-polling
та фонові задачі (cache_refresh, write_flush, scheduled_activation)
в одному asyncio.gather.
"""

from __future__ import annotations

import asyncio
import logging

import gspread
import uvicorn
from aiogram import Bot

from bot.dispatcher import build_dispatcher
from config import settings
from jobs.cache_refresh import cache_refresh_loop, refresh_cache_once
from jobs.scheduled_activation import scheduled_activation_loop
from jobs.write_flush import write_flush_loop
from storage.cache_store import CacheStore
from storage.sheets_client import SheetsClient
from storage.write_queue import WriteQueue
from webhook.app import build_fastapi_app
from webhook.handlers import PARTICIPANTS_COLUMN_MAP

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger(__name__)


async def main() -> None:
    bot = Bot(token=settings.BOT_TOKEN)

    gclient = gspread.service_account(filename=settings.GOOGLE_CREDENTIALS_PATH).open_by_key(
        settings.SPREADSHEET_ID
    )
    sheets = SheetsClient(gclient)

    cache = CacheStore()
    queue = WriteQueue(column_index_map={"Participants": PARTICIPANTS_COLUMN_MAP})

    # перше повне завантаження кешу -- синхронно, ДО старту polling,
    # щоб бот не приймав апдейти "наосліп" з порожнім кешем
    logger.info("Перше завантаження кешу...")
    await refresh_cache_once(cache, sheets)

    dp = build_dispatcher(cache, queue, sheets, settings.admin_ids_set)
    app = build_fastapi_app(cache, queue)

    server = uvicorn.Server(uvicorn.Config(app, host="0.0.0.0", port=settings.WEBHOOK_PORT, log_level="info"))

    logger.info("Старт бота...")
    await asyncio.gather(
        dp.start_polling(bot),
        server.serve(),
        cache_refresh_loop(cache, sheets, settings.CACHE_REFRESH_SEC),
        write_flush_loop(queue, sheets, settings.WRITE_FLUSH_SEC),
        scheduled_activation_loop(cache, queue, bot, settings.SCHEDULED_CHECK_SEC),
    )


if __name__ == "__main__":
    asyncio.run(main())