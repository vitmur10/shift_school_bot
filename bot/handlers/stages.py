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
from services.content_delivery import CopiesMessages, deliver_full_stage, TELEGRAM_CAPTION_LIMIT
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
    text = format_stage_message(stage)

    active_media = stage.active_media_group()
    active_media_len = len(active_media)
    has_video = (
        active_media_len > 0
        or (stage.video_ref is not None and stage.video_ref.is_set())
    )

    stream = cache.get_stream(participant.stream_id)
    is_last = participant.current_stage_order >= stream.total_active_stages()

    # визначаємо reply_markup для кнопки
    next_btn = None if is_last else next_stage_keyboard(stage.unlock_button_text)

    logger.info(
        "Доставка стейджу %s: has_video=%s, has_media_group=%s, text_len=%d",
        stage.stage_id, has_video, stage.has_media_group(), len(text)
    )

    if not has_video:
        # немає відео — текст з кнопкою
        if text:
            await callback.message.answer(text, reply_markup=next_btn)
        elif next_btn:
            await callback.message.answer(stage.unlock_button_text, reply_markup=next_btn)
    else:
        multi_no_file_id = (
            active_media_len > 1 and not all(ref.file_id for ref in active_media)
        )

        if multi_no_file_id or len(text) > TELEGRAM_CAPTION_LIMIT:
            # текст окремо перед відео, кнопка після відео
            if text:
                await callback.message.answer(text)
            await deliver_full_stage(bot, chat_id, stage, caption=None)
            if is_last:
                await callback.message.answer(COURSE_COMPLETED)
            elif next_btn:
                await callback.message.answer(stage.unlock_button_text, reply_markup=next_btn)
            return
        else:
            # текст як caption до відео — але caption не підтримує reply_markup
            # тому після відео надсилаємо кнопку окремо
            await deliver_full_stage(bot, chat_id, stage, caption=text)

    if is_last:
        await callback.message.answer(COURSE_COMPLETED)
    elif next_btn and has_video:
        # відео вже надіслано, кнопка окремим повідомленням
        await callback.message.answer(stage.unlock_button_text, reply_markup=next_btn)