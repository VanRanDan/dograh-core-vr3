"""
TDD tests for the billing outbox drainer arq cron task.

Tests that drain_billing_outbox:
  - fetches pending outbox rows
  - resolves org -> subscription id via get_workflow_run_by_id + get_workflow_by_id
  - calls lago_client.send_event with correct args
  - marks rows as sent on success
  - marks rows as failed (and continues) on lago error
"""

import uuid

import pytest
from sqlalchemy import select

from api.db.models import BillingEventOutboxModel, OrganizationModel, UserModel

GRAPH_MINIMAL = {
    "nodes": [
        {"id": "1", "type": "startCall", "data": {"name": "Start", "prompt": "Hi"}},
        {"id": "2", "type": "endCall", "data": {"name": "End", "prompt": "Bye"}},
    ],
    "edges": [{"id": "e1", "source": "1", "target": "2", "data": {"label": "End"}}],
}


@pytest.fixture
async def seeded_outbox(db_session, async_session):
    """
    Seed org + user + workflow + run + pending outbox row.

    Returns a dict with: org_id, run_id, org.
    Pattern mirrors test_outbox_client.py seeded_run fixture.
    """
    uid = uuid.uuid4().hex[:8]

    org = OrganizationModel(provider_id=f"test-org-drainer-{uid}")
    async_session.add(org)
    await async_session.flush()

    user = UserModel(
        provider_id=f"test-user-drainer-{uid}",
        selected_organization_id=org.id,
    )
    async_session.add(user)
    await async_session.flush()

    workflow = await db_session.create_workflow(
        name="Drainer Test Workflow",
        workflow_definition=GRAPH_MINIMAL,
        user_id=user.id,
        organization_id=org.id,
    )

    run = await db_session.create_workflow_run(
        name="Drainer Test Run",
        workflow_id=workflow.id,
        mode="webrtc",
        user_id=user.id,
    )

    await db_session.enqueue_billing_event(
        async_session,
        workflow_run_id=run.id,
        metric_code="voice_minutes",
        value=1.5,
    )
    await async_session.flush()

    return {"org_id": org.id, "run_id": run.id, "org": org}


async def test_drain_sends_pending_and_marks_sent(
    monkeypatch, db_session, async_session, seeded_outbox
):
    """
    drain_billing_outbox should:
    - call lago_client.send_event with correct subscription id and metric code
    - return a count >= 1
    - mark the outbox row as sent
    """
    from api.tasks.billing_tasks import drain_billing_outbox

    sent = []

    async def fake_send(**kw):
        sent.append(kw)

    monkeypatch.setattr("api.tasks.billing_tasks.lago_client.send_event", fake_send)

    count = await drain_billing_outbox({})

    assert count >= 1
    assert any(s["code"] == "voice_minutes" for s in sent)

    org_id = seeded_outbox["org_id"]
    run_id = seeded_outbox["run_id"]
    expected_sub_id = f"org-{org_id}"
    assert any(s["external_subscription_id"] == expected_sub_id for s in sent)

    # Verify the row is now marked sent (not pending)
    pending_after = await db_session.fetch_pending_billing_events()
    run_rows = [p for p in pending_after if p.workflow_run_id == run_id]
    assert len(run_rows) == 0, "Row should be marked sent, not pending"

    # Verify via direct ORM query
    result = await async_session.execute(
        select(BillingEventOutboxModel).where(
            BillingEventOutboxModel.workflow_run_id == run_id
        )
    )
    row = result.scalars().first()
    assert row is not None
    assert row.status == "sent"


async def test_drain_marks_failed_on_lago_error_and_continues(
    monkeypatch, db_session, async_session, seeded_outbox
):
    """
    If lago_client.send_event raises, drain_billing_outbox should:
    - mark the row as failed
    - not raise (continue processing remaining rows)
    - return count = 0 (nothing sent)
    """
    from api.tasks.billing_tasks import drain_billing_outbox

    async def fake_send_error(**kw):
        raise RuntimeError("Lago unavailable")

    monkeypatch.setattr(
        "api.tasks.billing_tasks.lago_client.send_event", fake_send_error
    )

    count = await drain_billing_outbox({})

    # Nothing should have been sent
    assert count == 0

    run_id = seeded_outbox["run_id"]
    result = await async_session.execute(
        select(BillingEventOutboxModel).where(
            BillingEventOutboxModel.workflow_run_id == run_id
        )
    )
    row = result.scalars().first()
    assert row is not None
    assert row.status == "failed"
    assert "Lago unavailable" in (row.last_error or "")
