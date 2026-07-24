"""
Dataclasses: Stream, Stage, Plan, Participant.

Це "сирі" структури даних, що відображають рядки Google Sheets у пам'яті.
Без бізнес-логіки — лише поля та мінімальні зручні властивості.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum


class ParticipantStatus(str, Enum):
    PENDING = "pending"      # токен видано, не активовано (бот ще не бачив користувача)
    ACTIVE = "active"        # доступ відкрито, учасник проходить курс
    PAUSED = "paused"        # доступ тимчасово призупинено адміном
    BLOCKED = "blocked"      # доступ заблоковано (напр. порушення/повернення коштів)


class PlanType(str, Enum):
    INSTANT = "instant"      # доступ одразу після оплати
    SCHEDULED = "scheduled"  # доступ відкривається в задану дату (групова активація)


class MediaType(str, Enum):
    VIDEO = "video"
    VOICE = "voice"
    AUDIO = "audio"
    DOCUMENT = "document"
    PHOTO = "photo"

    @classmethod
    def from_raw(cls, value: str | None, default: "MediaType" = None) -> "MediaType":
        """Парсить рядок із Sheets; повертає default (або VIDEO) якщо порожньо/невідомо."""
        if default is None:
            default = cls.VIDEO
        if not value:
            return default
        try:
            return cls(str(value).strip().lower())
        except ValueError:
            return default


@dataclass
class ContentRef:
    """
    Посилання на вихідне повідомлення в адмін-каналі або file_id.

    Є два способи використання:
    - source_chat_id + source_message_id → copy_message (universal fallback)
    - file_id + media_type → send_video / send_voice / send_audio / send_media_group
    """

    source_chat_id: int
    source_message_id: int
    file_id: str | None = None
    media_type: MediaType = MediaType.VIDEO  # використовується при відправці через file_id

    def is_set(self) -> bool:
        return bool(self.source_chat_id and self.source_message_id) or bool(self.file_id)


@dataclass
class Stage:
    """Один етап курсу (відео-урок + конспект + кружечки)."""

    stage_id: str
    stream_id: str
    order: int
    title: str
    video_ref: ContentRef | None      # одне основне відео (None якщо медіагрупа)
    notes_text: str
    circle_refs: list[ContentRef] = field(default_factory=list)
    media_group: list[ContentRef] = field(default_factory=list)  # кілька медіа одним повідомленням
    unlock_button_text: str = "Далі"
    is_active: bool = True
    # TRUE = етап лише для тарифів зі стартом за датою (scheduled);
    # для instant-тарифів (доступ одразу) такий етап пропускається.
    only_scheduled: bool = False
    # Список plan_id, для яких доступний цей етап (якщо порожній — доступний для всіх).
    # Приклад: "plan_basic,plan_pro" — етап лише для цих тарифів.
    plan_ids: list[str] = field(default_factory=list)

    def active_circle_refs(self) -> list[ContentRef]:
        """Повертає лише заповнені посилання на кружечки."""
        return [ref for ref in self.circle_refs if ref and ref.is_set()]

    def active_media_group(self) -> list[ContentRef]:
        """Повертає лише заповнені посилання медіагрупи."""
        return [ref for ref in self.media_group if ref and ref.is_set()]

    def has_media_group(self) -> bool:
        return len(self.active_media_group()) > 1


@dataclass
class Plan:
    """Тариф, прив'язаний до потоку."""

    plan_id: str
    stream_id: str
    plan_type: PlanType
    title: str
    start_date: datetime | None = None  # обов'язково для SCHEDULED, ігнорується для INSTANT
    is_active: bool = True
    # особистий ТГ куратора (для тарифу з кураторством, напр. 3-й).
    # Опціонально: якщо порожньо — кнопки «Написати куратору» не буде.
    curator_url: str | None = None
    # посилання на чат тарифу. Опціонально: порожньо — кнопки «Чат» не буде.
    chat_url: str | None = None

    def is_scheduled(self) -> bool:
        return self.plan_type == PlanType.SCHEDULED


@dataclass
class Stream:
    """Потік/курс — наскрізна сутність для мультипотоковості."""

    stream_id: str
    title: str
    is_active: bool = True
    stages: list[Stage] = field(default_factory=list)   # відсортовані за order
    plans: dict[str, Plan] = field(default_factory=dict)  # plan_id -> Plan
    # інвайт-посилання на телеграм-групу цього потоку (self-service у таблиці).
    # Опціонально: якщо порожньо — кнопки «Група потоку» не буде.
    telegram_group_url: str | None = None

    def get_stage(self, order: int) -> Stage | None:
        """Повертає активний етап за порядковим номером, або None."""
        for stage in self.stages:
            if stage.order == order and stage.is_active:
                return stage
        return None

    def total_active_stages(self) -> int:
        return sum(1 for s in self.stages if s.is_active)

    def get_plan(self, plan_id: str) -> Plan | None:
        return self.plans.get(plan_id)


@dataclass
class Participant:
    """Учасник курсу — головна сутність стану користувача."""

    participant_id: str
    telegram_id: int | None
    telegram_username: str | None
    phone_number: str | None
    stream_id: str
    plan_id: str
    access_token: str
    token_used: bool
    status: ParticipantStatus
    current_stage_order: int
    fsm_state: str | None
    notification_sent: bool
    row_index: int  # номер рядка в Google Sheets — потрібен для точкового запису

    # необов'язкові часові мітки — для аудиту/аналітики, не критичні для логіки доступу
    joined_at: datetime | None = None
    activated_at: datetime | None = None
    last_progress_at: datetime | None = None

    # які нагадування-прогрів уже надіслані (напр. "24,1") — щоб не дублювати
    reminders_sent: str = ""

    def is_active(self) -> bool:
        return self.status == ParticipantStatus.ACTIVE

    def normalized_username(self) -> str | None:
        """Username без '@' та в нижньому регістрі — для консистентного пошуку в індексах."""
        if not self.telegram_username:
            return None
        return self.telegram_username.lstrip("@").lower()