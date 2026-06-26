"""
TDD tests for the reconciliation cron (Task 11).

Tests that reconcile_org:
  - Never lowers a local meter below its current value (max rule for open period)
  - Raises a local meter to Lago's value when Lago is higher
  - Syncs org.included_* and cycle.allowance_* from plan when billing_plan_code is set

Tests that reconcile_billing:
  - Only processes orgs with lago_customer_id IS NOT NULL
  - Continues on per-org errors
  - Returns count of successes
"""

import uuid

import pytest
from sqlalchemy import select, update

from api.db.models import OrganizationModel, OrganizationUsageCycleModel


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _fetch_cycle(org_id: int, async_session) -> OrganizationUsageCycleModel:
    """Return the current usage cycle row for the given org."""
    result = await async_session.execute(
        select(OrganizationUsageCycleModel).where(
            OrganizationUsageCycleModel.organization_id == org_id
        )
    )
    return result.scalars().first()


async def _fetch_org(org_id: int, async_session) -> OrganizationModel:
    result = await async_session.execute(
        select(OrganizationModel).where(OrganizationModel.id == org_id)
    )
    return result.scalar_one()


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
async def provisioned_org(db_session, async_session):
    """
    Seed an org that has a Lago customer id and billing plan code,
    plus an open usage cycle with known used_* values.

    Returns (org, cycle).
    """
    uid = uuid.uuid4().hex[:8]

    org = OrganizationModel(
        provider_id=f"test-recon-{uid}",
        lago_customer_id=f"org-recon-{uid}",
        billing_plan_code="growth-test",
    )
    async_session.add(org)
    await async_session.flush()

    # Let _get_or_create_current_cycle_impl create the cycle with the correct
    # period_start/end for today, then update used_* to our desired initial state.
    cycle = await db_session.get_or_create_current_cycle(org.id, session=async_session)

    await async_session.execute(
        update(OrganizationUsageCycleModel)
        .where(OrganizationUsageCycleModel.id == cycle.id)
        .values(
            used_voice_minutes=5.0,
            used_ai_cost_cents=100.0,
            allowance_voice_minutes=0,
            allowance_ai_cost_cents=0,
        )
    )
    await async_session.flush()
    await async_session.refresh(cycle)

    return org, cycle


# ---------------------------------------------------------------------------
# RED → GREEN: open-period max rule
# ---------------------------------------------------------------------------


async def test_open_period_uses_max(monkeypatch, db_session, async_session, provisioned_org):
    """
    When Lago reports a lower value for one meter and a higher value for another:
    - The lower-Lago meter is NOT reduced (local wins)
    - The higher-Lago meter IS raised (Lago wins)
    """
    from api.services.billing.reconciliation import reconcile_org

    org, _cycle = provisioned_org

    # Lago returns: voice_minutes=3.0 (lower than local 5.0)
    #               ai_cost_cents=150.0 (higher than local 100.0)
    async def fake_usage(sub_id):
        return {"voice_minutes": 3.0, "ai_cost_cents": 150.0}

    async def fake_plan(code):
        return {"voice_minutes_free": 1000, "ai_cost_cents_free": 50000}

    monkeypatch.setattr(
        "api.services.billing.reconciliation.lago_client.get_current_usage", fake_usage
    )
    monkeypatch.setattr(
        "api.services.billing.reconciliation.lago_client.get_plan", fake_plan
    )

    await reconcile_org(org)

    cycle = await _fetch_cycle(org.id, async_session)
    # max(5.0, 3.0) = 5.0 — not lowered
    assert cycle.used_voice_minutes == 5.0, "Local voice_minutes must not be reduced"
    # max(100.0, 150.0) = 150.0 — raised to Lago
    assert cycle.used_ai_cost_cents == 150.0, "ai_cost_cents must rise to Lago value"


async def test_allowances_synced_from_plan(monkeypatch, db_session, async_session, provisioned_org):
    """
    When a billing_plan_code is set, reconcile_org should sync:
      - org.included_voice_minutes
      - org.included_ai_cost_cents
      - cycle.allowance_voice_minutes  (only if currently 0)
      - cycle.allowance_ai_cost_cents  (only if currently 0)
    """
    from api.services.billing.reconciliation import reconcile_org

    org, _cycle = provisioned_org

    async def fake_usage(sub_id):
        return {"voice_minutes": 0.0, "ai_cost_cents": 0.0}

    async def fake_plan(code):
        return {"voice_minutes_free": 1000, "ai_cost_cents_free": 50000}

    monkeypatch.setattr(
        "api.services.billing.reconciliation.lago_client.get_current_usage", fake_usage
    )
    monkeypatch.setattr(
        "api.services.billing.reconciliation.lago_client.get_plan", fake_plan
    )

    await reconcile_org(org)

    cycle = await _fetch_cycle(org.id, async_session)
    refreshed_org = await _fetch_org(org.id, async_session)

    assert cycle.allowance_voice_minutes == 1000
    assert cycle.allowance_ai_cost_cents == 50000
    assert refreshed_org.included_voice_minutes == 1000
    assert refreshed_org.included_ai_cost_cents == 50000


async def test_allowance_not_overwritten_when_already_set(
    monkeypatch, db_session, async_session, provisioned_org
):
    """
    If cycle.allowance_* is already non-zero, reconcile_org must NOT overwrite it
    (the field is only initialised, not continually re-synced from plan).
    """
    from api.services.billing.reconciliation import reconcile_org

    org, cycle = provisioned_org

    # Pre-set a non-zero allowance
    await async_session.execute(
        update(OrganizationUsageCycleModel)
        .where(OrganizationUsageCycleModel.id == cycle.id)
        .values(allowance_voice_minutes=500, allowance_ai_cost_cents=9999)
    )
    await async_session.flush()

    async def fake_usage(sub_id):
        return {"voice_minutes": 0.0, "ai_cost_cents": 0.0}

    async def fake_plan(code):
        return {"voice_minutes_free": 1000, "ai_cost_cents_free": 50000}

    monkeypatch.setattr(
        "api.services.billing.reconciliation.lago_client.get_current_usage", fake_usage
    )
    monkeypatch.setattr(
        "api.services.billing.reconciliation.lago_client.get_plan", fake_plan
    )

    await reconcile_org(org)

    refreshed = await _fetch_cycle(org.id, async_session)
    # Should remain at pre-set values, not overwritten by plan
    assert refreshed.allowance_voice_minutes == 500
    assert refreshed.allowance_ai_cost_cents == 9999


async def test_no_plan_skips_allowance_sync(
    monkeypatch, db_session, async_session
):
    """
    When billing_plan_code is None, reconcile_org must not call get_plan
    and must leave allowances untouched.
    """
    from api.services.billing.reconciliation import reconcile_org

    uid = uuid.uuid4().hex[:8]
    org = OrganizationModel(
        provider_id=f"test-recon-noplan-{uid}",
        lago_customer_id=f"org-noplan-{uid}",
        billing_plan_code=None,
    )
    async_session.add(org)
    await async_session.flush()

    cycle = await db_session.get_or_create_current_cycle(org.id, session=async_session)
    await async_session.flush()

    plan_called = []

    async def fake_usage(sub_id):
        return {"voice_minutes": 10.0, "ai_cost_cents": 200.0}

    async def fake_plan(code):
        plan_called.append(code)
        return {"voice_minutes_free": 9999, "ai_cost_cents_free": 99999}

    monkeypatch.setattr(
        "api.services.billing.reconciliation.lago_client.get_current_usage", fake_usage
    )
    monkeypatch.setattr(
        "api.services.billing.reconciliation.lago_client.get_plan", fake_plan
    )

    await reconcile_org(org)

    assert plan_called == [], "get_plan must not be called when billing_plan_code is None"
    refreshed = await _fetch_cycle(org.id, async_session)
    assert refreshed.used_voice_minutes == 10.0
    assert refreshed.used_ai_cost_cents == 200.0


# ---------------------------------------------------------------------------
# reconcile_billing integration
# ---------------------------------------------------------------------------


async def test_reconcile_billing_counts_successes(
    monkeypatch, db_session, async_session, provisioned_org
):
    """reconcile_billing returns count of successfully reconciled orgs."""
    from api.services.billing.reconciliation import reconcile_billing

    org, _ = provisioned_org

    async def fake_usage(sub_id):
        return {"voice_minutes": 1.0, "ai_cost_cents": 1.0}

    async def fake_plan(code):
        return {"voice_minutes_free": 100, "ai_cost_cents_free": 100}

    monkeypatch.setattr(
        "api.services.billing.reconciliation.lago_client.get_current_usage", fake_usage
    )
    monkeypatch.setattr(
        "api.services.billing.reconciliation.lago_client.get_plan", fake_plan
    )

    count = await reconcile_billing({})
    assert count >= 1


async def test_reconcile_billing_skips_orgs_without_lago_id(
    monkeypatch, db_session, async_session
):
    """reconcile_billing must not process orgs with lago_customer_id IS NULL."""
    from api.services.billing.reconciliation import reconcile_billing

    uid = uuid.uuid4().hex[:8]
    org = OrganizationModel(
        provider_id=f"test-recon-nolago-{uid}",
        lago_customer_id=None,
    )
    async_session.add(org)
    await async_session.flush()

    usage_called = []

    async def fake_usage(sub_id):
        usage_called.append(sub_id)
        return {"voice_minutes": 0.0, "ai_cost_cents": 0.0}

    async def fake_plan(code):
        return {"voice_minutes_free": 0, "ai_cost_cents_free": 0}

    monkeypatch.setattr(
        "api.services.billing.reconciliation.lago_client.get_current_usage", fake_usage
    )
    monkeypatch.setattr(
        "api.services.billing.reconciliation.lago_client.get_plan", fake_plan
    )

    await reconcile_billing({})

    # The no-lago org must never appear in the calls
    assert f"org-{org.id}" not in usage_called


async def test_reconcile_billing_continues_on_error(
    monkeypatch, db_session, async_session, provisioned_org
):
    """reconcile_billing must not raise when reconcile_org fails for an org."""
    from api.services.billing.reconciliation import reconcile_billing

    org, _ = provisioned_org

    async def fake_usage_error(sub_id):
        raise RuntimeError("Lago down")

    monkeypatch.setattr(
        "api.services.billing.reconciliation.lago_client.get_current_usage", fake_usage_error
    )

    # Must not raise; returns 0 successes
    count = await reconcile_billing({})
    assert count == 0
