"""Обробка повідомлень, що не підійшли під жоден інший handler."""

from __future__ import annotations

from aiogram import Router
from aiogram.types import Message, MessageOriginChannel

from bot.texts import UNKNOWN_MESSAGE_FALLBACK

router = Router(name="fallback")


@router.message()
async def handle_unknown(message: Message) -> None:
    # ігноруємо переслані повідомлення з каналів (для /getid та /getfileid)
    if message.forward_origin and isinstance(message.forward_origin, MessageOriginChannel):
        return

    # ігноруємо контакти — вони обробляються в handle_phone_shared
    # якщо людина надіслала контакт поза FSM — просто ігноруємо
    if message.contact:
        return

    # ігноруємо відео/документи — можуть приходити при тестуванні /getfileid
    if message.video or message.document or message.video_note or message.photo:
        return

    await message.answer(UNKNOWN_MESSAGE_FALLBACK)