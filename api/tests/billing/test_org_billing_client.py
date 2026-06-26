"""
TDD tests for OrganizationBillingClient mixin.

Tests:
  (a) settle_run_usage: applies (actual - est) delta, enqueues 2 outbox rows
  (b) reserve_meter: True + bumps when under limit; False + no bump when over limit
"""
import uuid

import pytest
from sqlalchemy import select, update

from api.db.models import (
    BillingEventOutboxModel,
    OrganizationModel,
    OrganizationUsageCycleModel,
    UserModel,
)

GRAPH_MINIMAL = {
    "nodes": [
        {"id": "1", "type": "startCall", "data": {"name": "Start", "prompt": "Hi"}},
        {"id": "2", "type": "endCall", "data": {"name": "End", "prompt": "Bye"}},
    ],
    "edges": [{"id": "e1", "source": "1", "target": "2", "data": {"label": "End"}}],
}


@pytest.fixture
async def seeded(db_session, async_session):
    """Seed org, user, workflow, run, and a billing cycle. Return (org, run, cycle)."""
    uid = uuid.uuid4().hex[:8]

    org = OrganizationModel(provider_id=f"test-billing-{uid}")
    async_session.add(org)
    await async_session.flush()

    user = UserModel(
        provider_id=f"test-billing-user-{uid}",
        selected_organization_id=org.id,
    )
    async_session.add(user)
    await async_session.flush()

    workflow = await db_session.create_workflow(
        name="Billing Test Workflow",
        workflow_definition=GRAPH_MINIMAL,
        user_id=user.id,
        organization_id=org.id,
    )

    run = await db_session.create_workflow_run(
        name="Billing Test Run",
        workflow_id=workflow.id,
        mode="webrtc",
        user_id=user.id,
    )

    # Let _get_or_create_current_cycle_impl create the cycle with correct period_start/end,
    # then update the used values and allowances to our desired initial state.
    cycle = await db_session.get_or_create_current_cycle(org.id, session=async_session)

    await async_session.execute(
        update(OrganizationUsageCycleModel)
        .where(OrganizationUsageCycleModel.id == cycle.id)
        .values(
            used_voice_minutes=1.0,
            used_ai_cost_cents=25.0,
            allowance_voice_minutes=100,
            allowance_ai_cost_cents=1000,
        )
    )
    await async_session.flush()
    await async_session.refresh(cycle)

    return org, run, cycle


class TestSettleRunUsage:
    async def test_settle_applies_delta_and_enqueues_two_outbox_rows(
        self, db_session, async_session, seeded
    ):
        """settle_run_usage applies actual-minus-est delta and enqueues 2 outbox rows."""
        org, run, cycle = seeded

        # est was 1.0 min / 25.0 cents; actual is 3.0 min / 80.0 cents
        await db_session.settle_run_usage(
            org.id,
            run_id=run.id,
            voice_minutes=3.0,
            ai_cost_cents=80.0,
            est_minutes=1.0,
            est_cents=25.0,
        )

        usage = await db_session.get_cycle_billing_usage(org.id)
        # 1.0 + (3.0 - 1.0) = 3.0
        assert usage["used_voice_minutes"] == pytest.approx(3.0)
        # 25.0 + (80.0 - 25.0) = 80.0
        assert usage["used_ai_cost_cents"] == pytest.approx(80.0)

        # Check 2 outbox events were enqueued
        result = await async_session.execute(
            select(BillingEventOutboxModel).where(
                BillingEventOutboxModel.workflow_run_id == run.id
            )
        )
        rows = list(result.scalars().all())
        codes = {r.metric_code for r in rows}
        assert codes == {"voice_minutes", "ai_cost_cents"}

    async def test_settle_clamps_to_zero_on_negative_delta(
        self, db_session, async_session, seeded
    ):
        """settle_run_usage clamps used values to >= 0 on negative deltas."""
        org, run, cycle = seeded

        # est was larger than actual → negative delta → clamp to 0
        await db_session.settle_run_usage(
            org.id,
            run_id=run.id,
            voice_minutes=0.5,
            ai_cost_cents=10.0,
            est_minutes=5.0,   # est > actual → delta negative
            est_cents=100.0,
        )

        usage = await db_session.get_cycle_billing_usage(org.id)
        assert usage["used_voice_minutes"] >= 0.0
        assert usage["used_ai_cost_cents"] >= 0.0


class TestReserveMeter:
    async def test_reserve_returns_true_and_bumps_when_under_limit(
        self, db_session, async_session, seeded
    ):
        """reserve_meter returns True and increments used when est fits under limit."""
        org, run, cycle = seeded

        # cycle has used_voice_minutes=1.0, allowance=100.0
        # reserve 2.0 more → 1.0 + 2.0 = 3.0 ≤ 100.0 → should succeed
        result = await db_session.reserve_meter(
            async_session,
            cycle_id=cycle.id,
            meter="voice_minutes",
            est=2.0,
            limit=100.0,
        )
        await async_session.flush()

        assert result is True

        # Verify the cycle was bumped
        await async_session.refresh(cycle)
        assert cycle.used_voice_minutes == pytest.approx(3.0)

    async def test_reserve_returns_false_and_no_bump_when_over_limit(
        self, db_session, async_session, seeded
    ):
        """reserve_meter returns False and does NOT bump when est exceeds limit."""
        org, run, cycle = seeded

        # cycle has used_voice_minutes=1.0; est=200.0 → 201.0 > 100.0 limit
        result = await db_session.reserve_meter(
            async_session,
            cycle_id=cycle.id,
            meter="voice_minutes",
            est=200.0,
            limit=100.0,
        )
        await async_session.flush()

        assert result is False

        # Verify the cycle was NOT bumped
        await async_session.refresh(cycle)
        assert cycle.used_voice_minutes == pytest.approx(1.0)
