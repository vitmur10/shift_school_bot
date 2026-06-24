"""
Доставка медіаконтенту етапу учаснику.
"""

from __future__ import annotations

import logging
from typing import Protocol

from aiogram.types import InputMediaVideo

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

    async def send_media_group(
        self,
        chat_id: int,
        media: list,
    ) -> object: ...


async def deliver_stage_video(
    bot: CopiesMessages,
    chat_id: int,
    stage: Stage,
    caption: str | None = None,
) -> bool:
    """
    Надсилає відео етапу.
    - Якщо є медіагрупа з file_id → send_media_group (справжній album)
    - Якщо є медіагрупа без file_id → copy_message по черзі
    - Якщо одне відео → copy_message
    """
    if caption and len(caption) > TELEGRAM_CAPTION_LIMIT:
        caption = None

    if stage.has_media_group():
        return await _deliver_media_group(bot, chat_id, stage, caption)

    if stage.video_ref is None or not stage.video_ref.is_set():
        logger.warning("Stage %s: video_ref не заповнено — пропускаю", stage.stage_id)
        return False

    return await _copy_ref(
        bot, chat_id, stage.video_ref,
        label=f"video стейджу {stage.stage_id}",
        caption=caption,
    )


async def deliver_stage_circles(bot: CopiesMessages, chat_id: int, stage: Stage) -> int:
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
    video_ok = await deliver_stage_video(bot, chat_id, stage, caption=caption)
    circles_total = len(stage.active_circle_refs())
    circles_ok = await deliver_stage_circles(bot, chat_id, stage)
    return {
        "video_delivered": video_ok,
        "media_group": stage.has_media_group(),
        "circles_delivered": circles_ok,
        "circles_total": circles_total,
    }


async def _deliver_media_group(
    bot: CopiesMessages,
    chat_id: int,
    stage: Stage,
    caption: str | None = None,
) -> bool:
    refs = stage.active_media_group()

    # якщо всі мають file_id — використовуємо send_media_group (справжній album)
    all_have_file_id = all(ref.file_id for ref in refs)
    if all_have_file_id:
        try:
            media = []
            for i, ref in enumerate(refs):
                # caption лише до останнього елементу
                item_caption = caption if i == len(refs) - 1 else None
                media.append(InputMediaVideo(
                    media=ref.file_id,
                    caption=item_caption,
                ))
            await bot.send_media_group(chat_id=chat_id, media=media)
            return True
        except Exception:
            logger.exception(
                "send_media_group не вдалось для стейджу %s — пробую copy_message",
                stage.stage_id
            )

    # fallback: copy_message по черзі
    ok = True
    for i, ref in enumerate(refs):
        ref_caption = caption if i == len(refs) - 1 else None
        ok = ok and await _copy_ref(
            bot, chat_id, ref,
            label=f"media_group_{i+1} стейджу {stage.stage_id}",
            caption=ref_caption,
        )
    return ok


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
        logger.exception(
            "Не вдалося скопіювати контент (%s) учаснику chat_id=%s",
            label, chat_id
        )
        return False