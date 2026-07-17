"""
Допоміжні функції роботи з датами/часом.

Єдина точка істини для часового поясу проєкту — Київ (Europe/Kyiv).
Усі «тепер», мітки часу в таблиці та парсинг дат зі Sheets мають іти
через ці хелпери, щоб час усюди був київський (з коректним переходом
літо/зима).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

try:
    from zoneinfo import ZoneInfo

    KYIV_TZ = ZoneInfo("Europe/Kyiv")
except Exception:  # немає tzdata — запасний фіксований зсув (без DST)
    KYIV_TZ = timezone(timedelta(hours=2))

SHEETS_DT_FORMAT = "%Y-%m-%d %H:%M:%S"


def now_kyiv() -> datetime:
    """Поточний час у Києві (aware datetime)."""
    return datetime.now(KYIV_TZ)


def now_kyiv_str(fmt: str = SHEETS_DT_FORMAT) -> str:
    """Поточний київський час рядком (для запису в таблицю/логи)."""
    return now_kyiv().strftime(fmt)


def to_kyiv(dt: datetime | None) -> datetime | None:
    """
    Приводить datetime до київського поясу.
    Наївний (без tz) вважаємо вже київським; aware — конвертуємо.
    """
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=KYIV_TZ)
    return dt.astimezone(KYIV_TZ)
