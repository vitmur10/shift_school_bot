"""
Доставка медіаконтенту етапу учаснику.

Логіка caption:
  - caption передається з stages.py і застосовується до ПЕРШОГО елемента групи
  - решта елементів групи — без caption
  - copy_message — caption передається якщо є
  - кружечки — завжди без caption
"""

from __future__ import annotations

import logging
from typing import Protocol

from aiogram.types import InputMediaAudio, InputMediaDocument, InputMediaPhoto, InputMediaVideo

from storage.models import ContentRef, MediaType, Stage

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

    async def send_voice(
        self,
        chat_id: int,
        voice: str,
        caption: str | None = None,
    ) -> object: ...

    async def send_audio(
        self,
        chat_id: int,
        audio: str,
        caption: str | None = None,
    ) -> object: ...


# ── Маппінг MediaType → InputMedia* для send_media_group ────────────────────

_INPUT_MEDIA_CLS = {
    MediaType.VIDEO: InputMediaVideo,
    MediaType.AUDIO: InputMediaAudio,
    MediaType.DOCUMENT: InputMediaDocument,
    MediaType.PHOTO: InputMediaPhoto,
}


def _build_input_media(ref: ContentRef, caption: str | None):
    """Повертає InputMedia* об'єкт для send_media_group або None якщо тип не підтримується."""
    cls = _INPUT_MEDIA_CLS.get(ref.media_type)
    if cls is None:
        # voice не підтримується в media_group — буде надіслано окремо через _send_single_by_file_id
        return None
    return cls(media=ref.file_id, caption=caption)


async def _send_single_by_file_id(
    bot: CopiesMessages,
    chat_id: int,
    ref: ContentRef,
    caption: str | None,
    label: str,
) -> bool:
    """Надсилає один файл через відповідний send_* метод за file_id."""
    try:
        if ref.media_type == MediaType.VIDEO:
            await bot.send_video(chat_id=chat_id, video=ref.file_id, caption=caption)
        elif ref.media_type == MediaType.VOICE:
            await bot.send_voice(chat_id=chat_id, voice=ref.file_id, caption=caption)
        elif ref.media_type == MediaType.AUDIO:
            await bot.send_audio(chat_id=chat_id, audio=ref.file_id, caption=caption)
        else:
            # document, photo — fallback на copy_message (рідкісний кейс в одиночному відправленні)
            logger.warning(
                "%s: media_type=%s не підтримує одиночне file_id відправлення — fallback на copy_message",
                label, ref.media_type.value,
            )
            return await _copy_ref(bot, chat_id, ref, label=label, caption=caption)
        return True
    except Exception:
        logger.exception("%s: send_%s НЕ ВДАЛОСЬ", label, ref.media_type.value)
        return False


async def deliver_stage_media(
    bot: CopiesMessages,
    chat_id: int,
    stage: Stage,
    caption: str | None = None,
) -> bool:
    """
    Універсальна доставка основного медіаконтенту етапу.
    Підтримує video, voice, audio в обох режимах (file_id та copy_message).
    """
    active_media = stage.active_media_group()

    # ── 2+ медіафайли ───────────────────────────────────────────────────────
    if len(active_media) > 1:
        all_have_file_id = all(ref.file_id for ref in active_media)

        if all_have_file_id:
            # voice не підтримується у media_group — якщо всі файли voice,
            # надсилаємо їх послідовно через send_voice
            all_voice = all(ref.media_type == MediaType.VOICE for ref in active_media)
            if all_voice:
                ok = True
                for i, ref in enumerate(active_media):
                    ok = ok and await _send_single_by_file_id(
                        bot, chat_id, ref,
                        caption=caption if i == 0 else None,
                        label=f"voice_{i + 1} стейджу {stage.stage_id}",
                    )
                return ok

            # змішана група або суто video/audio — намагаємось send_media_group
            try:
                media_items = []
                for i, ref in enumerate(active_media):
                    item = _build_input_media(ref, caption=caption if i == 0 else None)
                    if item is None:
                        # voice в групі — надсилаємо окремо після групи
                        logger.warning(
                            "Stage %s: ref %d має media_type=%s, не підтримується в media_group — пропускаю в групі",
                            stage.stage_id, i + 1, ref.media_type.value,
                        )
                        continue
                    media_items.append(item)

                if media_items:
                    logger.info(
                        "send_media_group для стейджу %s: chat_id=%s, %d файлів",
                        stage.stage_id, chat_id, len(media_items),
                    )
                    await bot.send_media_group(chat_id=chat_id, media=media_items)
                    logger.info("send_media_group успішно для стейджу %s", stage.stage_id)
                    return True
            except Exception:
                logger.exception(
                    "send_media_group НЕ ВДАЛОСЬ для стейджу %s — fallback на copy_message",
                    stage.stage_id,
                )

        # без file_id або після невдалого send_media_group —
        # caption на першому, решта без
        ok = True
        for i, ref in enumerate(active_media):
            ok = ok and await _copy_ref(
                bot, chat_id, ref,
                label=f"media_group_{i + 1} стейджу {stage.stage_id}",
                caption=caption if i == 0 else None,
            )
        return ok

    # ── 1 медіафайл ─────────────────────────────────────────────────────────
    if len(active_media) == 1:
        ref = active_media[0]
        if ref.file_id:
            success = await _send_single_by_file_id(
                bot, chat_id, ref,
                caption=caption,
                label=f"media_1 стейджу {stage.stage_id}",
            )
            if success:
                return True
            # якщо _send_single_by_file_id повернув False і є chat_id+message_id — fallback
            if ref.source_chat_id and ref.source_message_id:
                logger.info(
                    "media_1 стейджу %s: спроба fallback через copy_message", stage.stage_id
                )
                return await _copy_ref(
                    bot, chat_id, ref,
                    label=f"media_1 fallback стейджу {stage.stage_id}",
                    caption=caption,
                )
            return False
        else:
            return await _copy_ref(
                bot, chat_id, ref,
                label=f"media_1 стейджу {stage.stage_id}",
                caption=caption,
            )

    # ── Немає media_group — резервний шлях через video_ref ──────────────────
    if stage.video_ref is None or not stage.video_ref.is_set():
        logger.warning("Stage %s: video_ref не заповнено — пропускаю", stage.stage_id)
        return False

    video_ref = stage.video_ref

    if getattr(video_ref, "file_id", None):
        success = await _send_single_by_file_id(
            bot, chat_id, video_ref,
            caption=caption,
            label=f"video_ref стейджу {stage.stage_id}",
        )
        if success:
            return True
        logger.info(
            "video_ref стейджу %s: fallback на copy_message", stage.stage_id
        )

    return await _copy_ref(
        bot, chat_id, video_ref,
        label=f"video_ref стейджу {stage.stage_id}",
        caption=caption,
    )


# Зворотна сумісність: старе ім'я функції
deliver_stage_video = deliver_stage_media


async def deliver_stage_circles(bot: CopiesMessages, chat_id: int, stage: Stage) -> int:
    """Надсилає всі кружечки (video_note) етапу без підпису."""
    delivered = 0
    for i, ref in enumerate(stage.active_circle_refs(), start=1):
        ok = await _copy_ref(
            bot, chat_id, ref,
            label=f"circle {i} стейджу {stage.stage_id}",
            caption=None,
        )
        if ok:
            delivered += 1
    return delivered


async def deliver_full_stage(
    bot: CopiesMessages,
    chat_id: int,
    stage: Stage,
    caption: str | None = None,
) -> dict[str, int | bool]:
    """
    Надсилає весь медіаконтент етапу (медіа + кружечки).
    caption передається у deliver_stage_media і чіпляється до першого елемента.
    """
    video_ok = await deliver_stage_media(bot, chat_id, stage, caption=caption)
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
        await bot.copy_message(
            chat_id=chat_id,
            from_chat_id=ref.source_chat_id,
            message_id=ref.source_message_id,
            caption=caption,
        )
        return True
    except Exception:
        logger.exception(
            "Не вдалося скопіювати контент (%s) учаснику chat_id=%s",
            label, chat_id,
        )
        return False