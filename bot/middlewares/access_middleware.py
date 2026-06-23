"""
Middleware: перевірка статусу participant перед content-handler'ами.

Застосовується ВИБІРКОВО — підключається лише до router'а
bot/handlers/stages.py (видача етапів), а НЕ до глобального Dispatcher.
Причина: на /start учасник ще може бути невідомий (немає в кеші
за tg_id) — це нормальний кейс, що обробляє bot/handlers/start.py
сам, через services/identification.py. Якщо повісити цей middleware
глобально, /start для нового користувача впаде на "учасника не
знайдено" ще до того, як встигне відпрацювати онбординг.

Знайденого participant кладе в data["participant"], щоб handler
не робив повторний cache.get_participant_by_tg_id(...) самостійно.
"""

from __future__ import annotations

from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware
from aiogram.types import TelegramObject, Message, CallbackQuery

from bot.texts import ACCESS_BLOCKED, ACCESS_NOT_YET_SCHEDULED, DATA_INCONSISTENCY_ERROR
from services.access_control import AccessDecision, check_course_access
from storage.cache_store import CacheStore


class AccessControlMiddleware(BaseMiddleware):
    """
    Якщо учасника взагалі немає в кеші за tg_id — пропускає подію далі
    без перевірки (це означає, що людина ще не пройшла онбординг;
    відповідний handler сам вирішить, що з цим робити — або це не
    повинно статись, якщо middleware підключений правильно лише на
    "захищені" хендлери, що йдуть ПІСЛЯ онбордингу).
    """

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        cache: CacheStore = data["cache"]
        tg_id = _extract_user_id(event)
        if tg_id is None:
            return await handler(event, data)

        participant = cache.get_participant_by_tg_id(tg_id)
        if participant is None:
            # немає в кеші -- хай конкретний handler сам вирішує
            # (зазвичай це означає неочікуваний edge-кейс, не помилку)
            return await handler(event, data)

        access = check_course_access(cache, participant)
        if not access.granted:
            await _reply(event, _message_for(access.decision))
            return  # обриваємо ланцюжок, handler НЕ викликається

        data["participant"] = participant
        return await handler(event, data)


def _extract_user_id(event: TelegramObject) -> int | None:
    if isinstance(event, Message) and event.from_user:
        return event.from_user.id
    if isinstance(event, CallbackQuery) and event.from_user:
        return event.from_user.id
    return None


def _message_for(decision: AccessDecision) -> str:
    return {
        AccessDecision.BLOCKED_STATUS: ACCESS_BLOCKED,
        AccessDecision.NOT_YET_SCHEDULED: ACCESS_NOT_YET_SCHEDULED,
        AccessDecision.NO_STREAM: DATA_INCONSISTENCY_ERROR,
        AccessDecision.NO_PLAN: DATA_INCONSISTENCY_ERROR,
    }.get(decision, DATA_INCONSISTENCY_ERROR)


async def _reply(event: TelegramObject, text: str) -> None:
    if isinstance(event, Message):
        await event.answer(text)
    elif isinstance(event, CallbackQuery):
        await event.answer(text, show_alert=True)