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

from storage.cache_store import CacheStore, LeadRow, normalize_phone
from storage.models import (
    ContentRef,
    MediaType,
    Participant,
    ParticipantStatus,
    Plan,
    PlanType,
    Stage,
    Stream,
)
from storage.sheets_client import SheetsClient
from storage.write_queue import AppendRow, PendingWrite, WriteQueue
from webhook.handlers import (
    LEAD_STATUS_PAID,
    LEADS_COL_PAID_AT,
    LEADS_COL_PLAN,
    LEADS_COL_STATUS,
    LEADS_COL_STREAM,
    PARTICIPANTS_COLUMN_ORDER,
    SHEET_LEADS,
)

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


def _parse_content_ref(
    chat_id_raw,
    message_id_raw,
    file_id_raw=None,
    media_type_raw=None,
    default_media_type: MediaType = MediaType.VIDEO,
) -> "ContentRef | None":
    """
    Будує ContentRef з колонок Sheets.
    - file_id + media_type → send_video / send_voice / send_audio
    - chat_id + message_id → copy_message (universal fallback)

    media_type_raw: рядок із колонки media_N_type / video_type.
    Якщо порожньо — використовується default_media_type.
    """
    file_id = str(file_id_raw).strip() if file_id_raw else None
    chat_id = _parse_int(chat_id_raw, default=0)
    message_id = _parse_int(message_id_raw, default=0)

    if not file_id and not (chat_id and message_id):
        return None

    media_type = MediaType.from_raw(media_type_raw, default=default_media_type)

    return ContentRef(
        source_chat_id=chat_id,
        source_message_id=message_id,
        file_id=file_id or None,
        media_type=media_type,
    )


def build_cache_from_raw(
    streams_rows: list[dict],
    stages_rows: list[dict],
    plans_rows: list[dict],
    participants_rows: list[dict],
    leads_rows: list[dict] | None = None,
) -> CacheStore:
    """
    Чиста функція: сирі рядки (як їх повертає gspread get_all_records)
    -> заповнений CacheStore. Винесена окремо від async-обгортки нижче,
    щоб можна було юніт-тестити без жодного asyncio/мережі.

    Очікувані колонки листа Stages для медіаконтенту:
      video_chat_id, video_message_id, video_type        — основний медіафайл
      circle_1_chat_id, circle_1_message_id              — кружечок 1
      circle_2_chat_id, circle_2_message_id              — кружечок 2
      circle_3_chat_id, circle_3_message_id              — кружечок 3 (опційно)
      media_N_chat_id, media_N_message_id,
        media_N_file_id, media_N_type                    — медіагрупа (N = 1..10)
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
            telegram_group_url=(str(row.get("telegram_group_url")).strip() or None)
            if row.get("telegram_group_url") else None,
        )

    for row in sorted(stages_rows, key=lambda r: _parse_int(r.get("order"))):
        stream_id = str(row.get("stream_id", "")).strip()
        stream = cache.streams.get(stream_id)
        if stream is None:
            logger.warning(
                "Stage %r посилається на невідомий stream_id=%r — пропущено",
                row.get("stage_id"), stream_id,
            )
            continue

        circle_refs = [
            _parse_content_ref(row.get("circle_1_chat_id"), row.get("circle_1_message_id")),
            _parse_content_ref(row.get("circle_2_chat_id"), row.get("circle_2_message_id")),
            _parse_content_ref(row.get("circle_3_chat_id"), row.get("circle_3_message_id")),
        ]

        # медіагрупа — до 10 елементів (ліміт Telegram)
        media_group = [
            r for r in [
                _parse_content_ref(
                    row.get("media_1_chat_id"), row.get("media_1_message_id"),
                    row.get("media_1_file_id"), row.get("media_1_type"),
                ),
                _parse_content_ref(
                    row.get("media_2_chat_id"), row.get("media_2_message_id"),
                    row.get("media_2_file_id"), row.get("media_2_type"),
                ),
                _parse_content_ref(
                    row.get("media_3_chat_id"), row.get("media_3_message_id"),
                    row.get("media_3_file_id"), row.get("media_3_type"),
                ),
                _parse_content_ref(
                    row.get("media_4_chat_id"), row.get("media_4_message_id"),
                    row.get("media_4_file_id"), row.get("media_4_type"),
                ),
                _parse_content_ref(
                    row.get("media_5_chat_id"), row.get("media_5_message_id"),
                    row.get("media_5_file_id"), row.get("media_5_type"),
                ),
                _parse_content_ref(
                    row.get("media_6_chat_id"), row.get("media_6_message_id"),
                    row.get("media_6_file_id"), row.get("media_6_type"),
                ),
                _parse_content_ref(
                    row.get("media_7_chat_id"), row.get("media_7_message_id"),
                    row.get("media_7_file_id"), row.get("media_7_type"),
                ),
                _parse_content_ref(
                    row.get("media_8_chat_id"), row.get("media_8_message_id"),
                    row.get("media_8_file_id"), row.get("media_8_type"),
                ),
                _parse_content_ref(
                    row.get("media_9_chat_id"), row.get("media_9_message_id"),
                    row.get("media_9_file_id"), row.get("media_9_type"),
                ),
                _parse_content_ref(
                    row.get("media_10_chat_id"), row.get("media_10_message_id"),
                    row.get("media_10_file_id"), row.get("media_10_type"),
                ),
            ] if r is not None
        ]

        if media_group:
            logger.info(
                "Stage %s: знайдено media_group (%d елементів), types/file_ids: %s",
                row.get("stage_id"),
                len(media_group),
                [(ref.media_type.value, ref.file_id) for ref in media_group],
            )

        stream.stages.append(Stage(
            stage_id=row.get("stage_id", ""),
            stream_id=stream_id,
            order=_parse_int(row.get("order")),
            title=row.get("title", ""),
            video_ref=_parse_content_ref(
                row.get("video_chat_id"), row.get("video_message_id"),
                media_type_raw=row.get("video_type"),
            ),
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
            logger.warning(
                "Plan %r посилається на невідомий stream_id=%r — пропущено",
                row.get("plan_id"), stream_id,
            )
            continue
        plan_id = str(row.get("plan_id", "")).strip()
        if not plan_id:
            continue
        raw_type = str(row.get("plan_type", "")).strip().lower()
        try:
            plan_type = PlanType(raw_type)
        except ValueError:
            logger.warning(
                "Невідомий plan_type=%r для plan_id=%r — пропущено", raw_type, plan_id
            )
            continue
        stream.plans[plan_id] = Plan(
            plan_id=plan_id,
            stream_id=stream_id,
            plan_type=plan_type,
            title=row.get("title", ""),
            start_date=_parse_datetime(row.get("start_date")),
            is_active=_parse_bool(row.get("is_active", True)),
            curator_url=(str(row.get("curator_url")).strip() or None)
            if row.get("curator_url") else None,
        )

    for i, row in enumerate(participants_rows):
        participant = _participant_from_record(row, row_index=i + 2)
        if participant is not None:
            cache.upsert_participant(participant)

    # лист Leads опціональний: None -> облік вимкнено (листа немає)
    if leads_rows is not None:
        cache.leads_enabled = True
        for i, row in enumerate(leads_rows):
            raw_phone = str(row.get("phone_number", "")).strip()
            if not raw_phone:
                continue
            phone = normalize_phone(raw_phone)
            cache.leads_by_phone[phone] = LeadRow(
                phone=phone,
                status=str(row.get("status", "")).strip(),
                row_index=i + 2,  # рядок 1 — заголовок
                raw_phone=raw_phone,
            )

    cache.last_synced_at = datetime.now(timezone.utc)
    return cache


def _participant_from_record(row: dict, row_index: int) -> Participant | None:
    """Будує Participant з dict-рядка (за назвами колонок). None, якщо немає participant_id."""
    participant_id = str(row.get("participant_id", "")).strip()
    if not participant_id:
        return None

    raw_status = str(row.get("status", "")).strip().lower()
    try:
        status = ParticipantStatus(raw_status)
    except ValueError:
        logger.warning(
            "Невідомий status=%r для participant_id=%r — встановлено PENDING",
            raw_status, participant_id,
        )
        status = ParticipantStatus.PENDING

    telegram_id_raw = row.get("telegram_id")
    telegram_id = _parse_int(telegram_id_raw) if telegram_id_raw not in (None, "") else None

    return Participant(
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
        row_index=row_index,
        joined_at=_parse_datetime(row.get("joined_at")),
        activated_at=_parse_datetime(row.get("activated_at")),
        last_progress_at=_parse_datetime(row.get("last_progress_at")),
        reminders_sent=str(row.get("reminders_sent") or "").strip(),
    )


def _parse_status(value) -> ParticipantStatus:
    try:
        return ParticipantStatus(str(value).strip().lower())
    except ValueError:
        return ParticipantStatus.PENDING


# Колонка листа Participants -> (атрибут Participant, конвертер значення).
# Використовується для накладання ще не злитих у Sheets точкових записів
# поверх свіжозчитаного кешу (див. _overlay_pending).
_WRITE_COLUMN_TO_FIELD = {
    "B": ("telegram_id", lambda v: _parse_int(v) if v not in (None, "") else None),
    "C": ("telegram_username", lambda v: (str(v) or None) if v else None),
    "H": ("token_used", _parse_bool),
    "I": ("status", _parse_status),
    "J": ("current_stage_order", _parse_int),
    "K": ("fsm_state", lambda v: (str(v) or None) if v else None),
    "L": ("joined_at", _parse_datetime),
    "M": ("activated_at", _parse_datetime),
    "N": ("last_progress_at", _parse_datetime),
    "O": ("notification_sent", _parse_bool),
    "P": ("reminders_sent", lambda v: str(v or "").strip()),
}


def _overlay_pending(
    new_cache: CacheStore,
    writes: list[PendingWrite],
    appends: list[AppendRow],
) -> None:
    """
    Накладає ще не злиті в Sheets зміни з черги поверх свіжозчитаного кешу.

    Без цього повне перезавантаження кешу затирало б стан, який поки що
    існує лише в пам'яті (напр. telegram_id, щойно прив'язаний через /start,
    але ще не flush-нутий у таблицю) — і бот на короткий час "губив" учасника.

    Спершу — appends (нові учасники, яких ще нема в Sheets), потім — точкові
    writes (новіші оновлення полів), щоб порядок відповідав flush-циклу.
    """
    for a in appends:
        if a.sheet_name != "Participants":
            continue
        row = dict(zip(PARTICIPANTS_COLUMN_ORDER, a.row_values))
        participant = _participant_from_record(row, row_index=-1)
        if participant is not None:
            new_cache.upsert_participant(participant)

    for w in writes:
        if w.sheet_name != "Participants" or not w.participant_id:
            continue
        field = _WRITE_COLUMN_TO_FIELD.get(w.column)
        if field is None:
            continue
        participant = new_cache.get_participant_by_id(w.participant_id)
        if participant is None:
            continue
        attr, convert = field
        setattr(participant, attr, convert(w.value))
        new_cache.upsert_participant(participant)  # переіндексувати tg_id/username/token


async def refresh_cache_once(
    cache: CacheStore,
    sheets: SheetsClient,
    queue: WriteQueue | None = None,
) -> None:
    """Один прохід оновлення: читає листи й атомарно підміняє кеш."""
    streams_rows, stages_rows, plans_rows, participants_rows, leads_rows = await asyncio.gather(
        asyncio.to_thread(sheets.read_streams),
        asyncio.to_thread(sheets.read_stages),
        asyncio.to_thread(sheets.read_plans),
        asyncio.to_thread(sheets.read_participants),
        asyncio.to_thread(sheets.read_leads),
    )

    new_cache = build_cache_from_raw(
        streams_rows, stages_rows, plans_rows, participants_rows, leads_rows,
    )

    # накласти ще не злиті в Sheets зміни з черги, щоб не втратити стан,
    # який поки існує лише в пам'яті (напр. свіжий /start до flush).
    if queue is not None:
        writes, appends = await queue.snapshot()
        _overlay_pending(new_cache, writes, appends)
        # позначити у листі Leads тих, хто вже оплатив (телефон збігається
        # з оплаченим учасником) — статус «оплатив» + дата/потік/тариф
        await _reconcile_paid_leads(new_cache, queue)

    cache.replace_with(new_cache)

    logger.info(
        "Кеш оновлено: %d потоків, %d учасників%s",
        len(cache.streams), len(cache.participants_by_id),
        f", {len(cache.leads_by_phone)} лідів" if cache.leads_enabled else "",
    )


async def _reconcile_paid_leads(new_cache: CacheStore, queue: WriteQueue) -> None:
    """
    Для кожного ліда у листі Leads, чий телефон збігається з оплаченим
    учасником, ставить статус «оплатив» (+ дата/потік/тариф). Ідемпотентно:
    якщо статус уже «оплатив» — пропускаємо; дедуплікація черги не дасть
    накопичити повторні записи між flush-ами.
    """
    if not new_cache.leads_enabled or not new_cache.leads_by_phone:
        return

    # телефон -> учасник (оплачені = ті, хто взагалі є у Participants)
    paid_by_phone: dict[str, "object"] = {}
    for p in new_cache.all_participants():
        if p.phone_number:
            paid_by_phone[normalize_phone(p.phone_number)] = p

    for lead in new_cache.leads_by_phone.values():
        if lead.status.strip().lower() == LEAD_STATUS_PAID.lower():
            continue
        participant = paid_by_phone.get(lead.phone)
        if participant is None or lead.row_index < 2:
            continue
        pid = participant.participant_id
        await queue.enqueue(PendingWrite(
            sheet_name=SHEET_LEADS, row_index=lead.row_index,
            column=LEADS_COL_STATUS, value=LEAD_STATUS_PAID, participant_id=pid,
        ))
        await queue.enqueue(PendingWrite(
            sheet_name=SHEET_LEADS, row_index=lead.row_index,
            column=LEADS_COL_PAID_AT, value=_now_iso_sheets(), participant_id=pid,
        ))
        await queue.enqueue(PendingWrite(
            sheet_name=SHEET_LEADS, row_index=lead.row_index,
            column=LEADS_COL_STREAM, value=participant.stream_id, participant_id=pid,
        ))
        await queue.enqueue(PendingWrite(
            sheet_name=SHEET_LEADS, row_index=lead.row_index,
            column=LEADS_COL_PLAN, value=participant.plan_id, participant_id=pid,
        ))


def _now_iso_sheets() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


async def cache_refresh_loop(
    cache: CacheStore,
    sheets: SheetsClient,
    interval_sec: int,
    queue: WriteQueue | None = None,
) -> None:
    """
    Нескінченний цикл оновлення кешу. Перший прохід відбувається одразу
    при старті (до старту polling — див. main.py), далі — кожні interval_sec.

    Помилки одного проходу логуються, але НЕ зупиняють цикл — тимчасова
    недоступність Google Sheets API не повинна валити весь бот-процес,
    бот продовжує працювати зі старим (можливо трохи застарілим) кешем.
    """
    while True:
        try:
            await refresh_cache_once(cache, sheets, queue)
        except Exception:
            logger.exception("Помилка під час оновлення кешу — лишаємо попередній стан кешу")
        await asyncio.sleep(interval_sec)