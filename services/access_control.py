"""
Перевірка доступу: чи відкрито курс/етап для учасника, логіка тарифів.

INSTANT (Тариф 1): доступ є одразу, якщо participant.status == ACTIVE.
SCHEDULED (Тариф 2): доступ є тільки якщо настала plan.start_date —
групова активація (jobs/scheduled_activation.py) переводить статус
PENDING -> ACTIVE рівно в момент настання дати, а до того моменту,
навіть якщо токен активовано, contentу не видаємо.

Ця логіка НЕ звертається до Sheets/кешу напряму для запису — лише
читає й повертає рішення. Запис змін (просування current_stage_order)
робить виклик, що приймає рішення (handler), через окрему функцію
advance_to_next_stage нижче.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum

from storage.cache_store import CacheStore
from storage.models import Participant, ParticipantStatus, Plan, PlanType, Stage, Stream
from storage.write_queue import PendingWrite, WriteQueue

COL_CURRENT_STAGE_ORDER = "J"
COL_STATUS = "I"


class AccessDecision(str, Enum):
    GRANTED = "granted"                    # доступ є, можна видавати контент
    BLOCKED_STATUS = "blocked_status"       # status == BLOCKED/PAUSED
    NOT_YET_SCHEDULED = "not_yet_scheduled"  # Тариф 2, дата ще не настала
    NO_STREAM = "no_stream"                 # stream_id з Participant не знайдено в кеші (дані розсинхронізовані)
    NO_PLAN = "no_plan"                     # аналогічно для plan_id
    NO_MORE_STAGES = "no_more_stages"       # курс пройдено повністю


@dataclass
class AccessCheckResult:
    decision: AccessDecision

    @property
    def granted(self) -> bool:
        return self.decision == AccessDecision.GRANTED


def check_course_access(cache: CacheStore, participant: Participant) -> AccessCheckResult:
    """
    Перевіряє, чи учасник взагалі має право бачити контент курсу
    (незалежно від конкретного етапу) — викликається перед видачею
    будь-якого етапу.
    """
    if participant.status in (ParticipantStatus.BLOCKED, ParticipantStatus.PAUSED):
        return AccessCheckResult(AccessDecision.BLOCKED_STATUS)

    stream = cache.get_stream(participant.stream_id)
    if stream is None:
        return AccessCheckResult(AccessDecision.NO_STREAM)

    plan = stream.get_plan(participant.plan_id)
    if plan is None:
        return AccessCheckResult(AccessDecision.NO_PLAN)

    if plan.plan_type == PlanType.SCHEDULED:
        # PENDING для scheduled-тарифу означає "чекаємо групову активацію";
        # навіть якщо start_date вже минула, але джоба ще не встигла
        # перевести в ACTIVE — вважаємо доступ не відкритим (джоба
        # відповідає за єдину точку істини "коли саме відкрилось")
        if participant.status != ParticipantStatus.ACTIVE:
            return AccessCheckResult(AccessDecision.NOT_YET_SCHEDULED)

    # INSTANT: ACTIVE достатньо саме по собі
    if participant.status != ParticipantStatus.ACTIVE:
        return AccessCheckResult(AccessDecision.NOT_YET_SCHEDULED)

    return AccessCheckResult(AccessDecision.GRANTED)


def get_current_stage(cache: CacheStore, participant: Participant) -> Stage | None:
    """Повертає етап, який зараз бачить учасник (відповідно до current_stage_order)."""
    stream = cache.get_stream(participant.stream_id)
    if stream is None:
        return None
    if participant.current_stage_order <= 0:
        return None
    return stream.get_stage(participant.current_stage_order)


def get_next_stage(cache: CacheStore, participant: Participant) -> Stage | None:
    """Повертає НАСТУПНИЙ етап (для попереднього показу перед видачею) або None, якщо курс завершено."""
    stream = cache.get_stream(participant.stream_id)
    if stream is None:
        return None
    next_order = participant.current_stage_order + 1
    return stream.get_stage(next_order)


async def advance_to_next_stage(
    cache: CacheStore,
    queue: WriteQueue,
    participant: Participant,
) -> tuple[AccessCheckResult, Stage | None]:
    """
    Просуває учасника на наступний етап (натискання "Далі").

    Повертає (результат_перевірки_доступу, новий_етап).
    Якщо access не GRANTED — стан НЕ змінюється, новий_етап буде None.
    Якщо етапів більше немає — повертає NO_MORE_STAGES, стан не змінюється.
    """
    access = check_course_access(cache, participant)
    if not access.granted:
        return access, None

    stream = cache.get_stream(participant.stream_id)
    next_stage = get_next_stage(cache, participant)
    if next_stage is None:
        return AccessCheckResult(AccessDecision.NO_MORE_STAGES), None

    participant.current_stage_order = next_stage.order
    participant.last_progress_at = datetime.now(timezone.utc)
    cache.upsert_participant(participant)

    await queue.enqueue(PendingWrite(
        sheet_name="Participants",
        row_index=participant.row_index,
        column=COL_CURRENT_STAGE_ORDER,
        value=next_stage.order,
        participant_id=participant.participant_id,
    ))

    return AccessCheckResult(AccessDecision.GRANTED), next_stage


async def activate_scheduled_plan(
    cache: CacheStore,
    queue: WriteQueue,
    participant: Participant,
) -> None:
    """
    Переводить учасника зі статусу PENDING у ACTIVE для scheduled-тарифу,
    коли настала start_date. Викликається з jobs/scheduled_activation.py
    (циклічна перевірка) або з admin-команди групової активації.

    Не видає перший етап автоматично — за вимогами користувач сам
    тисне "Далі" після сповіщення (advance_to_next_stage викликається
    окремо в handler'і кнопки).
    """
    if participant.status == ParticipantStatus.ACTIVE:
        return  # вже активовано, ідемпотентно виходимо

    participant.status = ParticipantStatus.ACTIVE
    participant.activated_at = datetime.now(timezone.utc)
    cache.upsert_participant(participant)

    await queue.enqueue(PendingWrite(
        sheet_name="Participants",
        row_index=participant.row_index,
        column=COL_STATUS,
        value=ParticipantStatus.ACTIVE.value,
        participant_id=participant.participant_id,
    ))


def find_due_scheduled_participants(cache: CacheStore, now: datetime | None = None) -> list[Participant]:
    """
    Повертає всіх PENDING-учасників на SCHEDULED-тарифах, чия start_date
    вже настала — для jobs/scheduled_activation.py.
    """
    now = now or datetime.now(timezone.utc)
    due: list[Participant] = []

    for participant in cache.all_participants():
        if participant.status != ParticipantStatus.PENDING:
            continue
        if not participant.token_used:
            # токен ще не прив'язаний до Telegram -> нема кому слати сповіщення/відкривати доступ
            continue

        stream = cache.get_stream(participant.stream_id)
        if stream is None:
            continue
        plan = stream.get_plan(participant.plan_id)
        if plan is None or plan.plan_type != PlanType.SCHEDULED:
            continue
        if plan.start_date is None:
            continue
        if plan.start_date <= now:
            due.append(participant)

    return due