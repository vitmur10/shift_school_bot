"""
WriteQueue: черга відкладених записів у Google Sheets.

Замість запису при кожній дії користувача (що швидко впреться в ліміт
Google Sheets API — 60 write-запитів/хв на проект), зміни накопичуються
в пам'яті і скидаються пачкою (batch_update) за розкладом — див.
jobs/write_flush.py.

Дедуплікація: якщо те саме поле (sheet, row, column) змінюється кілька
разів між двома flush, у черзі лишається тільки останнє значення —
немає сенсу писати в Sheets проміжні стани.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any


@dataclass
class PendingWrite:
    """Один запланований запис у конкретну клітинку конкретного листа."""

    sheet_name: str
    row_index: int          # номер рядка в Google Sheets (з урахуванням заголовка)
    column: str              # буква колонки, напр. "F", або A1-нотація без рядка
    value: Any
    participant_id: str | None = None  # для логування/дебагу, не бере участі в дедуплікації
    enqueued_at: datetime = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.enqueued_at is None:
            self.enqueued_at = datetime.now(timezone.utc)

    @property
    def dedup_key(self) -> tuple[str, int, str]:
        return (self.sheet_name, self.row_index, self.column)


@dataclass
class AppendRow:
    """
    Окремий тип запису — додавання НОВОГО рядка (напр. новий Participant
    від Webflow webhook). На відміну від PendingWrite, тут немає row_index
    наперед (його видасть Sheets), тому append-и не дедуплікуються між
    собою і завжди виконуються окремим append_rows-викликом при flush.
    """

    sheet_name: str
    row_values: list[Any]
    participant_id: str | None = None
    enqueued_at: datetime = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.enqueued_at is None:
            self.enqueued_at = datetime.now(timezone.utc)


class WriteQueue:
    """
    Потокобезпечна (через asyncio.Lock) черга відкладених записів.

    enqueue/enqueue_append викликаються з handler'ів і services одразу
    після зміни даних — сама мережева операція в Sheets відбудеться
    пізніше, у flush-циклі.
    """

    def __init__(self, column_index_map: dict[str, dict[str, int]] | None = None) -> None:
        """
        column_index_map: {sheet_name: {column_letter: position_in_row_values}}
        Потрібен лише для патчингу pending append-ів (див. _try_patch_pending_append).
        Якщо не передано — точкові записи для ще не відправлених append-ів
        просто стають окремими (потенційно ризикованими) writes, як і
        раніше; передавання мапи — рекомендована практика для листа
        Participants, див. webhook/handlers.py:PARTICIPANTS_COLUMN_MAP.
        """
        self._pending_writes: dict[tuple[str, int, str], PendingWrite] = {}
        self._pending_appends: list[AppendRow] = []
        self._lock = asyncio.Lock()
        self._column_index_map = column_index_map or {}

    async def enqueue(self, write: PendingWrite) -> None:
        """
        Додає точковий запис у клітинку. Дублі за тим самим ключем перезаписуються.

        Якщо для цього ж participant_id у черзі ще лежить НЕвідправлений
        AppendRow (учасника щойно створено через webhook, реальний
        row_index у Sheets ще невідомий) — точковий запис НЕ додається
        окремо, бо writes/appends застосовуються в одному flush-циклі
        в порядку (writes, потім appends), і запис за row_index=-1
        зламав би структуру таблиці. Замість цього зміна вноситься
        напряму в ще не відправлений AppendRow.row_values.
        """
        async with self._lock:
            if write.participant_id and self._try_patch_pending_append(write):
                return
            self._pending_writes[write.dedup_key] = write

    def _try_patch_pending_append(self, write: PendingWrite) -> bool:
        """
        Шукає серед ще не відправлених AppendRow той, що належить тому ж
        participant_id, і патчить відповідну колонку безпосередньо в
        row_values за позицією з self._column_index_map[sheet_name].
        Повертає True, якщо патч застосовано (тобто окремий запис не потрібен).
        """
        column_map = self._column_index_map.get(write.sheet_name)
        if not column_map or write.column not in column_map:
            return False

        for append in self._pending_appends:
            if append.participant_id != write.participant_id:
                continue
            if append.sheet_name != write.sheet_name:
                continue
            col_index = column_map[write.column]
            if col_index >= len(append.row_values):
                continue
            append.row_values[col_index] = write.value
            return True
        return False

    async def enqueue_append(self, append: AppendRow) -> None:
        """Додає новий рядок у чергу (напр. новий учасник)."""
        async with self._lock:
            self._pending_appends.append(append)

    async def drain(self) -> tuple[list[PendingWrite], list[AppendRow]]:
        """
        Атомарно забирає все накопичене і очищає чергу.
        Повертає (точкові_записи, нові_рядки) — саме в такому порядку
        їх і слід застосовувати: спершу апдейти існуючих, потім append-и,
        щоб номери рядків не "поїхали" під час одного flush-циклу.
        """
        async with self._lock:
            writes = list(self._pending_writes.values())
            appends = list(self._pending_appends)
            self._pending_writes.clear()
            self._pending_appends.clear()
            return writes, appends

    async def requeue(self, writes: list[PendingWrite], appends: list[AppendRow]) -> None:
        """
        Повертає в чергу записи, які щойно були забрані через drain(),
        але не вдалось застосувати в Sheets (мережева помилка тощо).

        Важливо: НЕ перезаписує ключі, які вже встигли з'явитись у черзі
        заново (наприклад, handler поклав новіше значення для тієї самої
        клітинки, поки flush з попередньою версією falив). Старе значення,
        що повертається, програє новому — інакше можна "відкотити" вже
        актуальніші дані назад до застарілих.
        """
        async with self._lock:
            for write in writes:
                self._pending_writes.setdefault(write.dedup_key, write)
            self._pending_appends.extend(appends)

    def pending_count(self) -> int:
        """Для діагностики/admin-команди — скільки записів очікує на flush."""
        return len(self._pending_writes) + len(self._pending_appends)