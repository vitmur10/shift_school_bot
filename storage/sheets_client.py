"""
SheetsClient: тонка обгортка над gspread.

Єдине місце в проєкті, де відбуваються реальні звернення до Google Sheets
API. Усі методи синхронні (бо gspread синхронний), тому виклики з
async-коду мають йти через asyncio.to_thread — інакше один повільний
HTTP-запит до Google заблокує весь event loop (і aiogram-polling,
і FastAPI webhook одночасно).

На цьому етапі (поки немає реальної таблиці/кредів) клас написаний так,
щоб його було легко підмінити SheetsClientStub (див. tests/) — реальне
підключення gspread підʼєднаємо окремим кроком, коли будуть дані.
"""

from __future__ import annotations

from typing import Any, Protocol

from storage.write_queue import AppendRow, PendingWrite


class WorksheetLike(Protocol):
    """Мінімальний інтерфейс, який нам потрібен від gspread.Worksheet (для тестів/стабів)."""

    def get_all_records(self) -> list[dict[str, Any]]: ...
    def batch_update(self, data: list[dict[str, Any]]) -> Any: ...
    def append_rows(self, values: list[list[Any]], value_input_option: str = "USER_ENTERED") -> Any: ...


class SheetsClient:
    """
    Обгортка над одним Google Spreadsheet з кількома листами.

    gclient — будь-який об'єкт з методом .worksheet(name) -> WorksheetLike.
    Реально це буде gspread.Spreadsheet, але на етапі розробки без кредів
    можна підставити фейковий об'єкт (див. storage/sheets_client_stub.py,
    додамо коли знадобиться для локальних тестів).
    """

    SHEET_STREAMS = "Streams"
    SHEET_STAGES = "Stages"
    SHEET_PLANS = "Plans"
    SHEET_PARTICIPANTS = "Participants"
    SHEET_AUDIT_LOG = "AuditLog"

    def __init__(self, gclient: Any) -> None:
        self._gclient = gclient

    def _worksheet(self, sheet_name: str) -> WorksheetLike:
        return self._gclient.worksheet(sheet_name)

    # ---- читання (повне завантаження листа, синхронно) ----

    def read_all_records(self, sheet_name: str) -> list[dict[str, Any]]:
        """
        Повертає список словників (заголовок -> значення) для кожного рядка.
        Викликається з jobs/cache_refresh.py через asyncio.to_thread.
        """
        return self._worksheet(sheet_name).get_all_records()

    def read_streams(self) -> list[dict[str, Any]]:
        return self.read_all_records(self.SHEET_STREAMS)

    def read_stages(self) -> list[dict[str, Any]]:
        return self.read_all_records(self.SHEET_STAGES)

    def read_plans(self) -> list[dict[str, Any]]:
        return self.read_all_records(self.SHEET_PLANS)

    def read_participants(self) -> list[dict[str, Any]]:
        return self.read_all_records(self.SHEET_PARTICIPANTS)

    # ---- запис (застосування накопиченої черги, синхронно) ----

    def apply_writes(self, writes: list[PendingWrite]) -> None:
        """
        Групує точкові записи за листом і робить один batch_update на лист.
        Викликається з jobs/write_flush.py через asyncio.to_thread.
        """
        if not writes:
            return

        by_sheet: dict[str, list[PendingWrite]] = {}
        for w in writes:
            by_sheet.setdefault(w.sheet_name, []).append(w)

        for sheet_name, items in by_sheet.items():
            worksheet = self._worksheet(sheet_name)
            cell_updates = [
                {"range": f"{w.column}{w.row_index}", "values": [[w.value]]}
                for w in items
            ]
            worksheet.batch_update(cell_updates)

    def apply_appends(self, appends: list[AppendRow]) -> None:
        """
        Групує нові рядки за листом і робить один append_rows на лист.
        Порядок колонок у row_values МАЄ відповідати порядку колонок у
        самій таблиці — формування правильного порядку лежить на викликаючому
        коді (services/), sheets_client про семантику колонок не знає.
        """
        if not appends:
            return

        by_sheet: dict[str, list[list[Any]]] = {}
        for a in appends:
            by_sheet.setdefault(a.sheet_name, []).append(a.row_values)

        for sheet_name, rows in by_sheet.items():
            worksheet = self._worksheet(sheet_name)
            worksheet.append_rows(rows, value_input_option="USER_ENTERED")

    def apply_queue_snapshot(self, writes: list[PendingWrite], appends: list[AppendRow]) -> None:
        """Зручний агрегат: застосувати і точкові записи, і нові рядки за один виклик."""
        self.apply_writes(writes)
        self.apply_appends(appends)