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


class ContentDeliveryError(Exception):
    """Не вдалося доставити частину контенту (відео не залите, помилка copy_message)."""


async def deliver_stage_video(bot: CopiesMessages, chat_id: int, stage: Stage) -> bool:
    """
    Копіює основний відео-урок етапу учаснику.
    Повертає False (без винятку), якщо відео ще не залите адміном —
    це очікуваний стан під час наповнення курсу, не помилка системи.
    """
    if stage.video_ref is None or not stage.video_ref.is_set():
        logger.warning("Stage %s: video_ref не заповнено — пропускаю відправку відео", stage.stage_id)
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
    Зручний агрегат: доставляє відео-урок + усі кружечки одним викликом.
    Повертає підсумок для логування/діагностики:
        {"video_delivered": bool, "circles_delivered": int, "circles_total": int}
    """
    video_ok = await deliver_stage_video(bot, chat_id, stage)
    circles_total = len(stage.active_circle_refs())
    circles_ok = await deliver_stage_circles(bot, chat_id, stage)
    return {
        "video_delivered": video_ok,
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