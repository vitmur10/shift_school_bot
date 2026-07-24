"""
Міграція структури Google Sheets під нові фічі.

Ідемпотентний і БЕЗПЕЧНИЙ скрипт: нічого не видаляє й не перевпорядковує,
лише додає те, чого бракує:

  1. Streams  -> колонка `telegram_group_url` (якщо немає)
  2. Plans    -> колонка `curator_url` (якщо немає)
  3. Leads    -> створює лист із заголовками (якщо листа немає)
  4. Participants -> лише ПЕРЕВІРЯЄ порядок колонок і попереджає, якщо він
     не збігається з тим, що очікує код (append учасника пише за позицією!).

Скрипт САМОДОСТАТНІЙ — не імпортує config/бота, тому працює й локально.
Дані для підключення береться (у порядку пріоритету):
  1) аргументи   --spreadsheet-id / --credentials
  2) змінні оточення SPREADSHEET_ID / GOOGLE_CREDENTIALS_PATH
  3) файл .env у корені проєкту (KEY=VALUE)

Приклади:
  # на сервері, де є .env:
  ./venv/bin/python scripts/setup_sheets.py
  # локально з явними параметрами:
  python scripts/setup_sheets.py --spreadsheet-id ABC123 --credentials creds.json
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path

import gspread

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# --- канонічні константи (тримати синхронними з webhook/handlers.py) ---------
PARTICIPANTS_COLUMN_ORDER = [
    "participant_id", "telegram_id", "telegram_username", "phone_number",
    "stream_id", "plan_id", "access_token", "token_used", "status",
    "current_stage_order", "fsm_state",
    "joined_at", "activated_at", "last_progress_at", "notification_sent",
    "reminders_sent",
]
# нова колонка Participants (дописується в кінець = позиція P)
PARTICIPANTS_NEW_COLUMNS = ["reminders_sent"]
LEADS_COLUMN_ORDER = [
    "created_at", "phone_number", "telegram_username", "email",
    "status", "paid_at", "stream_id", "plan_id",
]
BROADCASTS_COLUMN_ORDER = [
    "created_at", "stream_id", "plan_id", "message",
    "status", "sent_at", "sent_count", "note",
    "media", "button_url", "button_text",
]

STREAMS_NEW_COLUMNS = ["telegram_group_url"]
PLANS_NEW_COLUMNS = ["curator_url", "chat_url"]
STAGES_NEW_COLUMNS = ["only_scheduled", "plan_ids"]

SHEET_STREAMS = "Streams"
SHEET_STAGES = "Stages"
SHEET_PLANS = "Plans"
SHEET_PARTICIPANTS = "Participants"
SHEET_LEADS = "Leads"
SHEET_BROADCASTS = "Broadcasts"


def _load_dotenv(path: Path) -> dict[str, str]:
    """Мінімальний парсер .env (KEY=VALUE), без зовнішніх залежностей."""
    result: dict[str, str] = {}
    if not path.exists():
        return result
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        result[key.strip()] = value.strip().strip('"').strip("'")
    return result


def _resolve_settings(args) -> tuple[str, str]:
    """spreadsheet_id + credentials_path з args / env / .env."""
    dotenv = _load_dotenv(PROJECT_ROOT / ".env")

    spreadsheet_id = (
        args.spreadsheet_id
        or os.environ.get("SPREADSHEET_ID")
        or dotenv.get("SPREADSHEET_ID")
    )
    credentials = (
        args.credentials
        or os.environ.get("GOOGLE_CREDENTIALS_PATH")
        or dotenv.get("GOOGLE_CREDENTIALS_PATH")
    )

    if not spreadsheet_id or not credentials:
        raise SystemExit(
            "Не знайдено SPREADSHEET_ID та/або GOOGLE_CREDENTIALS_PATH.\n"
            "Передайте їх аргументами (--spreadsheet-id / --credentials), "
            "змінними оточення або через .env у корені проєкту."
        )

    # відносний шлях до кредів рахуємо від кореня проєкту
    cred_path = Path(credentials)
    if not cred_path.is_absolute():
        cred_path = PROJECT_ROOT / cred_path
    if not cred_path.exists():
        raise SystemExit(f"Файл кредів не знайдено: {cred_path}")

    return spreadsheet_id, str(cred_path)


def _open_spreadsheet(spreadsheet_id: str, credentials: str):
    print(f"Підключення до таблиці {spreadsheet_id} ...")
    gc = gspread.service_account(filename=credentials)
    return gc.open_by_key(spreadsheet_id)


def _get_worksheet(ss, name):
    try:
        return ss.worksheet(name)
    except gspread.exceptions.WorksheetNotFound:
        return None


def _header(ws) -> list[str]:
    return [str(v).strip() for v in ws.row_values(1)]


def ensure_columns(ss, sheet_name: str, required: list[str]) -> None:
    """Додає у кінець рядка-заголовка ті колонки, яких ще немає."""
    ws = _get_worksheet(ss, sheet_name)
    if ws is None:
        print(f"  [!] Лист '{sheet_name}' не знайдено — пропускаю (створіть його вручну).")
        return

    header = _header(ws)
    existing = {h.lower() for h in header}
    added = []
    for col in required:
        if col.lower() in existing:
            continue
        col_index = len(header) + 1  # наступна вільна колонка (1-based)
        ws.update_cell(1, col_index, col)
        header.append(col)
        added.append(col)

    if added:
        print(f"  [+] {sheet_name}: додано колонки {added}")
    else:
        print(f"  [=] {sheet_name}: усі потрібні колонки вже є ({required})")


def ensure_sheet_with_headers(ss, sheet_name: str, columns: list[str]) -> None:
    """Створює лист із заголовками, якщо його немає; або дозаписує відсутні заголовки."""
    ws = _get_worksheet(ss, sheet_name)
    if ws is not None:
        header = _header(ws)
        have = {h.lower() for h in header}
        missing = [c for c in columns if c.lower() not in have]
        if missing:
            # розширити сітку, якщо потрібно більше колонок
            needed_cols = len(header) + len(missing)
            if needed_cols > ws.col_count:
                ws.resize(cols=needed_cols + 5)
            for col in missing:
                ws.update_cell(1, len(header) + 1, col)
                header.append(col)
            print(f"  [+] {sheet_name}: лист існував, додано відсутні заголовки {missing}")
        else:
            print(f"  [=] {sheet_name}: лист уже існує з правильними заголовками")
        return

    ws = ss.add_worksheet(title=sheet_name, rows=1000, cols=max(len(columns), 8))
    ws.append_row(columns, value_input_option="USER_ENTERED")
    print(f"  [+] {sheet_name}: створено лист із заголовками {columns}")


def verify_participants_order(ss) -> None:
    """
    Перевіряє, що перші N колонок Participants ідуть у тому ж порядку, що очікує
    код (append учасника пише значення ЗА ПОЗИЦІЄЮ A..O). Нічого не змінює —
    лише голосно попереджає, бо авто-перевпорядкування колонок з даними ризиковане.
    """
    ws = _get_worksheet(ss, SHEET_PARTICIPANTS)
    if ws is None:
        print(f"  [!] Лист '{SHEET_PARTICIPANTS}' не знайдено — критично, перевірте назву.")
        return

    header = _header(ws)
    expected = PARTICIPANTS_COLUMN_ORDER
    mismatches = []
    for i, col in enumerate(expected):
        actual = header[i] if i < len(header) else "<немає>"
        if actual.strip().lower() != col.lower():
            mismatches.append((i + 1, col, actual))

    if not mismatches:
        print(f"  [=] Participants: порядок перших {len(expected)} колонок правильний")
        return

    print("  [!] Participants: НЕЗБІГ порядку колонок — append учасника може писати не в ту колонку!")
    print("      Очікуваний порядок (A..):", ", ".join(expected))
    for pos, want, got in mismatches:
        letter = chr(ord("A") + pos - 1) if pos <= 26 else f"#{pos}"
        print(f"      колонка {letter}: очікується '{want}', у таблиці '{got}'")
    print("      Виправте порядок вручну (перетягніть колонки), не видаляючи даних.")


def main() -> None:
    parser = argparse.ArgumentParser(description="Міграція структури Google Sheets")
    parser.add_argument("--spreadsheet-id", help="ID таблиці (перекриває env/.env)")
    parser.add_argument("--credentials", help="шлях до service_account JSON")
    args = parser.parse_args()

    spreadsheet_id, credentials = _resolve_settings(args)
    ss = _open_spreadsheet(spreadsheet_id, credentials)
    print("Готово. Застосовую міграцію:\n")

    ensure_columns(ss, SHEET_STREAMS, STREAMS_NEW_COLUMNS)
    ensure_columns(ss, SHEET_STAGES, STAGES_NEW_COLUMNS)
    ensure_columns(ss, SHEET_PLANS, PLANS_NEW_COLUMNS)
    ensure_columns(ss, SHEET_PARTICIPANTS, PARTICIPANTS_NEW_COLUMNS)
    ensure_sheet_with_headers(ss, SHEET_LEADS, LEADS_COLUMN_ORDER)
    ensure_sheet_with_headers(ss, SHEET_BROADCASTS, BROADCASTS_COLUMN_ORDER)
    verify_participants_order(ss)

    print("\nМіграцію завершено. Перезапустіть бота: sudo systemctl restart tgbot")


if __name__ == "__main__":
    main()
