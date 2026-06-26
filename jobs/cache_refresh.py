"""
Фонова задача: періодичне повне перезавантаження CacheStore з Google Sheets.

Будує НОВИЙ CacheStore повністю окремо від поточного (нульовий ризик
показати handler'у напівзаповнений стан), і лише в кінці атомарно
підміняє вміст робочого кешу через CacheStore.replace_with().

gspread синхронний -> усі read_* виклики SheetsClient загорнуті в
asyncio.to_thread, щоб не блокувати event loop (де паралельно живуть
aiogram-polling і FastAPI webhook).
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

from storage.cache_store import CacheStore
from storage.models import (
    ContentRef,
    Participant,
    ParticipantStatus,
    Plan,
    PlanType,
    Stage,
    Stream,
)
from storage.sheets_client import SheetsClient

logger = logging.getLogger(__name__)


def _parse_bool(value) -> bool:
    """Google Sheets віддає булеві значення по-різному залежно від формату клітинки."""
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in ("true", "1", "так", "yes")
    return bool(value)


def _parse_int(value, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _parse_datetime(value) -> datetime | None:
    """
    Очікує ISO-подібний рядок з Sheets (напр. '2026-07-01 10:00:00').
    Порожнє значення -> None (для INSTANT-тарифів start_date не заповнюється).
    """
    if not value:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    text = str(value).strip()
    if not text:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            dt = datetime.strptime(text, fmt)
            return dt.replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    logger.warning("Не вдалося розпарсити дату/час: %r", value)
    return None


def _parse_content_ref(chat_id_raw, message_id_raw, file_id_raw=None) -> "ContentRef | None":
    """
    Будує ContentRef з колонок Sheets.
    Якщо є file_id — використовується для send_media_group.
    Якщо є chat_id + message_id — для copy_message.
    """
    file_id = str(file_id_raw).strip() if file_id_raw else None
    chat_id = _parse_int(chat_id_raw, default=0)
    message_id = _parse_int(message_id_raw, default=0)

    if not file_id and not (chat_id and message_id):
        return None

    return ContentRef(
        source_chat_id=chat_id,
        source_message_id=message_id,
        file_id=file_id or None,
    )


def build_cache_from_raw(
    streams_rows: list[dict],
    stages_rows: list[dict],
    plans_rows: list[dict],
    participants_rows: list[dict],
) -> CacheStore:
    """
    Чиста функція: сирі рядки (як їх повертає gspread get_all_records)
    -> заповнений CacheStore. Винесена окремо від async-обгортки нижче,
    щоб можна було юніт-тестити без жодного asyncio/мережі.

    Очікувані колонки листа Stages для відео-контенту (замість прямих
    URL — посилання на повідомлення в адмін-каналі, див. ContentRef):
      video_chat_id, video_message_id           — основний відео-урок
      circle_1_chat_id, circle_1_message_id     — кружечок 1
      circle_2_chat_id, circle_2_message_id     — кружечок 2
      circle_3_chat_id, circle_3_message_id     — кружечок 3 (опційно)
    """
    cache = CacheStore()

    for row in streams_rows:
        stream_id = str(row.get("stream_id", "")).strip()
        if not stream_id:
            continue
        cache.streams[stream_id] = Stream(
            stream_id=stream_id,
            title=row.get("title", ""),
            is_active=_parse_bool(row.get("is_active", True)),
        )

    for row in sorted(stages_rows, key=lambda r: _parse_int(r.get("order"))):
        stream_id = str(row.get("stream_id", "")).strip()
        stream = cache.streams.get(stream_id)
        if stream is None:
            logger.warning("Stage %r посилається на невідомий stream_id=%r — пропущено", row.get("stage_id"), stream_id)
            continue

        circle_refs = [
            _parse_content_ref(row.get("circle_1_chat_id"), row.get("circle_1_message_id")),
            _parse_content_ref(row.get("circle_2_chat_id"), row.get("circle_2_message_id")),
            _parse_content_ref(row.get("circle_3_chat_id"), row.get("circle_3_message_id")),
        ]

        # медіагрупа — до 10 елементів (ліміт Telegram)
        media_group = [
            r for r in [
                _parse_content_ref(row.get("media_1_chat_id"), row.get("media_1_message_id"), row.get("media_1_file_id")),
                _parse_content_ref(row.get("media_2_chat_id"), row.get("media_2_message_id"), row.get("media_2_file_id")),
                _parse_content_ref(row.get("media_3_chat_id"), row.get("media_3_message_id"), row.get("media_3_file_id")),
                _parse_content_ref(row.get("media_4_chat_id"), row.get("media_4_message_id"), row.get("media_4_file_id")),
                _parse_content_ref(row.get("media_5_chat_id"), row.get("media_5_message_id"), row.get("media_5_file_id")),
                _parse_content_ref(row.get("media_6_chat_id"), row.get("media_6_message_id"), row.get("media_6_file_id")),
                _parse_content_ref(row.get("media_7_chat_id"), row.get("media_7_message_id"), row.get("media_7_file_id")),
                _parse_content_ref(row.get("media_8_chat_id"), row.get("media_8_message_id"), row.get("media_8_file_id")),
                _parse_content_ref(row.get("media_9_chat_id"), row.get("media_9_message_id"), row.get("media_9_file_id")),
                _parse_content_ref(row.get("media_10_chat_id"), row.get("media_10_message_id"), row.get("media_10_file_id")),
            ] if r is not None
        ]

        if media_group:
            logger.info(
                "Stage %s: знайдено media_group (%d елементів), file_ids: %s",
                row.get("stage_id"),
                len(media_group),
                [ref.file_id for ref in media_group],
            )

        stream.stages.append(Stage(
            stage_id=row.get("stage_id", ""),
            stream_id=stream_id,
            order=_parse_int(row.get("order")),
            title=row.get("title", ""),
            video_ref=_parse_content_ref(row.get("video_chat_id"), row.get("video_message_id")),
            notes_text=row.get("notes_text", ""),
            circle_refs=circle_refs,
            media_group=media_group,
            unlock_button_text=row.get("unlock_button_text") or "Далі",
            is_active=_parse_bool(row.get("is_active", True)),
        ))

    for row in plans_rows:
        stream_id = str(row.get("stream_id", "")).strip()
        stream = cache.streams.get(stream_id)
        if stream is None:
            logger.warning("Plan %r посилається на невідомий stream_id=%r — пропущено", row.get("plan_id"), stream_id)
            continue
        plan_id = str(row.get("plan_id", "")).strip()
        if not plan_id:
            continue
        raw_type = str(row.get("plan_type", "")).strip().lower()
        try:
            plan_type = PlanType(raw_type)
        except ValueError:
            logger.warning("Невідомий plan_type=%r для plan_id=%r — пропущено", raw_type, plan_id)
            continue
        stream.plans[plan_id] = Plan(
            plan_id=plan_id,
            stream_id=stream_id,
            plan_type=plan_type,
            title=row.get("title", ""),
            start_date=_parse_datetime(row.get("start_date")),
            is_active=_parse_bool(row.get("is_active", True)),
        )

    for i, row in enumerate(participants_rows):
        participant_id = str(row.get("participant_id", "")).strip()
        if not participant_id:
            continue
        raw_status = str(row.get("status", "")).strip().lower()
        try:
            status = ParticipantStatus(raw_status)
        except ValueError:
            logger.warning("Невідомий status=%r для participant_id=%r — встановлено PENDING", raw_status, participant_id)
            status = ParticipantStatus.PENDING

        telegram_id_raw = row.get("telegram_id")
        telegram_id = _parse_int(telegram_id_raw) if telegram_id_raw not in (None, "") else None

        participant = Participant(
            participant_id=participant_id,
            telegram_id=telegram_id,
            telegram_username=(row.get("telegram_username") or None),
            phone_number=(row.get("phone_number") or None),
            stream_id=str(row.get("stream_id", "")).strip(),
            plan_id=str(row.get("plan_id", "")).strip(),
            access_token=row.get("access_token", ""),
            token_used=_parse_bool(row.get("token_used", False)),
            status=status,
            current_stage_order=_parse_int(row.get("current_stage_order")),
            fsm_state=(row.get("fsm_state") or None),
            notification_sent=_parse_bool(row.get("notification_sent", False)),
            row_index=i + 2,  # рядок 1 — заголовок, дані з рядка 2
            joined_at=_parse_datetime(row.get("joined_at")),
            activated_at=_parse_datetime(row.get("activated_at")),
            last_progress_at=_parse_datetime(row.get("last_progress_at")),
        )
        cache.upsert_participant(participant)

    cache.last_synced_at = datetime.now(timezone.utc)
    return cache


async def refresh_cache_once(cache: CacheStore, sheets: SheetsClient) -> None:
    """Один прохід оновлення: читає всі 4 листи й атомарно підміняє кеш."""
    streams_rows, stages_rows, plans_rows, participants_rows = await asyncio.gather(
        asyncio.to_thread(sheets.read_streams),
        asyncio.to_thread(sheets.read_stages),
        asyncio.to_thread(sheets.read_plans),
        asyncio.to_thread(sheets.read_participants),
    )

    new_cache = build_cache_from_raw(streams_rows, stages_rows, plans_rows, participants_rows)
    cache.replace_with(new_cache)

    logger.info(
        "Кеш оновлено: %d потоків, %d учасників",
        len(cache.streams), len(cache.participants_by_id),
    )


async def cache_refresh_loop(cache: CacheStore, sheets: SheetsClient, interval_sec: int) -> None:
    """
    Нескінченний цикл оновлення кешу. Перший прохід відбувається одразу
    при старті (до старту polling — див. main.py), далі — кожні interval_sec.

    Помилки одного проходу логуються, але НЕ зупиняють цикл — тимчасова
    недоступність Google Sheets API не повинна валити весь бот-процес,
    бот продовжує працювати зі старим (можливо трохи застарілим) кешем.
    """
    while True:
        try:
            await refresh_cache_once(cache, sheets)
        except Exception:
            logger.exception("Помилка під час оновлення кешу — лишаємо попередній стан кешу")
        await asyncio.sleep(interval_sec)