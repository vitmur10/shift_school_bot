"""FastAPI app, реєстрація роутів."""

from __future__ import annotations

import json
import logging

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse
from pydantic import ValidationError

from storage.cache_store import CacheStore
from storage.write_queue import WriteQueue
from webhook.handlers import (
    build_wayforpay_accept_response,
    handle_wayforpay_payment,
    handle_webflow_lead,
    verify_wayforpay_signature,
)
from webhook.schemas import WayForPayCallback, WebflowFormSubmission

logger = logging.getLogger(__name__)


def build_fastapi_app(cache: CacheStore, queue: WriteQueue) -> FastAPI:
    app = FastAPI()

    @app.exception_handler(RequestValidationError)
    async def log_validation_errors(request: Request, exc: RequestValidationError):
        raw_body = await request.body()
        logger.error(
            "422 Validation error на %s %s\nRaw body: %s\nErrors: %s",
            request.method,
            request.url.path,
            raw_body.decode("utf-8", errors="replace"),
            exc.errors(),
        )
        # jsonable_encoder декодує сирі bytes у 'input', інакше JSONResponse
        # падає з TypeError: Object of type bytes is not JSON serializable.
        return JSONResponse(
            status_code=422,
            content=jsonable_encoder({"detail": exc.errors()}),
        )

    @app.post("/webhook/webflow")
    async def webflow_webhook(submission: WebflowFormSubmission):
        """
        Приймає нативний form_submission webhook від Webflow (сама форма,
        ДО оплати). Лише зберігає контакти (Telegram/телефон) у пам'яті --
        Participant тут НЕ створюється, у Sheets нічого не пишеться.
        """
        lead = await handle_webflow_lead(submission, cache)
        return {"ok": True, "phone": lead.raw_phone}

    @app.post("/webhook/wayforpay")
    async def wayforpay_webhook(request: Request):
        """
        Приймає callback від WayForPay (serviceUrl) після спроби оплати.
        Саме тут -- і лише тут -- створюється Participant і робиться
        реальний запис у Google Sheets, і лише якщо оплата Approved.

        WayForPay шле тіло з Content-Type application/x-www-form-urlencoded
        (не application/json), тому FastAPI не парсить його автоматично --
        читаємо сире тіло і валідуємо модель вручну.

        ВАЖЛИВО: WayForPay вимагає у відповідь строго визначений JSON
        з підписом (build_wayforpay_accept_response), інакше вважає
        доставку невдалою і буде повторювати callback нескінченно.
        """
        raw_body = await request.body()
        payload: dict | None = None
        try:
            payload = json.loads(raw_body)
            callback = WayForPayCallback.model_validate(payload)
        except (json.JSONDecodeError, ValidationError) as exc:
            logger.error(
                "Не вдалося розпарсити callback WayForPay: %s\nRaw body: %s",
                exc,
                raw_body.decode("utf-8", errors="replace"),
            )
            # Повертаємо 200 з accept, щоб WayForPay не спамив ретраями.
            order_ref = payload.get("orderReference", "") if isinstance(payload, dict) else ""
            return build_wayforpay_accept_response(order_ref)

        if not verify_wayforpay_signature(callback, raw=payload):
            logger.error(
                "Відхилено callback WayForPay через невірний підпис: orderReference=%s",
                callback.orderReference,
            )
            # Все одно повертаємо 200 з accept, щоб WayForPay не спамив ретраями --
            # сам факт невірного підпису вже залоговано як ERROR вище.
            return build_wayforpay_accept_response(callback.orderReference)

        await handle_wayforpay_payment(callback, cache, queue)
        return build_wayforpay_accept_response(callback.orderReference)

    @app.get("/health")
    async def health():
        return {"ok": True, "cache_synced_at": cache.last_synced_at}

    return app