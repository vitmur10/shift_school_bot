"""
Доставка медіаконтенту етапу (відео-урок + кружечки) учаснику.

Відео НЕ завантажується ботом — лише копіюється (copy_message) з
адмін-каналу, де матеріали залиті вручну через звичайний Telegram-клієнт.
"""

from __future__ import annotations

import logging
from typing import Protocol

from storage.models import ContentRef, Stage

logger = logging.getLogger(__name__)

TELEGRAM_CAPTION_LIMIT = 1024


class CopiesMessages(Protocol):
    async def copy_message(
        self,
        chat_id: int,
        from_chat_id: int,
        message_id: int,
        caption: str | None = None,
    ) -> object: ...


class ContentDeliveryError(Exception):
    """Не вдалося доставити частину контенту."""


async def deliver_stage_video(
    bot: CopiesMessages,
    chat_id: int,
    stage: Stage,
    caption: str | None = None,
) -> bool:
    """
    Надсилає відео етапу. caption (якщо є) передається першому відео.
    Якщо caption довший за 1024 символи — ігнорується (має надсилатись окремо).
    """
    if caption and len(caption) > TELEGRAM_CAPTION_LIMIT:
        caption = None

    if stage.has_media_group():
        refs = stage.active_media_group()
        ok = True
        for i, ref in enumerate(refs):
            ref_caption = caption if i == 0 else None
            ok = ok and await _copy_ref(
                bot, chat_id, ref,
                label=f"media_group_{i+1} стейджу {stage.stage_id}",
                caption=ref_caption,
            )
        return ok

    if stage.video_ref is None or not stage.video_ref.is_set():
        logger.warning("Stage %s: video_ref не заповнено — пропускаю", stage.stage_id)
        return False

    return await _copy_ref(
        bot, chat_id, stage.video_ref,
        label=f"video стейджу {stage.stage_id}",
        caption=caption,
    )


async def deliver_stage_circles(bot: CopiesMessages, chat_id: int, stage: Stage) -> int:
    """Надсилає всі кружечки по черзі. Повертає кількість успішно надісланих."""
    delivered = 0
    for i, ref in enumerate(stage.active_circle_refs(), start=1):
        ok = await _copy_ref(bot, chat_id, ref, label=f"circle {i} стейджу {stage.stage_id}")
        if ok:
            delivered += 1
    return delivered


async def deliver_full_stage(
    bot: CopiesMessages,
    chat_id: int,
    stage: Stage,
    caption: str | None = None,
) -> dict[str, int | bool]:
    """Доставляє відео/медіагрупу + кружечки. caption іде до першого відео."""
    video_ok = await deliver_stage_video(bot, chat_id, stage, caption=caption)
    circles_total = len(stage.active_circle_refs())
    circles_ok = await deliver_stage_circles(bot, chat_id, stage)
    return {
        "video_delivered": video_ok,
        "media_group": stage.has_media_group(),
        "circles_delivered": circles_ok,
        "circles_total": circles_total,
    }


async def _copy_ref(
    bot: CopiesMessages,
    chat_id: int,
    ref: ContentRef,
    label: str,
    caption: str | None = None,
) -> bool:
    try:
        kwargs = dict(
            chat_id=chat_id,
            from_chat_id=ref.source_chat_id,
            message_id=ref.source_message_id,
        )
        if caption:
            kwargs["caption"] = caption
        await bot.copy_message(**kwargs)
        return True
    except Exception:
        logger.exception("Не вдалося скопіювати контент (%s) учаснику chat_id=%s", label, chat_id)
        return False