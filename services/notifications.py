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
from typing import Any, Protocol

from storage.models import Participant

logger = logging.getLogger(__name__)


class SendsMessages(Protocol):
    async def send_message(self, chat_id: int, text: str, reply_markup: Any = None) -> None: ...


DEFAULT_SCHEDULED_START_TEXT = (
    "Привіт! 🎉 Доступ до курсу відкрито.\n\n"
    "Тисни «Далі», щоб почати перший етап."
)


async def notify_participant(
    bot: SendsMessages,
    participant: Participant,
    text: str,
    reply_markup: Any = None,
) -> bool:
    """
    Універсальна розсилка одного повідомлення учаснику. Повертає True/False —
    успіх чи ні (щоб виклик міг вирішити, позначати відправлене чи повторити).

    Помилки (заблокований бот, видалений акаунт тощо) логуються, але НЕ
    кидаються — одна невдала розсилка не повинна зупиняти обробку решти
    учасників у циклі.
    """
    if participant.telegram_id is None:
        logger.warning(
            "Не можу надіслати повідомлення: відсутній telegram_id (participant_id=%s)",
            participant.participant_id,
        )
        return False

    try:
        await bot.send_message(chat_id=participant.telegram_id, text=text, reply_markup=reply_markup)
        return True
    except Exception:
        logger.exception(
            "Не вдалося надіслати повідомлення (participant_id=%s, telegram_id=%s)",
            participant.participant_id, participant.telegram_id,
        )
        return False


async def notify_scheduled_access_opened(
    bot: SendsMessages,
    participant: Participant,
    text: str = DEFAULT_SCHEDULED_START_TEXT,
    reply_markup: Any = None,
) -> bool:
    """Сповіщення про відкриття доступу (обгортка над notify_participant)."""
    return await notify_participant(bot, participant, text, reply_markup=reply_markup)