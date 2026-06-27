"""
Кнопка "Далі": просування на наступний етап + доставка контенту.

Схема доставки:

  Варіант B — всі медіа через file_id І текст ≤ 1024:
      [send_media_group, caption=text на першому файлі]
      → [«Готовий йти далі? 👇» + кнопка]

  Варіант A — немає file_id АБО текст > 1024:
      [текст окремо] → [медіа без caption] → [кнопка окремо]

  Без медіа:
      [текст + кнопка]
"""

from __future__ import annotations

import logging

from aiogram import Router
from aiogram.types import CallbackQuery

from bot.keyboards.stages_kb import NEXT_STAGE_CALLBACK, next_stage_keyboard
from bot.texts import (
    ACCESS_BLOCKED,
    ACCESS_NOT_YET_SCHEDULED,
    COURSE_COMPLETED,
    DATA_INCONSISTENCY_ERROR,
    format_stage_message,
)
from services.access_control import AccessDecision, advance_to_next_stage
from services.content_delivery import CopiesMessages, deliver_full_stage, TELEGRAM_CAPTION_LIMIT
from storage.cache_store import CacheStore
from storage.write_queue import WriteQueue

logger = logging.getLogger(__name__)

router = Router(name="stages")

_NEXT_PROMPT = "Готовий йти далі? 👇"


def _all_media_have_file_id(stage) -> bool:
    """
    True лише якщо ВСІ медіафайли стейджу мають file_id.
    Враховує обидва джерела: active_media_group() та video_ref.
    """
    active_media = stage.active_media_group()

    if active_media:
        return all(bool(ref.file_id) for ref in active_media)

    if stage.video_ref is not None and stage.video_ref.is_set():
        return bool(getattr(stage.video_ref, "file_id", None))

    return False


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

    # ── Підготовка даних ────────────────────────────────────────────────────
    chat_id = callback.from_user.id
    text = format_stage_message(stage)

    active_media = stage.active_media_group()
    has_video = (
        len(active_media) > 0
        or (stage.video_ref is not None and stage.video_ref.is_set())
    )

    stream = cache.get_stream(participant.stream_id)
    is_last = participant.current_stage_order >= stream.total_active_stages()
    next_btn = None if is_last else next_stage_keyboard(stage.unlock_button_text)

    logger.info(
        "Доставка стейджу %s: has_video=%s, has_media_group=%s, text_len=%d",
        stage.stage_id, has_video, stage.has_media_group(), len(text),
    )

    # ── Без медіа ───────────────────────────────────────────────────────────
    if not has_video:
        if text:
            await callback.message.answer(text, reply_markup=next_btn)
        elif next_btn:
            await callback.message.answer(stage.unlock_button_text, reply_markup=next_btn)
        if is_last:
            await callback.message.answer(COURSE_COMPLETED)
        return

    # ── З медіа: визначаємо варіант ─────────────────────────────────────────
    use_caption = (
        _all_media_have_file_id(stage)
        and len(text) <= TELEGRAM_CAPTION_LIMIT
    )

    if use_caption:
        # Варіант B: група з caption на першому → окреме повідомлення з кнопкою
        await deliver_full_stage(bot, chat_id, stage, caption=text or None)
        if is_last:
            await callback.message.answer(COURSE_COMPLETED)
        elif next_btn:
            await callback.message.answer(_NEXT_PROMPT, reply_markup=next_btn)
    else:
        # Варіант A: текст окремо → медіа без caption → кнопка окремо
        if text:
            await callback.message.answer(text)
        await deliver_full_stage(bot, chat_id, stage, caption=None)
        if is_last:
            await callback.message.answer(COURSE_COMPLETED)
        elif next_btn:
            await callback.message.answer(_NEXT_PROMPT, reply_markup=next_btn)