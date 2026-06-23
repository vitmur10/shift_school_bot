"""
Розсилка сповіщень користувачам через Telegram Bot API.

Зараз єдиний сценарій — сповіщення про відкриття доступу для
SCHEDULED-тарифу (Тариф 2), коли настала start_date і
activate_scheduled_plan уже перевів учасника в ACTIVE.

Bot тут — Protocol, а не aiogram.Bot напряму, щоб services/ не залежав
від конкретної бібліотеки бота і легко тестувався фейковим об'єктом.
"""

from __future__ import annotations

import logging
from typing import Protocol

from storage.models import Participant

logger = logging.getLogger(__name__)


class SendsMessages(Protocol):
    async def send_message(self, chat_id: int, text: str) -> None: ...


DEFAULT_SCHEDULED_START_TEXT = (
    "Привіт! 🎉 Доступ до курсу відкрито.\n\n"
    "Тисни «Далі», щоб почати перший етап."
)


async def notify_scheduled_access_opened(
    bot: SendsMessages,
    participant: Participant,
    text: str = DEFAULT_SCHEDULED_START_TEXT,
) -> bool:
    """
    Надсилає повідомлення про відкриття доступу. Повертає True/False —
    успіх чи ні, щоб виклик (jobs/scheduled_activation.py) міг вирішити,
    позначати participant.notification_sent чи спробувати пізніше.

    Помилки (заблокований бот користувачем, видалений акаунт тощо)
    логуються, але не кидаються — одна невдала розсилка не повинна
    зупиняти обробку решти учасників у циклі.
    """
    if participant.telegram_id is None:
        logger.warning(
            "Не можу надіслати сповіщення: відсутній telegram_id (participant_id=%s)",
            participant.participant_id,
        )
        return False

    try:
        await bot.send_message(chat_id=participant.telegram_id, text=text)
        return True
    except Exception:
        logger.exception(
            "Не вдалося надіслати сповіщення про старт (participant_id=%s, telegram_id=%s)",
            participant.participant_id, participant.telegram_id,
        )
        return False