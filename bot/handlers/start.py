"""
/start та FSM-онбординг: ідентифікація учасника, прив'язка токена.

Каскад (детально в services/identification.py):
  1. tg_id вже відомий (повторний /start) -> одразу показуємо стан доступу.
  2. username збігається з кимось у Sheets -> прив'язка, показуємо стан.
  3. Інакше -> FSM: запит телефону -> якщо не впізнали -> запит токена.

Прив'язка (token_service.bind_token_to_telegram) виконується лише
після того, як знайдено КОНКРЕТНОГО participant — незалежно, яким
саме методом його знайшли (username/phone/token), бо у всіх випадках
потрібно так само записати telegram_id/username і token_used=True.
"""

from __future__ import annotations

import logging

from aiogram import F, Router
from aiogram.filters import CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import Message

from bot.keyboards.onboarding_kb import remove_keyboard, request_phone_keyboard
from bot.keyboards.stages_kb import next_stage_keyboard
from bot.states.onboarding import OnboardingStates
from bot.texts import (
    ACCESS_BLOCKED,
    ACCESS_GRANTED_FIRST_TIME,
    ACCESS_NOT_YET_SCHEDULED,
    ASK_FOR_PHONE,
    ASK_FOR_TOKEN,
    COURSE_COMPLETED,
    DATA_INCONSISTENCY_ERROR,
    IDENTIFICATION_FAILED_FALLBACK,
    TOKEN_ALREADY_USED_BY_OTHER,
    TOKEN_NOT_FOUND,
    WELCOME_BACK,
    WELCOME_NEW_USER,
    format_stage_message,
)
from services.access_control import AccessDecision, check_course_access, get_current_stage
from services.identification import identify_by_phone, identify_participant
from services.token_service import (
    TokenAlreadyUsedError,
    TokenNotFoundError,
    bind_token_to_telegram,
    find_participant_by_token,
)
from storage.cache_store import CacheStore
from storage.models import Participant
from storage.write_queue import WriteQueue

logger = logging.getLogger(__name__)

router = Router(name="start")


@router.message(CommandStart())
async def handle_start(message: Message, cache: CacheStore, queue: WriteQueue, state: FSMContext) -> None:
    tg_id = message.from_user.id
    username = message.from_user.username  # None, якщо в людини взагалі немає username

    result = identify_participant(cache, telegram_id=tg_id, telegram_username=username)

    if result.found:
        # знайдено за tg_id (повторний /start) або за username -- в обох
        # випадках прив'язка ідемпотентна для tg_id, що вже збігається,
        # і виконує реальну прив'язку, якщо знайдено саме за username
        participant = await bind_token_to_telegram(
            cache, queue, result.participant, telegram_id=tg_id, telegram_username=username,
        )
        await state.clear()
        await _show_access_state(message, cache, participant, is_first_time=False)
        return

    # нічого не знайдено -- починаємо FSM-онбординг
    await message.answer(WELCOME_NEW_USER)
    await message.answer(ASK_FOR_PHONE, reply_markup=request_phone_keyboard())
    await state.set_state(OnboardingStates.waiting_for_phone)


@router.message(OnboardingStates.waiting_for_phone, F.contact)
async def handle_phone_shared(message: Message, cache: CacheStore, queue: WriteQueue, state: FSMContext) -> None:
    phone_number = message.contact.phone_number
    tg_id = message.from_user.id
    username = message.from_user.username

    result = identify_by_phone(cache, phone_number)

    if result.found:
        participant = await bind_token_to_telegram(
            cache, queue, result.participant, telegram_id=tg_id, telegram_username=username,
        )
        await state.clear()
        await message.answer(WELCOME_BACK, reply_markup=remove_keyboard())
        await _show_access_state(message, cache, participant, is_first_time=True)
        return

    # телефон теж не допоміг -- останній варіант: запит токена напряму
    await message.answer(IDENTIFICATION_FAILED_FALLBACK, reply_markup=remove_keyboard())
    await state.set_state(OnboardingStates.waiting_for_token)


@router.message(OnboardingStates.waiting_for_phone)
async def handle_phone_step_wrong_input(message: Message) -> None:
    """Людина в стані очікування телефону написала текст замість натискання кнопки."""
    await message.answer(ASK_FOR_PHONE, reply_markup=request_phone_keyboard())


@router.message(OnboardingStates.waiting_for_token, F.text)
async def handle_token_input(message: Message, cache: CacheStore, queue: WriteQueue, state: FSMContext) -> None:
    token = message.text.strip()
    tg_id = message.from_user.id
    username = message.from_user.username

    try:
        participant = find_participant_by_token(cache, token)
    except TokenNotFoundError:
        await message.answer(TOKEN_NOT_FOUND)
        return  # лишаємось у waiting_for_token, даємо спробувати ще раз

    try:
        participant = await bind_token_to_telegram(
            cache, queue, participant, telegram_id=tg_id, telegram_username=username,
        )
    except TokenAlreadyUsedError:
        await message.answer(TOKEN_ALREADY_USED_BY_OTHER)
        return

    await state.clear()
    await _show_access_state(message, cache, participant, is_first_time=True)


async def _show_access_state(
    message: Message,
    cache: CacheStore,
    participant: Participant,
    is_first_time: bool,
) -> None:
    """
    Спільна логіка показу поточного стану доступу — викликається з
    усіх трьох гілок ідентифікації (tg_id/username/phone/token), щоб
    не дублювати перевірку access_control у кожному handler'і окремо.
    """
    access = check_course_access(cache, participant)

    if access.decision == AccessDecision.BLOCKED_STATUS:
        await message.answer(ACCESS_BLOCKED)
        return

    if access.decision == AccessDecision.NOT_YET_SCHEDULED:
        await message.answer(ACCESS_NOT_YET_SCHEDULED)
        return

    if access.decision in (AccessDecision.NO_STREAM, AccessDecision.NO_PLAN):
        logger.error(
            "Розсинхронізація даних: participant_id=%s, decision=%s",
            participant.participant_id, access.decision,
        )
        await message.answer(DATA_INCONSISTENCY_ERROR)
        return

    # GRANTED
    current_stage = get_current_stage(cache, participant)
    if current_stage is None:
        # доступ є, але учасник ще не бачив жодного етапу -- перший /start
        # після відкриття доступу; показуємо привітання + кнопку "Далі"
        await message.answer(ACCESS_GRANTED_FIRST_TIME, reply_markup=next_stage_keyboard())
        return

    # учасник уже десь усередині курсу (повторний /start) -- показуємо,
    # на якому етапі він зараз зупинився, без повторної видачі контенту
    # (повторна видача контенту, якщо потрібна, — окрема дія в stages.py)
    stream = cache.get_stream(participant.stream_id)
    is_last = participant.current_stage_order >= stream.total_active_stages()
    text = format_stage_message(current_stage)
    if is_last:
        await message.answer(text)
        await message.answer(COURSE_COMPLETED)
    else:
        await message.answer(text, reply_markup=next_stage_keyboard())