"""
Генерація та валідація одноразових токенів доступу.

Токен видається при створенні Participant (зазвичай — webhook від Webflow)
і прив'язується до конкретного Telegram-акаунту при першому /start.
Після прив'язки token_used=True, повторне використання того ж токена
іншим акаунтом має блокуватись на рівні services/identification.py.
"""

from __future__ import annotations

import secrets
import string

from storage.cache_store import CacheStore
from storage.models import Participant
from storage.write_queue import PendingWrite, WriteQueue

# колонки листа Participants — винесено в константи, щоб не дублювати
# "магічні" літери по всьому коду; якщо порядок колонок у Sheets зміниться,
# правка лише тут
COL_TELEGRAM_ID = "B"
COL_TELEGRAM_USERNAME = "C"
COL_TOKEN_USED = "H"
COL_STATUS = "I"

_TOKEN_ALPHABET = string.ascii_letters + string.digits
_TOKEN_LENGTH = 24


def generate_token() -> str:
    """
    Генерує криптографічно стійкий випадковий токен.
    secrets (не random) — бо токен дає доступ до платного контенту,
    тут не можна покладатись на передбачуваний генератор.
    """
    return "".join(secrets.choice(_TOKEN_ALPHABET) for _ in range(_TOKEN_LENGTH))


class TokenError(Exception):
    """Базовий клас помилок, повʼязаних із токенами."""


class TokenNotFoundError(TokenError):
    """Токена немає в кеші — або помилка введення, або токен з іншого потоку/застарілий."""


class TokenAlreadyUsedError(TokenError):
    """Токен уже прив'язано до іншого Telegram-акаунту."""


def find_participant_by_token(cache: CacheStore, token: str) -> Participant:
    """
    Шукає учасника за токеном. Кидає виняток замість повернення None,
    щоб виклик у handler'і виглядав як явний happy/unhappy path:

        try:
            participant = find_participant_by_token(cache, token)
        except TokenNotFoundError:
            ... повідомити користувачу, що токен невірний
    """
    participant = cache.get_participant_by_token(token.strip())
    if participant is None:
        raise TokenNotFoundError(token)
    return participant


async def bind_token_to_telegram(
    cache: CacheStore,
    queue: WriteQueue,
    participant: Participant,
    telegram_id: int,
    telegram_username: str | None,
) -> Participant:
    """
    Прив'язує знайденого за токеном учасника до конкретного Telegram-акаунту.

    Викликається з bot/handlers/start.py одразу після успішного
    find_participant_by_token. Якщо токен уже використаний ІНШИМ
    telegram_id — кидаємо TokenAlreadyUsedError (захист від передачі
    токена третім особам).

    Оновлює:
      - кеш (одразу, синхронно — наступний handler побачить зміну миттєво)
      - WriteQueue (відкладено, потрапить у Sheets при найближчому flush)
    """
    if participant.token_used and participant.telegram_id != telegram_id:
        raise TokenAlreadyUsedError(participant.access_token)

    # якщо токен уже прив'язаний до ЦЬОГО ж telegram_id (повторний /start) —
    # ідемпотентно повертаємо учасника без зайвого запису
    if participant.token_used and participant.telegram_id == telegram_id:
        return participant

    participant.telegram_id = telegram_id
    participant.telegram_username = telegram_username
    participant.token_used = True

    cache.upsert_participant(participant)

    await queue.enqueue(PendingWrite(
        sheet_name="Participants",
        row_index=participant.row_index,
        column=COL_TELEGRAM_ID,
        value=telegram_id,
        participant_id=participant.participant_id,
    ))
    await queue.enqueue(PendingWrite(
        sheet_name="Participants",
        row_index=participant.row_index,
        column=COL_TELEGRAM_USERNAME,
        value=telegram_username or "",
        participant_id=participant.participant_id,
    ))
    await queue.enqueue(PendingWrite(
        sheet_name="Participants",
        row_index=participant.row_index,
        column=COL_TOKEN_USED,
        value=True,
        participant_id=participant.participant_id,
    ))

    return participant