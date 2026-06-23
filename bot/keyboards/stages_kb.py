"""
Inline-кнопки для видачі етапів курсу ("Далі" тощо).
"""

from __future__ import annotations

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

NEXT_STAGE_CALLBACK = "next_stage"


def next_stage_keyboard(button_text: str = "Далі") -> InlineKeyboardMarkup:
    """Одна кнопка переходу до наступного етапу."""
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text=button_text, callback_data=NEXT_STAGE_CALLBACK)]]
    )