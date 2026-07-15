"""Pydantic-моделі вхідних payload-ів: Webflow (форма) та WayForPay (оплата)."""

from __future__ import annotations

from pydantic import BaseModel, Field, computed_field


class WebflowFormData(BaseModel):
    """Поля самої форми на сторінці — назви збігаються з fieldName у Webflow."""

    Telegram: str | None = Field(default=None)
    phone: str
    Email: str | None = Field(default=None)


class WebflowFormPayload(BaseModel):
    data: WebflowFormData


class WebflowFormSubmission(BaseModel):
    """
    Реальний формат нативного Webflow form_submission webhook-а.
    Це НЕ оплата — лише збір контактів (Telegram/телефон) до оплати.
    stream_id/plan_id тут немає і бути не може: вони визначаються пізніше,
    з callback-у WayForPay після успішної оплати.
    """

    triggerType: str
    payload: WebflowFormPayload


class WayForPayCallback(BaseModel):
    """
    Callback від WayForPay (serviceUrl) після спроби оплати.
    Список полів — мінімум, який реально потрібен нашій бізнес-логіці;
    WayForPay надсилає й інші поля, вони ігноруються завдяки extra="ignore".
    """

    model_config = {"extra": "ignore"}

    merchantAccount: str
    orderReference: str
    amount: float
    currency: str
    authCode: str | None = None
    cardPan: str | None = None
    transactionStatus: str
    reasonCode: int | str | None = None
    merchantSignature: str
    phone: str | None = None
    email: str | None = None
    # WayForPay надсилає товари масивом об'єктів у полі `products`:
    # [{"name": "...", "price": 1, "count": 1}], а не пласким `productName`.
    products: list[dict] | None = None

    @computed_field  # type: ignore[prop-decorator]
    @property
    def productName(self) -> list[str]:
        """Назви товарів з `products` -- саме за ними мапиться stream/plan."""
        if not self.products:
            return []
        return [str(p.get("name", "")) for p in self.products if p.get("name")]