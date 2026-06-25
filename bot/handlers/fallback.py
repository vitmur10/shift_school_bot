"""Обробка повідомлень, що не підійшли під жоден інший handler."""

from __future__ import annotations

from aiogram import Router
from aiogram.types import Message, MessageOriginChannel

from bot.texts import UNKNOWN_MESSAGE_FALLBACK

router = Router(name="fallback")


@router.message()
async def handle_unknown(message: Message) -> None:
    # ігноруємо переслані повідомлення з каналів — вони використовуються
    # адміном для команд /getid та /getfileid
    if message.forward_origin and isinstance(message.forward_origin, MessageOriginChannel):
        return

    await message.answer(UNKNOWN_MESSAGE_FALLBACK)