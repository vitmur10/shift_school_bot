"""
Доставка медіаконтенту етапу (відео-урок + кружечки) учаснику.

Відео НЕ зберігається і НЕ завантажується ботом — лише копіюється
(copy_message) з адмін-каналу/чату-сховища, де матеріали залиті
вручну через звичайний Telegram-клієнт (обхід ліміту 50 МБ на
завантаження файлів через сам Bot API; деталі — у storage/models.py,
docstring ContentRef).

Bot тут — Protocol (як у services/notifications.py), щоб не тягнути
залежність від aiogram.Bot у бізнес-логіку напряму.
"""

from __future__ import annotations

import logging
from typing import Protocol

from storage.models import ContentRef, Stage

logger = logging.getLogger(__name__)


class CopiesMessages(Protocol):
    async def copy_message(self, chat_id: int, from_chat_id: int, message_id: int) -> object: ...
    async def forward_messages(self, chat_id: int, from_chat_id: int, message_ids: list[int]) -> object: ...


class ContentDeliveryError(Exception):
    """Не вдалося доставити частину контенту."""


async def deliver_media_group(bot: CopiesMessages, chat_id: int, stage: Stage) -> bool:
    """
    Надсилає кілька відео одним повідомленням (медіагрупа/album) через
    forward_messages — саме так Telegram показує кілька відео разом,
    як в оригінальному повідомленні з каналу.

    Усі відео в медіагрупі мають бути з одного chat_id —
    forward_messages вимагає однакового from_chat_id для всіх.
    """
    refs = stage.active_media_group()
    if not refs:
        return False

    # перевіряємо що всі з одного чату
    chat_ids = {ref.source_chat_id for ref in refs}
    if len(chat_ids) > 1:
        logger.warning(
            "Stage %s: медіагрупа містить відео з різних каналів (%s) — "
            "надсилаємо по черзі замість групи",
            stage.stage_id, chat_ids
        )
        # fallback: надсилаємо по черзі
        ok = True
        for ref in refs:
            ok = ok and await _copy_ref(bot, chat_id, ref, label=f"media_group стейджу {stage.stage_id}")
        return ok

    try:
        from_chat_id = refs[0].source_chat_id
        message_ids = [ref.source_message_id for ref in refs]
        await bot.forward_messages(
            chat_id=chat_id,
            from_chat_id=from_chat_id,
            message_ids=message_ids,
        )
        return True
    except Exception:
        logger.exception(
            "Не вдалося надіслати медіагрупу стейджу %s — пробую по черзі",
            stage.stage_id
        )
        # fallback: по черзі
        ok = True
        for ref in refs:
            ok = ok and await _copy_ref(bot, chat_id, ref, label=f"media_group fallback {stage.stage_id}")
        return ok


async def deliver_stage_video(bot: CopiesMessages, chat_id: int, stage: Stage) -> bool:
    """
    Надсилає відео етапу. Якщо є медіагрупа — надсилає як album,
    інакше — одне відео через copy_message.
    """
    if stage.has_media_group():
        return await deliver_media_group(bot, chat_id, stage)

    if stage.video_ref is None or not stage.video_ref.is_set():
        logger.warning("Stage %s: video_ref не заповнено — пропускаю", stage.stage_id)
        return False

    return await _copy_ref(bot, chat_id, stage.video_ref, label=f"video стейджу {stage.stage_id}")


async def deliver_stage_circles(bot: CopiesMessages, chat_id: int, stage: Stage) -> int:
    """
    Копіює всі заповнені відео-кружечки етапу по черзі.
    Повертає кількість УСПІШНО доставлених кружечків (для діагностики/логів).
    Один невдалий кружечок не блокує доставку решти.
    """
    delivered = 0
    for i, ref in enumerate(stage.active_circle_refs(), start=1):
        ok = await _copy_ref(bot, chat_id, ref, label=f"circle {i} стейджу {stage.stage_id}")
        if ok:
            delivered += 1
    return delivered


async def deliver_full_stage(bot: CopiesMessages, chat_id: int, stage: Stage) -> dict[str, int | bool]:
    """
    Зручний агрегат: доставляє відео/медіагрупу + усі кружечки одним викликом.
    """
    video_ok = await deliver_stage_video(bot, chat_id, stage)
    circles_total = len(stage.active_circle_refs())
    circles_ok = await deliver_stage_circles(bot, chat_id, stage)
    return {
        "video_delivered": video_ok,
        "media_group": stage.has_media_group(),
        "circles_delivered": circles_ok,
        "circles_total": circles_total,
    }


async def _copy_ref(bot: CopiesMessages, chat_id: int, ref: ContentRef, label: str) -> bool:
    try:
        await bot.copy_message(
            chat_id=chat_id,
            from_chat_id=ref.source_chat_id,
            message_id=ref.source_message_id,
        )
        return True
    except Exception:
        logger.exception("Не вдалося скопіювати контент (%s) учаснику chat_id=%s", label, chat_id)
        return False