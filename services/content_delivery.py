"""
Доставка медіаконтенту етапу учаснику.

Логіка доставки:
- 1 медіа з file_id → send_video з caption
- 2+ медіа з file_id → send_media_group (album) з caption на першому
- 1 медіа без file_id → copy_message з caption
- 2+ медіа без file_id → copy_message по черзі БЕЗ caption (йдуть після тексту)
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

    async def send_video(
        self,
        chat_id: int,
        video: str,
        caption: str | None = None,
    ) -> object: ...


async def deliver_stage_video(
    bot: CopiesMessages,
    chat_id: int,
    stage: Stage,
    caption: str | None = None,
) -> bool:
    if caption and len(caption) > TELEGRAM_CAPTION_LIMIT:
        caption = None

    active_media = stage.active_media_group()

    # 2+ медіа
    if len(active_media) > 1:
        all_have_file_id = all(ref.file_id for ref in active_media)

        if all_have_file_id:
            # send_media_group з caption на першому
            try:
                logger.info(
                    "send_media_group для стейджу %s: chat_id=%s, %d відео",
                    stage.stage_id, chat_id, len(active_media)
                )
                media = []
                for i, ref in enumerate(active_media):
                    media.append(InputMediaVideo(
                        media=ref.file_id,
                        caption=caption if i == 0 else None,
                    ))
                await bot.send_media_group(chat_id=chat_id, media=media)
                logger.info("send_media_group успішно для стейджу %s", stage.stage_id)
                return True
            except Exception as e:
                logger.exception("send_media_group НЕ ВДАЛОСЬ для стейджу %s: %s", stage.stage_id, e)
                # fallback нижче

        # без file_id АБО після невдалого send_media_group:
        # copy_message по черзі БЕЗ caption (текст вже надіслано окремо)
        ok = True
        for i, ref in enumerate(active_media):
            ok = ok and await _copy_ref(
                bot, chat_id, ref,
                label=f"media_group_{i+1} стейджу {stage.stage_id}",
                caption=None,
            )
        return ok

    # 1 медіа
    if len(active_media) == 1:
        ref = active_media[0]
        if ref.file_id:
            try:
                await bot.send_video(chat_id=chat_id, video=ref.file_id, caption=caption)
                return True
            except Exception:
                logger.exception("send_video НЕ ВДАЛОСЬ для стейджу %s", stage.stage_id)
                return False
        else:
            # copy_message з caption
            return await _copy_ref(
                bot, chat_id, ref,
                label=f"media_1 стейджу {stage.stage_id}",
                caption=caption,
            )

    # немає media_group — резерв через video_ref
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
        "media_group": len(stage.active_media_group()) > 1,
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
        logger.exception(
            "Не вдалося скопіювати контент (%s) учаснику chat_id=%s",
            label, chat_id
        )
        return False