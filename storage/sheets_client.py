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

import logging
from typing import Any, Protocol

from storage.write_queue import AppendRow, PendingWrite

logger = logging.getLogger(__name__)


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
    SHEET_LEADS = "Leads"
    SHEET_BROADCASTS = "Broadcasts"
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

        Читаємо через get_all_values і будуємо словники самі (а не
        get_all_records), щоб бути стійкими до:
          - порожніх/дубльованих заголовків (get_all_records на них падає
            з GSpreadException 'header contains duplicates');
          - типів: усі значення повертаються рядками, тож числові телефони
            не «зрізають» ведучий 0 і не ламають парсинг.
        Порожні колонки-заголовки просто ігноруються.
        """
        logger.debug("Читання всіх записів з листа %s", sheet_name)
        values = self._worksheet(sheet_name).get_all_values()
        if not values:
            return []

        header = [str(h).strip().lower() for h in values[0]]
        records: list[dict[str, Any]] = []
        for raw_row in values[1:]:
            record: dict[str, Any] = {}
            for i, key in enumerate(header):
                if not key:
                    continue  # порожній заголовок (порожня колонка) — пропускаємо
                record[key] = raw_row[i] if i < len(raw_row) else ""
            records.append(record)

        logger.debug("Отримано %d рядків з листа %s", len(records), sheet_name)
        return records

    def read_streams(self) -> list[dict[str, Any]]:
        return self.read_all_records(self.SHEET_STREAMS)

    def read_stages(self) -> list[dict[str, Any]]:
        return self.read_all_records(self.SHEET_STAGES)

    def read_plans(self) -> list[dict[str, Any]]:
        return self.read_all_records(self.SHEET_PLANS)

    def read_participants(self) -> list[dict[str, Any]]:
        return self.read_all_records(self.SHEET_PARTICIPANTS)

    def read_leads(self) -> list[dict[str, Any]] | None:
        """
        Читає лист Leads. Повертає None, якщо листа немає (тоді облік лідів
        просто вимкнено, решта бота працює як раніше) або читання не вдалось —
        це НЕ повинно валити весь refresh. Self-heal: щойно лист з'явиться,
        наступний цикл почне його читати автоматично.
        """
        try:
            return self.read_all_records(self.SHEET_LEADS)
        except Exception as e:
            logger.warning(
                "Лист %s недоступний (%s) — облік лідів вимкнено до появи листа",
                self.SHEET_LEADS, e,
            )
            return None

    def read_broadcasts(self) -> list[dict[str, Any]] | None:
        """
        Читає лист Broadcasts. Повертає None, якщо листа немає (розсилки
        вимкнено) — це не повинно валити роботу бота. Self-heal: як лист
        з'явиться, наступний цикл почне його читати.
        """
        try:
            return self.read_all_records(self.SHEET_BROADCASTS)
        except Exception as e:
            logger.warning(
                "Лист %s недоступний (%s) — ручні розсилки вимкнено до появи листа",
                self.SHEET_BROADCASTS, e,
            )
            return None

    def set_broadcast_status(self, row_index: int, status: str) -> None:
        """Швидко проставляє лише статус (E) — щоб «застовпити» рядок у роботу."""
        self._worksheet(self.SHEET_BROADCASTS).batch_update([
            {"range": f"E{row_index}", "values": [[status]]},
        ])

    def delete_leads_rows(self, row_indices: list[int]) -> None:
        """
        Видаляє рядки з листа Leads за їх номерами. Видаляємо у спадному
        порядку, щоб номери решти рядків не «поїхали» під час видалення.
        Використовується, коли лід оплатив — він лишається у Participants,
        а з Leads прибирається (Leads = лише неоплачені).
        """
        ws = self._worksheet(self.SHEET_LEADS)
        for idx in sorted({i for i in row_indices if i and i >= 2}, reverse=True):
            ws.delete_rows(idx)

    def update_broadcast_status(
        self,
        row_index: int,
        status: str,
        sent_at: str,
        sent_count: int,
        note: str = "",
    ) -> None:
        """
        Прямо (синхронно) проставляє результат розсилки в рядок Broadcasts:
        E status, F sent_at, G sent_count, H note. Пряме оновлення (а не через
        WriteQueue) — щоб статус зафіксувався НЕГАЙНО і той самий рядок не
        розіслався повторно навіть при рестарті між циклами.
        """
        worksheet = self._worksheet(self.SHEET_BROADCASTS)
        worksheet.batch_update([
            {"range": f"E{row_index}", "values": [[status]]},
            {"range": f"F{row_index}", "values": [[sent_at]]},
            {"range": f"G{row_index}", "values": [[sent_count]]},
            {"range": f"H{row_index}", "values": [[note]]},
        ])

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
            logger.debug(
                "batch_update у лист %s: %d комірок -> %r",
                sheet_name, len(cell_updates), cell_updates,
            )
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
            logger.debug(
                "append_rows у лист %s: %d рядків -> %r",
                sheet_name, len(rows), rows,
            )
            # table_range="A1" ОБОВ'ЯЗКОВИЙ: без нього gspread сам «вгадує»
            # місце вставки і може записати рядок зі зсувом праворуч (якщо в
            # заголовку є порожні хвостові колонки) — дані потрапляють не в ті
            # колонки. З "A1" вставка завжди вирівнюється по колонці A.
            worksheet.append_rows(
                rows, value_input_option="USER_ENTERED", table_range="A1",
            )

    def apply_queue_snapshot(self, writes: list[PendingWrite], appends: list[AppendRow]) -> None:
        """Зручний агрегат: застосувати і точкові записи, і нові рядки за один виклик."""
        self.apply_writes(writes)
        self.apply_appends(appends)