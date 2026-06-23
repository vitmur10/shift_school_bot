"""Pydantic-моделі вхідного payload з Webflow."""

from __future__ import annotations

from pydantic import BaseModel, Field


class WebflowPaymentPayload(BaseModel):
    """
    Очікуваний мінімум від Webflow при оплаті курсу.
    telegram_username опційний (учасник міг не вказати) — phone_number
    тоді обов'язковий, бо це єдиний запасний ідентифікатор.
    """
    telegram_username: str | None = Field(default=None)
    phone_number: str
    stream_id: str
    plan_id: str