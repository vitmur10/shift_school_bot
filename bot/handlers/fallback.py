"""Обробка повідомлень, що не підійшли під жоден інший handler."""

from __future__ import annotations

from aiogram import Router
from aiogram.types import Message

from bot.texts import UNKNOWN_MESSAGE_FALLBACK

router = Router(name="fallback")


@router.message()
async def handle_unknown(message: Message) -> None:
    await message.answer(UNKNOWN_MESSAGE_FALLBACK)