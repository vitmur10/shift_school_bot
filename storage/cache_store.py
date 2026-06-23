"""
CacheStore: in-memory кеш над Google Sheets.

Усі швидкі операції (перевірка доступу, видача етапу, пошук учасника)
йдуть ЛИШЕ через цю структуру — жодних прямих звернень до Sheets API
у bot/handlers чи services.

Оновлюється цілком (atomic swap) у jobs/cache_refresh.py.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

from storage.models import Participant, Stream


@dataclass
class CacheStore:
    streams: dict[str, Stream] = field(default_factory=dict)

    # індекси учасників — для O(1) пошуку замість перебору
    participants_by_id: dict[str, Participant] = field(default_factory=dict)
    participants_by_tg_id: dict[int, str] = field(default_factory=dict)       # tg_id -> participant_id
    participants_by_username: dict[str, str] = field(default_factory=dict)    # normalized username -> participant_id
    participants_by_token: dict[str, str] = field(default_factory=dict)       # token -> participant_id

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
        одним кроком — щоб жоден handler не побачив "напівзаповнений" стан
        під час перезавантаження.
        """
        self.streams = other.streams
        self.participants_by_id = other.participants_by_id
        self.participants_by_tg_id = other.participants_by_tg_id
        self.participants_by_username = other.participants_by_username
        self.participants_by_token = other.participants_by_token
        self.last_synced_at = other.last_synced_at or datetime.now(timezone.utc)