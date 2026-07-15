"""Settings (pydantic-settings): токени, ID таблиці, інтервали, адмін-id."""

from __future__ import annotations

import json

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

    # ---- WayForPay ----
    WAYFORPAY_MERCHANT_ACCOUNT: str = ""
    WAYFORPAY_MERCHANT_SECRET_KEY: str = ""
    # JSON-рядок у .env: {"productName з WayForPay": "stream_id:plan_id", ...}
    # productName беремо як він приходить у callback (поле "productName", перший елемент масиву).
    # Приклад значення для .env (один рядок, без переносів):
    # WAYFORPAY_PRODUCT_MAP={"Потік 1 - Повна оплата":"stream_1:full","Потік 1 - Розстрочка":"stream_1:installment","Потік 2 - Повна оплата":"stream_2:full","Потік 2 - Розстрочка":"stream_2:installment","Потік 3 - Повна оплата":"stream_3:full","Потік 3 - Розстрочка":"stream_3:installment"}
    WAYFORPAY_PRODUCT_MAP: str = "{}"

    @property
    def admin_ids_set(self) -> set[int]:
        return {int(x) for x in self.ADMIN_IDS.split(",") if x.strip()}

    @property
    def wayforpay_product_map(self) -> dict[str, tuple[str, str]]:
        """Парсить WAYFORPAY_PRODUCT_MAP у {productName: (stream_id, plan_id)}."""
        raw: dict[str, str] = json.loads(self.WAYFORPAY_PRODUCT_MAP or "{}")
        result: dict[str, tuple[str, str]] = {}
        for product_name, value in raw.items():
            stream_id, _, plan_id = value.partition(":")
            result[product_name] = (stream_id, plan_id)
        return result


settings = Settings()