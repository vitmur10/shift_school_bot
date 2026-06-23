"""
Фейковий in-memory gspread-клієнт для локальних тестів без реального
підключення до Google Sheets.

Імітує мінімум API, яким користується SheetsClient:
- worksheet(name) -> FakeWorksheet
- get_all_records() -> list[dict]
- batch_update([{range, values}])
- append_rows([[...]])

Не використовується в production-коді, лише в tests/.
"""

from __future__ import annotations

from typing import Any


def _col_letter_to_index(letter: str) -> int:
    """'A' -> 0, 'B' -> 1, ... 'Z' -> 25, 'AA' -> 26 ..."""
    result = 0
    for ch in letter:
        result = result * 26 + (ord(ch.upper()) - ord("A") + 1)
    return result - 1


class FakeWorksheet:
    def __init__(self, header: list[str], rows: list[list[Any]]) -> None:
        self.header = header
        # rows[0] відповідає рядку №2 в Sheets (рядок 1 — заголовок)
        self.rows = rows

    def get_all_records(self) -> list[dict[str, Any]]:
        return [dict(zip(self.header, row)) for row in self.rows]

    def batch_update(self, data: list[dict[str, Any]]) -> None:
        for item in data:
            cell_range = item["range"]  # напр. "J5"
            value = item["values"][0][0]
            col_letters = "".join(ch for ch in cell_range if ch.isalpha())
            row_num = int("".join(ch for ch in cell_range if ch.isdigit()))
            row_idx = row_num - 2  # переводимо номер рядка Sheets у індекс self.rows
            col_idx = _col_letter_to_index(col_letters)

            # розширюємо рядок/таблицю, якщо запис виходить за поточні межі
            while len(self.rows) <= row_idx:
                self.rows.append([""] * len(self.header))
            while len(self.rows[row_idx]) <= col_idx:
                self.rows[row_idx].append("")

            self.rows[row_idx][col_idx] = value

    def append_rows(self, values: list[list[Any]], value_input_option: str = "USER_ENTERED") -> None:
        for row in values:
            self.rows.append(list(row))


class FakeSpreadsheet:
    def __init__(self) -> None:
        self._sheets: dict[str, FakeWorksheet] = {}

    def add_sheet(self, name: str, header: list[str], rows: list[list[Any]]) -> None:
        self._sheets[name] = FakeWorksheet(header, rows)

    def worksheet(self, name: str) -> FakeWorksheet:
        if name not in self._sheets:
            raise KeyError(f"Лист '{name}' не зареєстровано у FakeSpreadsheet")
        return self._sheets[name]