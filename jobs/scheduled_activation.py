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
from datetime import datetime, timedelta, timezone

from storage.cache_store import CacheStore
from storage.models import ParticipantStatus, PlanType
from storage.write_queue import PendingWrite, WriteQueue
from bot.keyboards.stages_kb import access_opened_keyboard
from bot.texts import REMINDER_1H, reminder_24h_text
from services.access_control import activate_scheduled_plan, find_due_scheduled_participants
from services.notifications import SendsMessages, notify_participant, notify_scheduled_access_opened

logger = logging.getLogger(__name__)

COL_NOTIFICATION_SENT = "O"  # у листі Participants: L joined_at, M activated_at, N last_progress_at, O notification_sent
COL_REMINDERS_SENT = "P"     # які нагадування-прогрів уже надіслані (напр. "24,1")

# нагадування-прогрів: за скільки годин до старту слати. Порядок від
# більшого до меншого (спершу «за добу», потім «за годину»).
REMINDER_HOURS = (24, 1)


def _parse_reminders_sent(value: str) -> set[int]:
    return {int(x) for x in str(value or "").split(",") if x.strip().isdigit()}


def _format_reminders_sent(hours: set[int]) -> str:
    return ",".join(str(h) for h in sorted(hours, reverse=True))


def _reminder_text(hours: int, start_date) -> str:
    """Текст нагадування залежно від того, за скільки годин до старту."""
    if hours >= 24:
        return reminder_24h_text(start_date)
    return REMINDER_1H


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
    # додаємо кнопки «Група потоку»/«Написати куратору», якщо задані в таблиці
    stream = cache.get_stream(participant.stream_id)
    plan = stream.get_plan(participant.plan_id) if stream else None
    keyboard = access_opened_keyboard(
        group_url=stream.telegram_group_url if stream else None,
        curator_url=plan.curator_url if plan else None,
    )
    sent_ok = await notify_scheduled_access_opened(bot, participant, reply_markup=keyboard)
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


async def process_reminders(
    cache: CacheStore,
    queue: WriteQueue,
    bot: SendsMessages,
    now: datetime | None = None,
) -> int:
    """
    Розсилає нагадування-прогрів учасникам scheduled-тарифу ДО старту:
    за REMINDER_HOURS годин до plan.start_date (напр. за 24 і за 1 год).

    Умови для нагадування:
      - учасник ще PENDING (доступ не відкрито), токен прив'язано, є telegram_id;
      - тариф SCHEDULED із заданою start_date, яка ще НЕ настала;
      - конкретне нагадування ще не надсилалось (див. reminders_sent).

    Якщо через простій бота настало вікно одразу кількох нагадувань —
    шлемо лише найтерміновіше (менше годин), а решту прострочених просто
    позначаємо як надіслані, щоб не спамити застарілим «до старту доба».
    """
    now = now or datetime.now(timezone.utc)
    count = 0

    for p in cache.all_participants():
        if p.status != ParticipantStatus.PENDING or not p.token_used or p.telegram_id is None:
            continue
        stream = cache.get_stream(p.stream_id)
        plan = stream.get_plan(p.plan_id) if stream else None
        if plan is None or plan.plan_type != PlanType.SCHEDULED or plan.start_date is None:
            continue
        if plan.start_date <= now:
            continue  # старт уже настав -> це вже активація, а не нагадування

        already = _parse_reminders_sent(p.reminders_sent)
        due = [
            h for h in REMINDER_HOURS
            if h not in already and now >= plan.start_date - timedelta(hours=h)
        ]
        if not due:
            continue

        # найтерміновіше нагадування (найменше годин до старту)
        hours_to_send = min(due)
        sent_ok = await notify_participant(bot, p, _reminder_text(hours_to_send, plan.start_date))
        if not sent_ok:
            logger.warning(
                "Не вдалось надіслати нагадування (participant_id=%s, за %d год) — повтор наступного циклу",
                p.participant_id, hours_to_send,
            )
            continue

        # позначаємо як надіслані і саме нагадування, і всі прострочені вікна
        already.update(due)
        p.reminders_sent = _format_reminders_sent(already)
        cache.upsert_participant(p)
        await queue.enqueue(PendingWrite(
            sheet_name="Participants",
            row_index=p.row_index,
            column=COL_REMINDERS_SENT,
            value=p.reminders_sent,
            participant_id=p.participant_id,
        ))
        count += 1

    if count:
        logger.info("Нагадування-прогрів: надіслано %d", count)
    return count


async def scheduled_activation_loop(
    cache: CacheStore,
    queue: WriteQueue,
    bot: SendsMessages,
    interval_sec: int,
) -> None:
    """
    Нескінченний цикл перевірки дат активації + нагадувань-прогріву. Помилка
    одного проходу логується, але не зупиняє цикл — наступна спроба буде
    через interval_sec, дані в кеші лишаються коректними.
    """
    while True:
        try:
            await process_due_participants(cache, queue, bot)
            await process_reminders(cache, queue, bot)
        except Exception:
            logger.exception("Помилка під час активації/нагадувань scheduled-тарифу")
        await asyncio.sleep(interval_sec)