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


GROUP_BUTTON_TEXT = "👥 Група потоку"
CURATOR_BUTTON_TEXT = "✍️ Написати куратору"


def access_opened_keyboard(
    next_button_text: str = "Далі",
    group_url: str | None = None,
    curator_url: str | None = None,
    include_next: bool = True,
) -> InlineKeyboardMarkup:
    """
    Клавіатура для моменту відкриття доступу.

    Завжди містить «Далі» (перехід до першого етапу), і додатково — URL-кнопки
    «Група потоку» та «Написати куратору», якщо відповідні посилання задані
    у таблиці (Streams.telegram_group_url, Plans.curator_url). Якщо обидва
    порожні — поводиться точно як next_stage_keyboard (нічого не ламає).
    """
    rows: list[list[InlineKeyboardButton]] = []
    if include_next:
        rows.append([InlineKeyboardButton(text=next_button_text, callback_data=NEXT_STAGE_CALLBACK)])
    if group_url:
        rows.append([InlineKeyboardButton(text=GROUP_BUTTON_TEXT, url=group_url)])
    if curator_url:
        rows.append([InlineKeyboardButton(text=CURATOR_BUTTON_TEXT, url=curator_url)])
    return InlineKeyboardMarkup(inline_keyboard=rows)