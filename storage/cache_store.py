"""
CacheStore: in-memory кеш над Google Sheets.

Усі швидкі операції (перевірка доступу, видача етапу, пошук учасника)
йдуть ЛИШЕ через цю структуру — жодних прямих звернень до Sheets API
у bot/handlers чи services.

Оновлюється цілком (atomic swap) у jobs/cache_refresh.py.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone

from storage.models import Participant, Stream


def normalize_phone(phone: str) -> str:
    """
    Приводить телефон до єдиного формату для зіставлення форми (Webflow)
    і оплати (WayForPay) — вони можуть слати номер по-різному
    ("+380972681637" vs "380972681637" vs з пробілами/дефісами).
    Лишає лише цифри, відкидає ведучий '0' зайвих країнових префіксів не робимо —
    просто зводимо до "останні 9 цифр після коду країни 380", якщо номер укр.
    """
    digits = re.sub(r"\D", "", phone or "")
    if digits.startswith("380") and len(digits) >= 12:
        digits = digits[-9:]
    elif digits.startswith("0") and len(digits) == 10:
        digits = digits[-9:]
    return digits


@dataclass
class Lead:
    """
    Заявка з форми Webflow ДО оплати: маємо контакти, ще не маємо
    stream_id/plan_id (з'являться з callback-у WayForPay).
    Живе лише в пам'яті — у Sheets нічого не пишеться, поки нема оплати.
    """

    phone_number: str  # нормалізований, див. normalize_phone
    telegram_username: str | None
    raw_phone: str
    email: str | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


@dataclass
class LeadRow:
    """
    Рядок листа Leads (персистентний облік заявок з форми — на відміну від
    in-memory Lead вище, що живе лише між формою та оплатою).
    row_index потрібен для точкового оновлення статусу на «оплатив».
    """

    phone: str          # нормалізований
    status: str         # напр. "очікує оплату" / "оплатив"
    row_index: int
    raw_phone: str = ""


@dataclass
class CacheStore:
    streams: dict[str, Stream] = field(default_factory=dict)

    # індекси учасників — для O(1) пошуку замість перебору
    participants_by_id: dict[str, Participant] = field(default_factory=dict)
    participants_by_tg_id: dict[int, str] = field(default_factory=dict)       # tg_id -> participant_id
    participants_by_username: dict[str, str] = field(default_factory=dict)    # normalized username -> participant_id
    participants_by_token: dict[str, str] = field(default_factory=dict)       # token -> participant_id

    # ліди з форми Webflow, що чекають на підтвердження оплати від WayForPay
    pending_leads: dict[str, Lead] = field(default_factory=dict)  # normalized phone -> Lead

    # персистентний облік заявок у листі Leads (читається з Sheets).
    # leads_enabled=False, якщо листа Leads немає — тоді облік просто вимкнено,
    # решта бота працює як раніше (повна зворотна сумісність).
    leads_enabled: bool = False
    leads_by_phone: dict[str, LeadRow] = field(default_factory=dict)  # normalized phone -> LeadRow

    last_synced_at: datetime | None = None

    # ---- читання ----

    def get_stream(self, stream_id: str) -> Stream | None:
        return self.streams.get(stream_id)

    def get_participant_by_id(self, participant_id: str) -> Participant | None:
        return self.participants_by_id.get(participant_id)

    def get_participant_by_tg_id(self, tg_id: int) -> Participant | None:
        participant_id = self.participants_by_tg_id.get(tg_id)
        return self.participants_by_id.get(participant_id) if participant_id else None

    def get_participant_by_username(self, username: str) -> Participant | None:
        normalized = username.lstrip("@").lower()
        participant_id = self.participants_by_username.get(normalized)
        return self.participants_by_id.get(participant_id) if participant_id else None

    def get_participant_by_token(self, token: str) -> Participant | None:
        participant_id = self.participants_by_token.get(token)
        return self.participants_by_id.get(participant_id) if participant_id else None

    def all_participants(self) -> list[Participant]:
        return list(self.participants_by_id.values())

    def get_lead(self, phone_number: str) -> Lead | None:
        return self.pending_leads.get(normalize_phone(phone_number))

    def upsert_lead(self, lead: Lead) -> None:
        """Кладе/оновлює лід з форми (до оплати). Ключ — нормалізований телефон."""
        self.pending_leads[lead.phone_number] = lead

    def pop_lead(self, phone_number: str) -> Lead | None:
        """
        Забирає лід за телефоном (використовується при підтвердженій оплаті —
        лід перетворюється на Participant і більше не потрібен в очікуванні).
        """
        return self.pending_leads.pop(normalize_phone(phone_number), None)

    def get_lead_row(self, phone_number: str) -> LeadRow | None:
        """Рядок листа Leads за телефоном (для оновлення статусу на «оплатив»)."""
        return self.leads_by_phone.get(normalize_phone(phone_number))

    # ---- запис у кеш (викликається ПІСЛЯ успішного enqueue у WriteQueue,
    # щоб handler одразу бачив оновлений стан, не чекаючи наступного refresh) ----

    def upsert_participant(self, participant: Participant) -> None:
        """Додає нового або оновлює існуючого учасника та всі індекси."""
        self.participants_by_id[participant.participant_id] = participant

        if participant.telegram_id is not None:
            self.participants_by_tg_id[participant.telegram_id] = participant.participant_id

        normalized = participant.normalized_username()
        if normalized:
            self.participants_by_username[normalized] = participant.participant_id

        if participant.access_token:
            self.participants_by_token[participant.access_token] = participant.participant_id

    def replace_with(self, other: "CacheStore") -> None:
        """
        Атомарна підміна вмісту поточного кешу даними з іншого CacheStore.

        Використовується у jobs/cache_refresh.py: новий кеш будується
        ПОВНІСТЮ окремо (у новому об'єкті), і лише наприкінці підміняється
        одним кроком — щоб жоден handler не побачив "напівзаповнений"
        стан під час перезавантаження.

        pending_leads НЕ підміняються — вони не приходять із Sheets
        (живуть лише в пам'яті між формою та оплатою), тому просто
        переносяться зі старого кешу як є.
        """
        self.streams = other.streams
        self.participants_by_id = other.participants_by_id
        self.participants_by_tg_id = other.participants_by_tg_id
        self.participants_by_username = other.participants_by_username
        self.participants_by_token = other.participants_by_token
        self.leads_enabled = other.leads_enabled
        self.leads_by_phone = other.leads_by_phone
        self.last_synced_at = other.last_synced_at or datetime.now(timezone.utc)