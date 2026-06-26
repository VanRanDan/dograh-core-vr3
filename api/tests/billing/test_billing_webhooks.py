"""TDD tests for Lago webhook endpoint (suspend/resume)."""
import hashlib
import hmac
import json
import uuid

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from api.db.models import OrganizationModel

API_PREFIX = "/api/v1"


def _make_webhook_app():
    """Build a minimal FastAPI app containing only the billing webhook router.

    This avoids importing api.app (which pulls in azure/pipecat dependencies
    that are not installed in the test environment).
    """
    from fastapi import FastAPI
    from api.routes.billing_webhooks import router as billing_webhooks_router

    app = FastAPI()
    app.include_router(billing_webhooks_router, prefix=API_PREFIX)
    return app


@pytest.fixture
async def billing_client(db_session):
    app = _make_webhook_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        yield client


@pytest.fixture
async def seed_org(async_session):
    """Factory: seed an OrganizationModel and flush. Returns the ORM object."""
    async def _seed(**kwargs):
        org = OrganizationModel(provider_id=f"bwh-{uuid.uuid4().hex[:8]}", **kwargs)
        async_session.add(org)
        await async_session.flush()
        return org
    return _seed


def _make_sig(secret: str, body: bytes) -> str:
    return hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


@pytest.mark.asyncio
async def test_payment_failure_suspends(billing_client, seed_org, async_session, monkeypatch):
    monkeypatch.setattr("api.routes.billing_webhooks.LAGO_WEBHOOK_SECRET", "testsecret")

    org = await seed_org(lago_customer_id="org-9", billing_suspended=False)

    payload = {"webhook_type": "invoice.payment_failure", "invoice": {"customer": {"external_id": "org-9"}}}
    body = json.dumps(payload).encode()
    sig = _make_sig("testsecret", body)

    response = await billing_client.post(
        API_PREFIX + "/billing/webhooks/lago",
        content=body,
        headers={"Content-Type": "application/json", "X-Lago-Signature": sig},
    )

    assert response.status_code == 200

    result = await async_session.execute(
        select(OrganizationModel).where(OrganizationModel.id == org.id)
    )
    updated_org = result.scalar_one()
    assert updated_org.billing_suspended is True


@pytest.mark.asyncio
async def test_payment_success_resumes(billing_client, seed_org, async_session, monkeypatch):
    monkeypatch.setattr("api.routes.billing_webhooks.LAGO_WEBHOOK_SECRET", "testsecret")

    org = await seed_org(lago_customer_id="org-10", billing_suspended=True)

    payload = {"webhook_type": "invoice.payment_success", "invoice": {"customer": {"external_id": "org-10"}}}
    body = json.dumps(payload).encode()
    sig = _make_sig("testsecret", body)

    response = await billing_client.post(
        API_PREFIX + "/billing/webhooks/lago",
        content=body,
        headers={"Content-Type": "application/json", "X-Lago-Signature": sig},
    )

    assert response.status_code == 200

    result = await async_session.execute(
        select(OrganizationModel).where(OrganizationModel.id == org.id)
    )
    updated_org = result.scalar_one()
    assert updated_org.billing_suspended is False


@pytest.mark.asyncio
async def test_subscription_terminated_suspends(billing_client, seed_org, async_session, monkeypatch):
    monkeypatch.setattr("api.routes.billing_webhooks.LAGO_WEBHOOK_SECRET", "testsecret")

    org = await seed_org(lago_customer_id="org-12", billing_suspended=False)

    payload = {"webhook_type": "subscription.terminated", "subscription": {"external_customer_id": "org-12"}}
    body = json.dumps(payload).encode()
    sig = _make_sig("testsecret", body)

    response = await billing_client.post(
        API_PREFIX + "/billing/webhooks/lago",
        content=body,
        headers={"Content-Type": "application/json", "X-Lago-Signature": sig},
    )

    assert response.status_code == 200

    result = await async_session.execute(
        select(OrganizationModel).where(OrganizationModel.id == org.id)
    )
    updated_org = result.scalar_one()
    assert updated_org.billing_suspended is True


@pytest.mark.asyncio
async def test_bad_signature_returns_401(billing_client, seed_org, monkeypatch):
    monkeypatch.setattr("api.routes.billing_webhooks.LAGO_WEBHOOK_SECRET", "testsecret")

    await seed_org(lago_customer_id="org-11", billing_suspended=False)

    payload = {"webhook_type": "invoice.payment_failure", "invoice": {"customer": {"external_id": "org-11"}}}
    body = json.dumps(payload).encode()

    response = await billing_client.post(
        API_PREFIX + "/billing/webhooks/lago",
        content=body,
        headers={"Content-Type": "application/json", "X-Lago-Signature": "wrong-signature"},
    )

    assert response.status_code == 401
