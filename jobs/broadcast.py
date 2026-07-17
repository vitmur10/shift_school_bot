"""
Фонова задача: ручні розсилки через лист Broadcasts.

Адмін заповнює у таблиці рядок (аудиторія + текст) і ставить у колонці
`status` тригер «надіслати». Ця джоба періодично читає лист, розсилає
повідомлення відповідній аудиторії (активні учасники потоку/тарифу) і
проставляє результат назад у рядок: «надіслано» + дата + скільки доставлено.

Ідемпотентність: перед розсилкою рядок одразу «застовплюється» статусом
«надсилається» (пряме синхронне оновлення), тож той самий рядок не піде
повторно навіть при рестарті процесу між циклами.

Лист Broadcasts опціональний: якщо його немає — розсилки просто вимкнено,
решта бота працює як раніше (read_broadcasts повертає None).
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

from services.notifications import SendsMessages
from storage.cache_store import CacheStore
from storage.models import ParticipantStatus
from storage.sheets_client import SheetsClient

logger = logging.getLogger(__name__)

# значення колонки status
TRIGGER_STATUSES = {"надіслати", "розіслати", "send", "готово"}
STATUS_PROCESSING = "надсилається"
STATUS_SENT = "надіслано"
STATUS_ERROR = "помилка"

# невелика пауза між повідомленнями, щоб не впертись у ліміти Telegram
SEND_DELAY_SEC = 0.05


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _recipients(cache: CacheStore, stream_id: str, plan_id: str) -> list:
    """
    Активні учасники з telegram_id, що підходять під аудиторію:
    - stream_id (якщо задано) — лише цей потік;
    - plan_id (якщо задано) — лише цей тариф.
    Хоча б одне з двох має бути задане (перевіряється у виклику).
    """
    out = []
    for p in cache.all_participants():
        if p.status != ParticipantStatus.ACTIVE or p.telegram_id is None:
            continue
        if stream_id and p.stream_id != stream_id:
            continue
        if plan_id and p.plan_id != plan_id:
            continue
        out.append(p)
    return out


async def process_broadcasts(
    cache: CacheStore,
    sheets: SheetsClient,
    bot: SendsMessages,
) -> int:
    """Один прохід: розіслати всі рядки Broadcasts із тригером. Повертає к-ть доставлених."""
    rows = await asyncio.to_thread(sheets.read_broadcasts)
    if not rows:  # None (листа нема) або порожньо
        return 0

    total_delivered = 0

    for i, row in enumerate(rows):
        row_index = i + 2  # рядок 1 — заголовок
        status_raw = str(row.get("status", "")).strip().lower()
        if status_raw not in TRIGGER_STATUSES:
            continue

        message = str(row.get("message", "")).strip()
        stream_id = str(row.get("stream_id", "")).strip()
        plan_id = str(row.get("plan_id", "")).strip()

        # одразу «застовпити» рядок, щоб не розіслати повторно
        await asyncio.to_thread(sheets.set_broadcast_status, row_index, STATUS_PROCESSING)

        if not message:
            await asyncio.to_thread(
                sheets.update_broadcast_status, row_index, STATUS_ERROR, _now(), 0,
                "порожнє повідомлення",
            )
            continue
        if not stream_id and not plan_id:
            await asyncio.to_thread(
                sheets.update_broadcast_status, row_index, STATUS_ERROR, _now(), 0,
                "не вказано аудиторію (stream_id або plan_id)",
            )
            continue

        recipients = _recipients(cache, stream_id, plan_id)
        delivered, failed = 0, 0
        for p in recipients:
            try:
                await bot.send_message(chat_id=p.telegram_id, text=message)
                delivered += 1
            except Exception:
                failed += 1
                logger.warning(
                    "Розсилка: не доставлено participant_id=%s (tg_id=%s)",
                    p.participant_id, p.telegram_id,
                )
            await asyncio.sleep(SEND_DELAY_SEC)

        note = f"аудиторія={len(recipients)}, доставлено={delivered}, помилок={failed}"
        await asyncio.to_thread(
            sheets.update_broadcast_status, row_index, STATUS_SENT, _now(), delivered, note,
        )
        total_delivered += delivered
        logger.info(
            "Розсилка (рядок %d, stream=%r plan=%r): %s",
            row_index, stream_id, plan_id, note,
        )

    return total_delivered


async def broadcast_loop(
    cache: CacheStore,
    sheets: SheetsClient,
    bot: SendsMessages,
    interval_sec: int,
) -> None:
    """
    Нескінченний цикл обробки ручних розсилок. Помилка одного проходу
    логується, але не зупиняє цикл.
    """
    while True:
        try:
            await process_broadcasts(cache, sheets, bot)
        except Exception:
            logger.exception("Помилка під час обробки ручних розсилок (Broadcasts)")
        await asyncio.sleep(interval_sec)
