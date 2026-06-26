"""
Скрипт ініціалізації та оновлення Google Таблиці для course_bot.

Що робить:
  - Якщо вкладки немає — створює з усіма колонками
  - Якщо вкладка є — дописує лише відсутні колонки (не чіпає дані)
  - Безпечний для повторного запуску

Запуск:
    python setup_spreadsheet.py
"""

import sys
import gspread
from gspread.exceptions import SpreadsheetNotFound
from dotenv import load_dotenv
import os

load_dotenv()

CREDENTIALS_PATH = os.getenv("GOOGLE_CREDENTIALS_PATH")
SPREADSHEET_ID   = os.getenv("SPREADSHEET_ID")

SHEETS = {
    "Streams": [
        "stream_id", "title", "is_active",
    ],
    "Stages": [
        "stage_id", "stream_id", "order", "title",
        "video_chat_id", "video_message_id",
        "notes_text",
        "circle_1_chat_id", "circle_1_message_id",
        "circle_2_chat_id", "circle_2_message_id",
        "circle_3_chat_id", "circle_3_message_id",
        "media_1_chat_id", "media_1_message_id", "media_1_file_id",
        "media_2_chat_id", "media_2_message_id", "media_2_file_id",
        "media_3_chat_id", "media_3_message_id", "media_3_file_id",
        "media_4_chat_id", "media_4_message_id", "media_4_file_id",
        "media_5_chat_id", "media_5_message_id", "media_5_file_id",
        "media_6_chat_id", "media_6_message_id", "media_6_file_id",
        "media_7_chat_id", "media_7_message_id", "media_7_file_id",
        "media_8_chat_id", "media_8_message_id", "media_8_file_id",
        "media_9_chat_id", "media_9_message_id", "media_9_file_id",
        "media_10_chat_id", "media_10_message_id", "media_10_file_id",
        "unlock_button_text", "is_active",
    ],
    "Plans": [
        "plan_id", "stream_id", "plan_type", "title", "start_date", "is_active",
    ],
    "Participants": [
        "participant_id", "telegram_id", "telegram_username", "phone_number",
        "stream_id", "plan_id", "access_token", "token_used", "status",
        "current_stage_order", "fsm_state", "joined_at", "activated_at",
        "last_progress_at", "notification_sent",
    ],
}


def _col_index_to_letter(index: int) -> str:
    """1 -> A, 26 -> Z, 27 -> AA ..."""
    result = ""
    while index > 0:
        index, remainder = divmod(index - 1, 26)
        result = chr(65 + remainder) + result
    return result


def main():
    if not CREDENTIALS_PATH or not SPREADSHEET_ID:
        print("❌ Не знайдено GOOGLE_CREDENTIALS_PATH або SPREADSHEET_ID у .env")
        sys.exit(1)

    if not os.path.exists(CREDENTIALS_PATH):
        print(f"❌ Файл ключа не знайдено: {CREDENTIALS_PATH}")
        sys.exit(1)

    print(f"🔑 Ключ:     {CREDENTIALS_PATH}")
    print(f"📋 Таблиця:  {SPREADSHEET_ID}")
    print()

    print("⏳ Підключаємось до Google...")
    try:
        gc = gspread.service_account(filename=CREDENTIALS_PATH)
    except Exception as e:
        print(f"❌ Не вдалось авторизуватись: {e}")
        sys.exit(1)

    try:
        spreadsheet = gc.open_by_key(SPREADSHEET_ID)
    except SpreadsheetNotFound:
        print("❌ Таблицю не знайдено. Перевірте SPREADSHEET_ID і доступ service account.")
        sys.exit(1)
    except Exception as e:
        print(f"❌ Помилка відкриття таблиці: {e}")
        sys.exit(1)

    print(f"✅ Підключення успішне! Таблиця: «{spreadsheet.title}»")
    print()

    existing_titles = [ws.title for ws in spreadsheet.worksheets()]

    for sheet_name, columns in SHEETS.items():
        if sheet_name not in existing_titles:
            # вкладки немає — створюємо з нуля
            ws = spreadsheet.add_worksheet(
                title=sheet_name, rows=1000, cols=len(columns) + 2
            )
            ws.append_row(columns)
            print(f"✅ «{sheet_name}» — створено, {len(columns)} колонок")
            continue

        ws = spreadsheet.worksheet(sheet_name)
        existing_headers = ws.row_values(1)

        if not existing_headers:
            ws.append_row(columns)
            print(f"✅ «{sheet_name}» — була порожня, заголовки додано")
            continue

        # дописуємо лише відсутні колонки — дані не чіпаємо
        missing = [col for col in columns if col not in existing_headers]

        if not missing:
            print(f"✅ «{sheet_name}» — всі {len(columns)} колонок вже є")
            continue

        # розширюємо вкладку якщо нових колонок не вистачає
        needed_cols = len(existing_headers) + len(missing)
        if ws.col_count < needed_cols:
            ws.resize(rows=ws.row_count, cols=needed_cols + 5)

        next_col = len(existing_headers) + 1
        for i, col_name in enumerate(missing):
            col_letter = _col_index_to_letter(next_col + i)
            ws.update_acell(f"{col_letter}1", col_name)

        print(f"✅ «{sheet_name}» — додано {len(missing)} нових колонок: {', '.join(missing)}")

    print()
    print("🎉 Готово!")
    print(f"🔗 https://docs.google.com/spreadsheets/d/{SPREADSHEET_ID}")


if __name__ == "__main__":
    main()