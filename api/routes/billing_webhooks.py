"""Lago → Dograh billing webhook handler.

Suspends or resumes an organisation's billing based on Lago payment/subscription events.
"""
import hashlib
import hmac

from fastapi import APIRouter, Header, HTTPException, Request
from loguru import logger
from sqlalchemy import update

from api.constants import LAGO_WEBHOOK_SECRET
from api.db import db_client
from api.db.models import OrganizationModel

router = APIRouter(prefix="/billing/webhooks", tags=["billing"])

_SUSPEND_TYPES = {"invoice.payment_failure", "subscription.terminated"}
_RESUME_TYPES = {"invoice.payment_success"}


def _verify(body: bytes, signature: str | None) -> bool:
    """Return True if signature is valid (or if no secret is configured)."""
    if not LAGO_WEBHOOK_SECRET:
        return True  # dev: skip verification
    if not signature:
        return False
    expected = hmac.new(LAGO_WEBHOOK_SECRET.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)


async def _set_suspended(external_id: str, suspended: bool) -> None:
    async with db_client.async_session() as s:
        await s.execute(
            update(OrganizationModel)
            .where(OrganizationModel.lago_customer_id == external_id)
            .values(billing_suspended=suspended)
        )
        await s.commit()


@router.post("/lago")
async def lago_webhook(
    request: Request,
    x_lago_signature: str | None = Header(default=None),
):
    """Receive Lago webhook events and update org billing_suspended state."""
    body = await request.body()

    if not _verify(body, x_lago_signature):
        raise HTTPException(status_code=401, detail="bad signature")

    payload = await request.json()
    wtype = payload.get("webhook_type", "")

    # Extract external_id — try nested Lago structures first, then flat top-level key
    external_id = (
        payload.get("invoice", {}).get("customer", {}).get("external_id")
        or payload.get("subscription", {}).get("external_customer_id")
        or payload.get("lago_customer_id")
    )

    if not external_id:
        return {"ok": True}

    if wtype in _SUSPEND_TYPES:
        await _set_suspended(external_id, True)
        logger.info(f"Lago webhook {wtype}: suspended org {external_id}")
    elif wtype in _RESUME_TYPES:
        await _set_suspended(external_id, False)
        logger.info(f"Lago webhook {wtype}: resumed org {external_id}")
    else:
        logger.debug(f"Lago webhook {wtype}: no-op for {external_id}")

    return {"ok": True}
