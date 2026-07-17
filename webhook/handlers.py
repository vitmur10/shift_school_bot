"""Бізнес-логіка обробки webhook: лід з форми Webflow -> оплата WayForPay -> Participant."""

from __future__ import annotations

import hashlib
import hmac
import logging
import time
import uuid
from datetime import datetime, timezone


def _now_iso() -> str:
    """Час у форматі, який очікує _parse_datetime (напр. 2026-07-17 13:40:00)."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

from config import settings
from services.token_service import generate_token
from storage.cache_store import CacheStore, Lead, normalize_phone
from storage.models import Participant, ParticipantStatus
from storage.write_queue import AppendRow, WriteQueue
from webhook.schemas import WayForPayCallback, WebflowFormSubmission

logger = logging.getLogger(__name__)

SHEET_PARTICIPANTS = "Participants"
SHEET_LEADS = "Leads"

# статуси у листі Leads
LEAD_STATUS_PENDING = "очікує оплату"
LEAD_STATUS_PAID = "оплатив"

# порядок колонок листа Leads (A..H). Заповнюється при заявці з форми;
# статус/дата оплати/потік/тариф дозаповнюються після успішної оплати.
LEADS_COLUMN_ORDER = [
    "created_at", "phone_number", "telegram_username", "email",
    "status", "paid_at", "stream_id", "plan_id",
]
LEADS_COL_STATUS = "E"
LEADS_COL_PAID_AT = "F"
LEADS_COL_STREAM = "G"
LEADS_COL_PLAN = "H"

# порядок МАЄ збігатись з порядком колонок у листі Participants (A..P)
PARTICIPANTS_COLUMN_ORDER = [
    "participant_id", "telegram_id", "telegram_username", "phone_number",
    "stream_id", "plan_id", "access_token", "token_used", "status",
    "current_stage_order", "fsm_state",
    "joined_at", "activated_at", "last_progress_at", "notification_sent",
    "reminders_sent",
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
    "L": 11,  # joined_at
    "M": 12,  # activated_at
    "N": 13,  # last_progress_at
    "O": 14,  # notification_sent (узгоджено з jobs/scheduled_activation.py COL_NOTIFICATION_SENT)
    "P": 15,  # reminders_sent (узгоджено з jobs/scheduled_activation.py COL_REMINDERS_SENT)
}


# =========================================================================
# КРОК 1: Webflow form_submission — лише збір контактів, БЕЗ запису в Sheets
# =========================================================================

async def handle_webflow_lead(
    submission: WebflowFormSubmission,
    cache: CacheStore,
    queue: WriteQueue | None = None,
) -> Lead:
    """
    Обробляє сирий form_submission webhook від Webflow (без оплати).

    На цьому етапі stream_id/plan_id ще невідомі — вони прийдуть пізніше
    разом з callback-ом WayForPay. Лід кладемо у пам'ять
    (CacheStore.pending_leads) для подальшого матчингу за телефоном при
    оплаті, а також (якщо є лист Leads) дописуємо рядок «очікує оплату»
    у таблицю — щоб бачити тих, хто заповнив форму, але не оплатив.
    """
    logger.debug("Отримано webhook form_submission від Webflow: %r", submission)

    data = submission.payload.data
    lead = Lead(
        phone_number=normalize_phone(data.phone),
        telegram_username=(data.Telegram or None),
        raw_phone=data.phone,
        email=data.Email,
    )
    cache.upsert_lead(lead)

    # дописати рядок у лист Leads (лише якщо облік увімкнено — тобто лист існує).
    # Пропускаємо, якщо цей телефон уже є у Leads, щоб не плодити дублі.
    if queue is not None and cache.leads_enabled and cache.get_lead_row(lead.phone_number) is None:
        row_values = [
            _now_iso(),                 # A created_at
            lead.raw_phone,             # B phone_number
            lead.telegram_username or "",  # C telegram_username
            lead.email or "",           # D email
            LEAD_STATUS_PENDING,        # E status
            "",                         # F paid_at
            "",                         # G stream_id
            "",                         # H plan_id
        ]
        await queue.enqueue_append(AppendRow(
            sheet_name=SHEET_LEADS,
            row_values=row_values,
        ))

    logger.info(
        "Новий лід з форми Webflow: phone=%s telegram=%s (очікує оплату)",
        lead.raw_phone, lead.telegram_username,
    )
    return lead


# =========================================================================
# КРОК 2: WayForPay callback — підтвердження оплати -> створення Participant
# =========================================================================

def _wayforpay_signature(fields: list[str]) -> str:
    """HMAC_MD5 за алгоритмом WayForPay: поля через ';', ключ — merchantSecretKey."""
    message = ";".join(str(f) for f in fields)
    return hmac.new(
        settings.WAYFORPAY_MERCHANT_SECRET_KEY.encode("utf-8"),
        message.encode("utf-8"),
        hashlib.md5,
    ).hexdigest()


def _sig_value(raw: dict | None, key: str, fallback) -> str:
    """
    Значення поля для рядка підпису. Беремо СИРЕ значення з JSON (raw),
    щоб не зловити коерсію Pydantic (наприклад amount 1 -> 1.0), яка ламає
    HMAC. Якщо raw немає — падаємо на значення з моделі.
    """
    val = raw.get(key) if isinstance(raw, dict) else None
    if val is None:
        val = fallback
    if val is None:
        return ""
    return str(val)


def verify_wayforpay_signature(
    callback: WayForPayCallback,
    raw: dict | None = None,
) -> bool:
    """
    Перевіряє merchantSignature вхідного callback-у.
    Порядок полів для запиту (serviceUrl) за докою WayForPay:
    merchantAccount;orderReference;amount;currency;authCode;cardPan;transactionStatus;reasonCode

    raw -- оригінальний розпарсений JSON. Підпис рахуємо саме з нього, бо
    Pydantic приводить типи (amount 1 -> 1.0), а WayForPay підписував рядок
    з початковим форматом числа.
    """
    expected = _wayforpay_signature([
        _sig_value(raw, "merchantAccount", callback.merchantAccount),
        _sig_value(raw, "orderReference", callback.orderReference),
        _sig_value(raw, "amount", callback.amount),
        _sig_value(raw, "currency", callback.currency),
        _sig_value(raw, "authCode", callback.authCode or ""),
        _sig_value(raw, "cardPan", callback.cardPan or ""),
        _sig_value(raw, "transactionStatus", callback.transactionStatus),
        _sig_value(raw, "reasonCode", callback.reasonCode if callback.reasonCode is not None else ""),
    ])
    is_valid = hmac.compare_digest(expected, callback.merchantSignature)
    if not is_valid:
        logger.warning(
            "Невірний merchantSignature у callback WayForPay: orderReference=%s",
            callback.orderReference,
        )
    return is_valid


def build_wayforpay_accept_response(order_reference: str) -> dict:
    """
    Формує обов'язкову JSON-квитанцію для WayForPay. Якщо не відповісти
    в такому форматі (або взагалі не відповісти 200), WayForPay вважатиме
    доставку невдалою і буде повторювати callback.
    """
    ts = int(time.time())
    signature = _wayforpay_signature([order_reference, "accept", ts])
    return {
        "orderReference": order_reference,
        "status": "accept",
        "time": ts,
        "signature": signature,
    }


def _resolve_stream_and_plan(callback: WayForPayCallback) -> tuple[str, str]:
    """
    Визначає stream_id/plan_id за назвою продукту з callback-у, використовуючи
    мапу settings.wayforpay_product_map (див. config.py, .env: WAYFORPAY_PRODUCT_MAP).

    Якщо продукт не знайдено в мапі — НЕ відкидаємо оплату (гроші вже списані!),
    а повертаємо ("UNKNOWN", "UNKNOWN") і голосно логуємо ERROR, щоб адмін
    вручну доправив рядок у Sheets. Тихо загубити оплаченого клієнта — гірше,
    ніж один раз розібратись руками.
    """
    product_names = callback.productName or []
    product_map = settings.wayforpay_product_map

    for name in product_names:
        if name in product_map:
            return product_map[name]

    logger.error(
        "Не вдалось визначити stream_id/plan_id для оплати orderReference=%s: "
        "productName=%r не знайдено у WAYFORPAY_PRODUCT_MAP. "
        "Учасника буде створено з stream_id/plan_id='UNKNOWN' -- ПОТРІБНО виправити вручну в Sheets!",
        callback.orderReference, product_names,
    )
    return "UNKNOWN", "UNKNOWN"


async def handle_wayforpay_payment(
    callback: WayForPayCallback,
    cache: CacheStore,
    queue: WriteQueue,
) -> Participant | None:
    """
    Обробляє підтверджену оплату WayForPay: знаходить лід за телефоном
    (залишений формою Webflow) і перетворює його на повноцінного Participant
    у статусі ACTIVE — саме тут, і лише тут, відбувається реальний запис
    у Sheets.

    Повертає None, якщо оплата НЕ Approved (наприклад Declined/Pending) —
    в цьому разі Participant не створюється, лід лишається в очікуванні.
    """
    logger.debug("Отримано callback від WayForPay: %r", callback)

    if callback.transactionStatus != "Approved":
        logger.info(
            "Оплата orderReference=%s має статус %s (не Approved) -- Participant не створюється",
            callback.orderReference, callback.transactionStatus,
        )
        return None

    phone_source = callback.phone or ""
    lead = cache.pop_lead(phone_source) if phone_source else None

    if lead is None:
        logger.warning(
            "Оплата orderReference=%s Approved, але лід за телефоном %r не знайдено в кеші "
            "(форму або не заповнювали, або телефон у WayForPay не збігається з формою). "
            "Participant буде створено лише з даними оплати, без Telegram-username.",
            callback.orderReference, phone_source,
        )

    stream_id, plan_id = _resolve_stream_and_plan(callback)

    telegram_username = lead.telegram_username if lead else None
    phone_number = (lead.raw_phone if lead else phone_source) or None

    # Учасника МОЖНА зв'язати з /start лише за telegram_username АБО phone_number
    # (див. storage/access_control -- пошук іде саме за ними). Якщо оплата
    # прийшла без обох -- Participant все одно створюємо (гроші вже списані,
    # мовчки губити оплаченого клієнта не можна), але це аварійна ситуація,
    # яку потрібно розібрати вручну: без жодного ідентифікатора людина ніколи
    # не зможе прив'язати токен через бота.
    if not telegram_username and not phone_number:
        logger.error(
            "КРИТИЧНО: оплата orderReference=%s Approved, але немає НІ telegram_username, "
            "НІ phone_number (лід не знайдено, callback.phone порожній). "
            "Participant буде створено без жодного ідентифікатора -- "
            "прив'язати токен через /start буде НЕМОЖЛИВО, поки хтось вручну "
            "не пропише телефон/username у Sheets.",
            callback.orderReference,
        )

    participant = Participant(
        participant_id=str(uuid.uuid4()),
        telegram_id=None,
        telegram_username=telegram_username,
        phone_number=phone_number,
        stream_id=stream_id,
        plan_id=plan_id,
        access_token=generate_token(),
        token_used=False,
        status=ParticipantStatus.ACTIVE,
        current_stage_order=0,
        fsm_state=None,
        notification_sent=False,
        row_index=-1,  # ще не записаний у Sheets -- реальний row_index з'явиться при наступному refresh
    )

    logger.debug("Створено Participant з підтвердженої оплати: %r", participant)
    cache.upsert_participant(participant)

    row_values = [
        participant.participant_id,            # A participant_id
        "",                                    # B telegram_id (проставиться при /start)
        participant.telegram_username or "",   # C telegram_username
        participant.phone_number,              # D phone_number
        participant.stream_id,                 # E stream_id
        participant.plan_id,                   # F plan_id
        participant.access_token,              # G access_token
        participant.token_used,                # H token_used
        participant.status.value,              # I status
        participant.current_stage_order,       # J current_stage_order
        "",                                    # K fsm_state
        "",                                    # L joined_at
        "",                                    # M activated_at
        "",                                    # N last_progress_at
        participant.notification_sent,         # O notification_sent
        participant.reminders_sent,            # P reminders_sent
    ]
    logger.debug(
        "row_values для append у Sheets (%s), порядок колонок %s: %r",
        SHEET_PARTICIPANTS, PARTICIPANTS_COLUMN_ORDER, row_values,
    )
    await queue.enqueue_append(AppendRow(
        sheet_name=SHEET_PARTICIPANTS,
        row_values=row_values,
        participant_id=participant.participant_id,
    ))

    logger.info(
        "Новий учасник створено після оплати WayForPay: participant_id=%s stream=%s plan=%s",
        participant.participant_id, stream_id, plan_id,
    )
    return participant