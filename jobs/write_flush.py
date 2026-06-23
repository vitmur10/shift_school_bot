"""
Фонова задача: періодичний flush WriteQueue у Google Sheets.

Забирає накопичені PendingWrite/AppendRow з черги (drain — атомарно
й одразу очищає чергу) і застосовує їх через SheetsClient. Якщо за
інтервал нічого не назбиралось — мережевий виклик не робиться взагалі.
"""

from __future__ import annotations

import asyncio
import logging

from storage.sheets_client import SheetsClient
from storage.write_queue import WriteQueue

logger = logging.getLogger(__name__)


async def flush_once(queue: WriteQueue, sheets: SheetsClient) -> int:
    """
    Один прохід: забрати все з черги й записати в Sheets.
    Повертає кількість оброблених записів (writes + appends) — зручно
    для admin-діагностики ("скільки щойно записалось").

    Якщо запис у Sheets падає (мережа, ліміт API, тимчасова
    недоступність) — щойно забрані з черги writes/appends повертаються
    назад через queue.requeue(...) ПЕРЕД тим, як виняток летить далі.
    Завдяки дедуплікації за (sheet, row, column) requeue безпечний:
    якщо за час падіння хтось встиг покласти в чергу новіше значення
    для тієї самої клітинки — воно не буде затерте поверненням
    застарілого значення (requeue не перезаписує вже існуючі ключі).
    """
    writes, appends = await queue.drain()
    if not writes and not appends:
        return 0

    try:
        await asyncio.to_thread(sheets.apply_queue_snapshot, writes, appends)
    except Exception:
        await queue.requeue(writes, appends)
        raise

    logger.info("Flush у Sheets: %d точкових записів, %d нових рядків", len(writes), len(appends))
    return len(writes) + len(appends)


async def write_flush_loop(queue: WriteQueue, sheets: SheetsClient, interval_sec: int) -> None:
    """
    Нескінченний цикл flush. При невдалому проході записи повертаються
    в чергу (див. flush_once) і будуть повторно спробувані наступного
    разу — тимчасова недоступність Google Sheets API не призводить до
    втрати даних, лише до затримки запису.
    """
    while True:
        try:
            await flush_once(queue, sheets)
        except Exception:
            logger.exception(
                "Помилка під час flush у Sheets — записи повернуто в чергу, "
                "спроба повториться наступного циклу. Якщо помилка повторюється "
                "довго, перевірте ліміти API чи доступ до таблиці."
            )
        await asyncio.sleep(interval_sec)