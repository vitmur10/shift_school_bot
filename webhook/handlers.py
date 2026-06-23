"""Бізнес-логіка обробки webhook: створення Participant + токена."""

from __future__ import annotations

import logging
import uuid

from services.token_service import generate_token
from storage.cache_store import CacheStore
from storage.models import Participant, ParticipantStatus
from storage.write_queue import AppendRow, WriteQueue
from webhook.schemas import WebflowPaymentPayload

logger = logging.getLogger(__name__)

SHEET_PARTICIPANTS = "Participants"

# порядок МАЄ збігатись з порядком колонок у листі Participants
PARTICIPANTS_COLUMN_ORDER = [
    "participant_id", "telegram_id", "telegram_username", "phone_number",
    "stream_id", "plan_id", "access_token", "token_used", "status",
    "current_stage_order", "fsm_state", "notification_sent",
]

# буква колонки Sheets -> позиція в PARTICIPANTS_COLUMN_ORDER / row_values.
# Використовується WriteQueue, щоб патчити ще не відправлений AppendRow
# напряму замість точкового PendingWrite з невідомим (ще) row_index —
# див. docstring WriteQueue._try_patch_pending_append.
PARTICIPANTS_COLUMN_MAP = {
    "A": 0,  # participant_id
    "B": 1,  # telegram_id
    "C": 2,  # telegram_username
    "D": 3,  # phone_number
    "E": 4,  # stream_id
    "F": 5,  # plan_id
    "G": 6,  # access_token
    "H": 7,  # token_used
    "I": 8,  # status
    "J": 9,  # current_stage_order
    "K": 10,  # fsm_state
    "N": 11,  # notification_sent (буква N навмисно — узгоджено з jobs/scheduled_activation.py COL_NOTIFICATION_SENT)
}


async def handle_webflow_payment(
    payload: WebflowPaymentPayload,
    cache: CacheStore,
    queue: WriteQueue,
) -> Participant:
    """
    Створює нового Participant (status=ACTIVE для коректної подальшої
    обробки в access_control: PENDING зарезервовано саме для SCHEDULED-
    очікування активації, INSTANT-учасник одразу ACTIVE і отримує
    доступ після прив'язки токена при /start).

    Кладе одразу в кеш (видно наступному /start без очікування
    наступного refresh) + в чергу як append-рядок для Sheets.
    """
    participant = Participant(
        participant_id=str(uuid.uuid4()),
        telegram_id=None,
        telegram_username=(payload.telegram_username or None),
        phone_number=payload.phone_number,
        stream_id=payload.stream_id,
        plan_id=payload.plan_id,
        access_token=generate_token(),
        token_used=False,
        status=ParticipantStatus.ACTIVE,
        current_stage_order=0,
        fsm_state=None,
        notification_sent=False,
        row_index=-1,  # ще не записаний у Sheets -- реальний row_index з'явиться при наступному refresh
    )

    cache.upsert_participant(participant)

    row_values = [
        participant.participant_id, "", participant.telegram_username or "",
        participant.phone_number, participant.stream_id, participant.plan_id,
        participant.access_token, participant.token_used, participant.status.value,
        participant.current_stage_order, "", participant.notification_sent,
    ]
    await queue.enqueue_append(AppendRow(
        sheet_name=SHEET_PARTICIPANTS,
        row_values=row_values,
        participant_id=participant.participant_id,
    ))

    logger.info("Новий учасник створено з Webflow: participant_id=%s", participant.participant_id)
    return participant