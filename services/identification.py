"""
Ідентифікація учасника при /start та інших точках входу.

Каскад пошуку:
  1. За telegram_id — якщо учасник уже колись прив'язав акаунт (повторний /start).
  2. За telegram_username — якщо Webflow передав нікнейм і він збігається
     з тим, що Telegram повідомляє про поточного користувача.
  3. За номером телефону — запасний варіант, коли username недоступний
     (людина його не вказала в Webflow або змінила в Telegram).
  4. Якщо нічого не знайдено — учасника в системі немає взагалі
     (NotFound), це відрізняється від "знайдено, але токен не підходить".

Сам токен NOT validated тут — це робота token_service. identification
відповідає лише за "хто ця людина в наших даних", а не за "чи може вона
користуватись токеном X".
"""

from __future__ import annotations

from enum import Enum

from storage.cache_store import CacheStore
from storage.models import Participant


class IdentificationMethod(str, Enum):
    TELEGRAM_ID = "telegram_id"          # повторний /start, акаунт вже прив'язаний
    USERNAME = "username"                 # знайдено за збігом @username з Webflow-даними
    PHONE = "phone"                       # знайдено за номером телефону (запасний варіант)
    NOT_FOUND = "not_found"               # учасника немає в жодному з потоків


class IdentificationResult:
    """Результат спроби ідентифікації — учасник (якщо знайдено) + яким методом."""

    __slots__ = ("participant", "method")

    def __init__(self, participant: Participant | None, method: IdentificationMethod) -> None:
        self.participant = participant
        self.method = method

    @property
    def found(self) -> bool:
        return self.participant is not None

    @property
    def needs_phone_fallback(self) -> bool:
        """
        True, якщо tg_id і username не дали результату — handler має
        запросити в користувача номер телефону як останній варіант
        перед тим, як попросити токен напряму.
        """
        return self.method == IdentificationMethod.NOT_FOUND


def identify_by_telegram_id(cache: CacheStore, telegram_id: int) -> IdentificationResult:
    participant = cache.get_participant_by_tg_id(telegram_id)
    if participant:
        return IdentificationResult(participant, IdentificationMethod.TELEGRAM_ID)
    return IdentificationResult(None, IdentificationMethod.NOT_FOUND)


def identify_by_username(cache: CacheStore, username: str | None) -> IdentificationResult:
    if not username:
        return IdentificationResult(None, IdentificationMethod.NOT_FOUND)
    participant = cache.get_participant_by_username(username)
    if participant:
        return IdentificationResult(participant, IdentificationMethod.USERNAME)
    return IdentificationResult(None, IdentificationMethod.NOT_FOUND)


def identify_by_phone(cache: CacheStore, phone_number: str) -> IdentificationResult:
    """
    Лінійний пошук по всіх учасниках — прийнятно, бо це не "гарячий" шлях
    (виконується рідко, лише коли tg_id і username не спрацювали), на
    відміну від get_participant_by_tg_id/by_username, які викликаються
    на кожен апдейт і тому мають бути O(1).
    """
    normalized = _normalize_phone(phone_number)
    for participant in cache.all_participants():
        if participant.phone_number and _normalize_phone(participant.phone_number) == normalized:
            return IdentificationResult(participant, IdentificationMethod.PHONE)
    return IdentificationResult(None, IdentificationMethod.NOT_FOUND)


def identify_participant(
    cache: CacheStore,
    telegram_id: int,
    telegram_username: str | None,
    phone_number: str | None = None,
) -> IdentificationResult:
    """
    Повний каскад пошуку для виклику з handler /start.

    phone_number передається опційно: на першому проході handler викликає
    цю функцію без телефону; якщо результат needs_phone_fallback — запитує
    телефон окремим кроком FSM і викликає identify_by_phone напряму.
    """
    by_id = identify_by_telegram_id(cache, telegram_id)
    if by_id.found:
        return by_id

    by_username = identify_by_username(cache, telegram_username)
    if by_username.found:
        return by_username

    if phone_number:
        by_phone = identify_by_phone(cache, phone_number)
        if by_phone.found:
            return by_phone

    return IdentificationResult(None, IdentificationMethod.NOT_FOUND)


def _normalize_phone(phone: str) -> str:
    """
    Прибирає все, крім цифр, і відкидає міжнародний '+' /
    провідний '0' неоднозначності не вирішує повністю (це окрема
    задача валідації на вході з Webflow), але достатньо для збігу
    '+380501112233' == '380501112233' == '0501112233' (без коду країни
    зрівняти не можна, тому Webflow-форма має або завжди слати з кодом
    країни, або тут варто додати explicit-конфіг дефолтного коду).
    """
    digits = "".join(ch for ch in str(phone) if ch.isdigit())
    return digits[-9:] if len(digits) >= 9 else digits