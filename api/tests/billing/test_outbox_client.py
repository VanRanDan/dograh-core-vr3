"""
TDD tests for BillingOutboxClient mixin.

Tests the transactional outbox client methods registered on db_client:
  - enqueue_billing_event: idempotent insert (ON CONFLICT DO NOTHING on transaction_id)
  - fetch_pending_billing_events: returns pending rows ordered by created_at
  - mark_billing_event_sent: sets status="sent", sent_at
  - mark_billing_event_failed: sets status="failed", increments attempts, stores error

These are DB integration tests using the transactional test session (savepoint isolation).
"""

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
async def seeded_run(db_session, async_session):
    """
    Seed a workflow_runs row and return its id.

    Pattern mirrors test_workflow_versioning.py: direct ORM inserts via async_session
    for org/user, then facade methods via db_session for workflow + run.
    """
    import uuid

    uid = uuid.uuid4().hex[:8]

    # Create org + user directly via ORM (async_session is the raw isolated session)
    org = OrganizationModel(provider_id=f"test-org-outbox-{uid}")
    async_session.add(org)
    await async_session.flush()

    user = UserModel(
        provider_id=f"test-user-outbox-{uid}",
        selected_organization_id=org.id,
    )
    async_session.add(user)
    await async_session.flush()

    # Create workflow via facade (uses same underlying session via db_session patch)
    workflow = await db_session.create_workflow(
        name="Outbox Test Workflow",
        workflow_definition=GRAPH_MINIMAL,
        user_id=user.id,
        organization_id=org.id,
    )

    # Create run via facade
    run = await db_session.create_workflow_run(
        name="Outbox Test Run",
        workflow_id=workflow.id,
        mode="webrtc",
        user_id=user.id,
    )

    return run.id


class TestEnqueueBillingEvent:
    async def test_enqueue_inserts_row(self, db_session, async_session, seeded_run):
        """enqueue_billing_event should insert a pending row."""
        await db_session.enqueue_billing_event(
            async_session,
            workflow_run_id=seeded_run,
            metric_code="voice_minutes",
            value=1.5,
        )
        await async_session.flush()

        result = await async_session.execute(
            select(BillingEventOutboxModel).where(
                BillingEventOutboxModel.workflow_run_id == seeded_run,
                BillingEventOutboxModel.metric_code == "voice_minutes",
            )
        )
        rows = list(result.scalars().all())
        assert len(rows) == 1
        row = rows[0]
        assert row.value == 1.5
        assert row.status == "pending"
        assert row.transaction_id == f"{seeded_run}:voice_minutes:0"

    async def test_enqueue_is_idempotent(self, db_session, async_session, seeded_run):
        """Enqueuing the same (run, metric, recompute_seq) twice yields exactly one row."""
        await db_session.enqueue_billing_event(
            async_session,
            workflow_run_id=seeded_run,
            metric_code="voice_minutes",
            value=1.5,
        )
        await async_session.flush()

        # Second enqueue with identical key — should do nothing
        await db_session.enqueue_billing_event(
            async_session,
            workflow_run_id=seeded_run,
            metric_code="voice_minutes",
            value=1.5,
        )
        await async_session.flush()

        result = await async_session.execute(
            select(BillingEventOutboxModel).where(
                BillingEventOutboxModel.workflow_run_id == seeded_run,
                BillingEventOutboxModel.metric_code == "voice_minutes",
            )
        )
        rows = list(result.scalars().all())
        assert len(rows) == 1
        assert rows[0].transaction_id == f"{seeded_run}:voice_minutes:0"

    async def test_enqueue_different_recompute_seq_creates_separate_row(
        self, db_session, async_session, seeded_run
    ):
        """Different recompute_seq produces a distinct row (different transaction_id)."""
        await db_session.enqueue_billing_event(
            async_session,
            workflow_run_id=seeded_run,
            metric_code="voice_minutes",
            value=1.5,
            recompute_seq=0,
        )
        await db_session.enqueue_billing_event(
            async_session,
            workflow_run_id=seeded_run,
            metric_code="voice_minutes",
            value=2.0,
            recompute_seq=1,
        )
        await async_session.flush()

        result = await async_session.execute(
            select(BillingEventOutboxModel).where(
                BillingEventOutboxModel.workflow_run_id == seeded_run,
                BillingEventOutboxModel.metric_code == "voice_minutes",
            )
        )
        rows = list(result.scalars().all())
        assert len(rows) == 2
        txn_ids = {r.transaction_id for r in rows}
        assert txn_ids == {
            f"{seeded_run}:voice_minutes:0",
            f"{seeded_run}:voice_minutes:1",
        }


class TestFetchPendingBillingEvents:
    async def test_fetch_pending_returns_pending_rows(
        self, db_session, async_session, seeded_run
    ):
        """fetch_pending_billing_events returns rows with status=pending."""
        await db_session.enqueue_billing_event(
            async_session,
            workflow_run_id=seeded_run,
            metric_code="ai_cost_cents",
            value=42.0,
        )
        await async_session.flush()

        pending = await db_session.fetch_pending_billing_events()
        run_rows = [p for p in pending if p.workflow_run_id == seeded_run]
        assert len(run_rows) == 1
        assert run_rows[0].status == "pending"

    async def test_fetch_pending_respects_limit(
        self, db_session, async_session, seeded_run
    ):
        """fetch_pending_billing_events respects the limit parameter."""
        for i in range(5):
            await db_session.enqueue_billing_event(
                async_session,
                workflow_run_id=seeded_run,
                metric_code=f"metric_{i}",
                value=float(i),
            )
        await async_session.flush()

        pending = await db_session.fetch_pending_billing_events(limit=3)
        # We seeded 5 rows but the limit is 3; total returned <= 3
        assert len(pending) <= 3


class TestMarkBillingEventSent:
    async def test_mark_sent_updates_status(self, db_session, async_session, seeded_run):
        """mark_billing_event_sent sets status=sent and populates sent_at."""
        await db_session.enqueue_billing_event(
            async_session,
            workflow_run_id=seeded_run,
            metric_code="voice_minutes",
            value=1.0,
        )
        await async_session.flush()

        result = await async_session.execute(
            select(BillingEventOutboxModel).where(
                BillingEventOutboxModel.workflow_run_id == seeded_run,
            )
        )
        row = result.scalars().first()
        assert row is not None

        await db_session.mark_billing_event_sent(row.id)

        # Re-fetch to verify update
        await async_session.refresh(row)
        assert row.status == "sent"
        assert row.sent_at is not None


class TestMarkBillingEventFailed:
    async def test_mark_failed_updates_status_and_increments_attempts(
        self, db_session, async_session, seeded_run
    ):
        """mark_billing_event_failed sets status=failed, increments attempts, stores error."""
        await db_session.enqueue_billing_event(
            async_session,
            workflow_run_id=seeded_run,
            metric_code="voice_minutes",
            value=1.0,
        )
        await async_session.flush()

        result = await async_session.execute(
            select(BillingEventOutboxModel).where(
                BillingEventOutboxModel.workflow_run_id == seeded_run,
            )
        )
        row = result.scalars().first()
        assert row is not None
        initial_attempts = row.attempts

        await db_session.mark_billing_event_failed(row.id, error="Connection timeout")

        await async_session.refresh(row)
        assert row.status == "failed"
        assert row.attempts == initial_attempts + 1
        assert row.last_error == "Connection timeout"

    async def test_mark_failed_truncates_long_error(
        self, db_session, async_session, seeded_run
    ):
        """mark_billing_event_failed truncates error strings to 1000 chars."""
        await db_session.enqueue_billing_event(
            async_session,
            workflow_run_id=seeded_run,
            metric_code="ai_cost_cents",
            value=5.0,
        )
        await async_session.flush()

        result = await async_session.execute(
            select(BillingEventOutboxModel).where(
                BillingEventOutboxModel.workflow_run_id == seeded_run,
                BillingEventOutboxModel.metric_code == "ai_cost_cents",
            )
        )
        row = result.scalars().first()

        long_error = "x" * 2000
        await db_session.mark_billing_event_failed(row.id, error=long_error)

        await async_session.refresh(row)
        assert len(row.last_error) == 1000
