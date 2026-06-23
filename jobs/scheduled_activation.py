"""
Фонова задача: групова активація SCHEDULED-тарифу (Тариф 2).

Періодично перевіряє кеш на наявність PENDING-учасників, чия
start_date уже настала, переводить їх у ACTIVE і надсилає сповіщення
"доступ відкрито". Одне сповіщення на учасника — після успішної
відправки (або вже відправленого раніше) виставляється
notification_sent=True, щоб не дублювати повідомлення на наступному
циклі.
"""

from __future__ import annotations

import asyncio
import logging

from storage.cache_store import CacheStore
from storage.models import ParticipantStatus, PlanType
from storage.write_queue import PendingWrite, WriteQueue
from services.access_control import activate_scheduled_plan, find_due_scheduled_participants
from services.notifications import SendsMessages, notify_scheduled_access_opened

logger = logging.getLogger(__name__)

COL_NOTIFICATION_SENT = "N"


def _find_unsent_notifications(cache: CacheStore):
    """
    Учасники SCHEDULED-тарифу, яких уже активовано (ACTIVE), але яким
    сповіщення про старт так і не вдалось надіслати з першої спроби
    (бот був заблокований, тимчасова помилка Telegram API тощо).

    Окремо від find_due_scheduled_participants, бо та шукає лише
    PENDING — щойно учасник стає ACTIVE, він випадає з її вибірки,
    і без цієї функції недоставлене сповіщення ніколи не повториться.

    Фільтр саме за SCHEDULED важливий: для INSTANT-тарифу сповіщення
    взагалі не передбачено за вимогами проєкту, тому notification_sent=False
    там — нормальний стан, а не "недоставлене", і його не слід намагатись
    відправити.
    """
    result = []
    for p in cache.all_participants():
        if p.status != ParticipantStatus.ACTIVE or p.notification_sent:
            continue
        stream = cache.get_stream(p.stream_id)
        if stream is None:
            continue
        plan = stream.get_plan(p.plan_id)
        if plan is None or plan.plan_type != PlanType.SCHEDULED:
            continue
        result.append(p)
    return result


async def process_due_participants(
    cache: CacheStore,
    queue: WriteQueue,
    bot: SendsMessages,
) -> int:
    """
    Один прохід: знайти всіх, кому час відкривати доступ, активувати
    і сповістити; додатково — повторити спробу сповіщення для тих,
    кого вже активовано раніше, але кому повідомлення так і не дійшло.
    Повертає кількість активованих + повторно-сповіщених учасників.

    Активація (status -> ACTIVE) відбувається НЕЗАЛЕЖНО від того, чи
    вдалось надіслати сповіщення — доступ важливіший за повідомлення:
    якщо Telegram-відправка зафейлилась, людина однаково матиме
    відкритий курс і побачить контент при наступному /start чи "Далі".
    notification_sent виставляється тільки при реальному успіху
    відправки, щоб не "загубити" людей, яким так і не прийшло сповіщення.
    """
    due_participants = find_due_scheduled_participants(cache)
    processed_count = 0

    for participant in due_participants:
        await activate_scheduled_plan(cache, queue, participant)
        processed_count += 1
        await _try_send_notification(cache, queue, bot, participant)

    # окремий прохід: ті, хто вже ACTIVE, але кому сповіщення не дійшло
    # минулого разу (бот заблокований, тимчасовий збій Telegram API)
    for participant in _find_unsent_notifications(cache):
        sent = await _try_send_notification(cache, queue, bot, participant)
        if sent:
            processed_count += 1

    if processed_count:
        logger.info("Групова активація: оброблено %d учасник(ів)", processed_count)
    return processed_count


async def _try_send_notification(
    cache: CacheStore,
    queue: WriteQueue,
    bot: SendsMessages,
    participant,
) -> bool:
    """Допоміжна: спроба надіслати сповіщення + оновити notification_sent при успіху."""
    sent_ok = await notify_scheduled_access_opened(bot, participant)
    if sent_ok:
        participant.notification_sent = True
        cache.upsert_participant(participant)
        await queue.enqueue(PendingWrite(
            sheet_name="Participants",
            row_index=participant.row_index,
            column=COL_NOTIFICATION_SENT,
            value=True,
            participant_id=participant.participant_id,
        ))
    else:
        logger.warning(
            "Не вдалось надіслати сповіщення (participant_id=%s) — "
            "спроба повториться наступного циклу",
            participant.participant_id,
        )
    return sent_ok


async def scheduled_activation_loop(
    cache: CacheStore,
    queue: WriteQueue,
    bot: SendsMessages,
    interval_sec: int,
) -> None:
    """
    Нескінченний цикл перевірки дат активації. Помилка одного проходу
    логується, але не зупиняє цикл — наступна спроба буде через
    interval_sec, дані в кеші лишаються коректними.
    """
    while True:
        try:
            await process_due_participants(cache, queue, bot)
        except Exception:
            logger.exception("Помилка під час групової активації scheduled-тарифу")
        await asyncio.sleep(interval_sec)