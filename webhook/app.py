"""FastAPI app, реєстрація роутів."""

from __future__ import annotations

import logging

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from storage.cache_store import CacheStore
from storage.write_queue import WriteQueue
from webhook.handlers import handle_webflow_payment
from webhook.schemas import WebflowPaymentPayload

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
        return JSONResponse(status_code=422, content={"detail": exc.errors()})

    @app.post("/webhook/webflow")
    async def webflow_webhook(payload: WebflowPaymentPayload):
        participant = await handle_webflow_payment(payload, cache, queue)
        return {"ok": True, "participant_id": participant.participant_id}

    @app.get("/health")
    async def health():
        return {"ok": True, "cache_synced_at": cache.last_synced_at}

    return app