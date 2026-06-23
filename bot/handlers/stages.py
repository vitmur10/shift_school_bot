"""
Кнопка "Далі": просування на наступний етап + доставка контенту.
"""

from __future__ import annotations

import logging

from aiogram import Router
from aiogram.types import CallbackQuery

from bot.keyboards.stages_kb import NEXT_STAGE_CALLBACK, next_stage_keyboard
from bot.texts import ACCESS_BLOCKED, ACCESS_NOT_YET_SCHEDULED, COURSE_COMPLETED, DATA_INCONSISTENCY_ERROR, format_stage_message
from services.access_control import AccessDecision, advance_to_next_stage
from services.content_delivery import CopiesMessages, deliver_full_stage
from storage.cache_store import CacheStore
from storage.write_queue import WriteQueue

logger = logging.getLogger(__name__)

router = Router(name="stages")


@router.callback_query(lambda c: c.data == NEXT_STAGE_CALLBACK)
async def handle_next_stage(
    callback: CallbackQuery,
    cache: CacheStore,
    queue: WriteQueue,
    bot: CopiesMessages,
    participant=None,
) -> None:
    await callback.answer()

    if participant is None:
        # AccessControlMiddleware не знайшов учасника за tg_id -- не повинно
        # траплятись у нормальному потоці (кнопка з'являється лише після
        # онбордингу), але про всяк випадок не валимо handler мовчки
        logger.error("handle_next_stage: participant=None для tg_id=%s", callback.from_user.id)
        await callback.message.answer(DATA_INCONSISTENCY_ERROR)
        return

    access, stage = await advance_to_next_stage(cache, queue, participant)

    if access.decision == AccessDecision.NO_MORE_STAGES:
        await callback.message.answer(COURSE_COMPLETED)
        return

    if access.decision == AccessDecision.BLOCKED_STATUS:
        await callback.message.answer(ACCESS_BLOCKED)
        return

    if access.decision == AccessDecision.NOT_YET_SCHEDULED:
        await callback.message.answer(ACCESS_NOT_YET_SCHEDULED)
        return

    if not access.granted or stage is None:
        await callback.message.answer(DATA_INCONSISTENCY_ERROR)
        return

    chat_id = callback.from_user.id
    await callback.message.answer(format_stage_message(stage))
    await deliver_full_stage(bot, chat_id, stage)

    stream = cache.get_stream(participant.stream_id)
    is_last = participant.current_stage_order >= stream.total_active_stages()
    if is_last:
        await callback.message.answer(COURSE_COMPLETED)
    else:
        await callback.message.answer("👇", reply_markup=next_stage_keyboard(stage.unlock_button_text))