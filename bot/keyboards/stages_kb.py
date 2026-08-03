"""
Inline-кнопки для видачі етапів курсу ("Далі" тощо).
"""

from __future__ import annotations

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

NEXT_STAGE_CALLBACK = "next_stage"


def next_stage_keyboard(button_text: str = "Далі") -> InlineKeyboardMarkup:
    """Одна кнопка переходу до наступного етапу."""
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text=button_text, callback_data=NEXT_STAGE_CALLBACK, style="primary")]]
    )


GROUP_BUTTON_TEXT = "👥 Група потоку"
CURATOR_BUTTON_TEXT = "✍️ Написати ментору"
CHAT_BUTTON_TEXT = "💬 Чат потоку"


def reminder_keyboard(
    chat_url: str | None = None,
    curator_url: str | None = None,
    group_url: str | None = None,
) -> InlineKeyboardMarkup | None:
    """
    Клавіатура для пре-стартових нагадувань (scheduled-тариф): кнопки групи,
    чату та куратора, якщо відповідні посилання задані в таблиці. Якщо всі
    порожні — повертає None (повідомлення піде без клавіатури).
    """
    rows: list[list[InlineKeyboardButton]] = []
    if group_url:
        rows.append([InlineKeyboardButton(text=GROUP_BUTTON_TEXT, url=group_url, style="primary")])
    if chat_url:
        rows.append([InlineKeyboardButton(text=CHAT_BUTTON_TEXT, url=chat_url, style="primary")])
    if curator_url:
        rows.append([InlineKeyboardButton(text=CURATOR_BUTTON_TEXT, url=curator_url, style="primary")])
    return InlineKeyboardMarkup(inline_keyboard=rows) if rows else None


WELCOME_CHAT_BUTTON_TEXT = "💬 Перейти в чат"
WELCOME_MENTOR_BUTTON_TEXT = "👤 Написати ментору"


def start_notification_keyboard(
    chat_url: str | None = None,
    mentor_url: str | None = None,
) -> InlineKeyboardMarkup:
    """
    Клавіатура для привітального сповіщення про старт курсу.

    Завжди містить кнопку «Далі» (перехід до першого етапу), а також
    (якщо задано в таблиці) кнопки чату та ментора.
    """
    rows: list[list[InlineKeyboardButton]] = [
        [InlineKeyboardButton(text="Далі", callback_data=NEXT_STAGE_CALLBACK, style="primary")],
    ]
    if chat_url:
        rows.append([InlineKeyboardButton(text=WELCOME_CHAT_BUTTON_TEXT, url=chat_url, style="primary")])
    if mentor_url:
        rows.append([InlineKeyboardButton(text=WELCOME_MENTOR_BUTTON_TEXT, url=mentor_url, style="primary")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


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
        rows.append([InlineKeyboardButton(text=next_button_text, callback_data=NEXT_STAGE_CALLBACK, style="primary")])
    if group_url:
        rows.append([InlineKeyboardButton(text=GROUP_BUTTON_TEXT, url=group_url, style="primary")])
    if curator_url:
        rows.append([InlineKeyboardButton(text=CURATOR_BUTTON_TEXT, url=curator_url, style="primary")])
    return InlineKeyboardMarkup(inline_keyboard=rows)
