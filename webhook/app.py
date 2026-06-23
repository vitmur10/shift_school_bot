"""FastAPI app, реєстрація роутів."""

from __future__ import annotations

from fastapi import FastAPI

from storage.cache_store import CacheStore
from storage.write_queue import WriteQueue
from webhook.handlers import handle_webflow_payment
from webhook.schemas import WebflowPaymentPayload


def build_fastapi_app(cache: CacheStore, queue: WriteQueue) -> FastAPI:
    app = FastAPI()

    @app.post("/webhook/webflow")
    async def webflow_webhook(payload: WebflowPaymentPayload):
        participant = await handle_webflow_payment(payload, cache, queue)
        return {"ok": True, "participant_id": participant.participant_id}

    @app.get("/health")
    async def health():
        return {"ok": True, "cache_synced_at": cache.last_synced_at}

    return app