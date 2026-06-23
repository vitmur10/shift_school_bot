"""Settings (pydantic-settings): токени, ID таблиці, інтервали, адмін-id."""

from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")

    BOT_TOKEN: str
    SPREADSHEET_ID: str
    GOOGLE_CREDENTIALS_PATH: str
    WEBHOOK_PORT: int = 8000
    CACHE_REFRESH_SEC: int = 180
    WRITE_FLUSH_SEC: int = 20
    SCHEDULED_CHECK_SEC: int = 60
    ADMIN_IDS: str = ""  # comma-separated tg_id, напр. "111,222"

    @property
    def admin_ids_set(self) -> set[int]:
        return {int(x) for x in self.ADMIN_IDS.split(",") if x.strip()}


settings = Settings()