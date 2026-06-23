"""
Клавіатури для онбордингу (запит телефону тощо).
"""

from __future__ import annotations

from aiogram.types import KeyboardButton, ReplyKeyboardMarkup, ReplyKeyboardRemove

from bot.texts import ASK_FOR_PHONE_BUTTON


def request_phone_keyboard() -> ReplyKeyboardMarkup:
    """
    Reply-клавіатура з кнопкою "Поділитись номером" (request_contact=True).
    Telegram сам підставляє номер з акаунту користувача при натисканні —
    людині не потрібно вводити цифри вручну.
    """
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text=ASK_FOR_PHONE_BUTTON, request_contact=True)]],
        resize_keyboard=True,
        one_time_keyboard=True,
    )


def remove_keyboard() -> ReplyKeyboardRemove:
    """Прибирає reply-клавіатуру після того, як вона більше не потрібна."""
    return ReplyKeyboardRemove()