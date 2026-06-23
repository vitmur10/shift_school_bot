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


@dataclass
class ContentRef:
    """
    Посилання на вихідне повідомлення в адмін-каналі/чаті, звідки бот
    копіює контент учаснику через copy_message (Bot API).

    Чому саме так, а не video_url/file_id напряму:
      - file_id сам по собі ПРАЦЮЄ для send_video(file_id=...), але тоді
        підпис (опис під відео) довелось би зберігати окремим полем і
        формувати повідомлення вручну.
      - copy_message(from_chat_id, message_id) копіює повідомлення
        ЦІЛКОМ — разом з підписом, типом контенту (video/video_note/
        document) і форматуванням — без потреби розрізняти на боці
        бота, що саме це за тип файлу. Для цього потрібні саме
        chat_id + message_id вихідного повідомлення, не file_id.

    Адмін заливає відео ОДИН РАЗ вручну (через звичайний Telegram-клієнт,
    не бота) у приватний канал, де бот має права адміна — тоді відео
    зберігається на серверах Telegram і не має обмеження 50 МБ, яке
    діє лише на ЗАВАНТАЖЕННЯ файлу через сам Bot API.
    """

    source_chat_id: int      # ID каналу/чату-сховища контенту (від'ємне число для каналів)
    source_message_id: int   # ID конкретного повідомлення з відео всередині цього чату

    def is_set(self) -> bool:
        return bool(self.source_chat_id and self.source_message_id)


@dataclass
class Stage:
    """Один етап курсу (відео-урок + конспект + кружечки)."""

    stage_id: str
    stream_id: str
    order: int
    title: str
    video_ref: ContentRef | None      # основний відео-урок (None, якщо ще не залито)
    notes_text: str
    circle_refs: list[ContentRef] = field(default_factory=list)  # 2-3 кружечки
    unlock_button_text: str = "Далі"
    is_active: bool = True

    def active_circle_refs(self) -> list[ContentRef]:
        """Повертає лише заповнені (заллиті) посилання на кружечки."""
        return [ref for ref in self.circle_refs if ref and ref.is_set()]


@dataclass
class Plan:
    """Тариф, прив'язаний до потоку."""

    plan_id: str
    stream_id: str
    plan_type: PlanType
    title: str
    start_date: datetime | None = None  # обов'язково для SCHEDULED, ігнорується для INSTANT
    is_active: bool = True

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

    def is_active(self) -> bool:
        return self.status == ParticipantStatus.ACTIVE

    def normalized_username(self) -> str | None:
        """Username без '@' та в нижньому регістрі — для консистентного пошуку в індексах."""
        if not self.telegram_username:
            return None
        return self.telegram_username.lstrip("@").lower()