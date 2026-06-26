import uuid
import pytest
from api.db.models import OrganizationModel, OrganizationUsageCycleModel
from api.services.billing.provisioning import provision_org


def _async(value):
    """Return a coroutine that resolves to value."""
    async def _coro(*args, **kwargs):
        return value
    return _coro


@pytest.mark.asyncio
async def test_provision_sets_customer_and_allowance(
    db_session, async_session, monkeypatch
):
    # Seed an un-provisioned org
    uid = uuid.uuid4().hex[:8]
    org = OrganizationModel(provider_id=f"test-provisioning-{uid}")
    async_session.add(org)
    await async_session.flush()
    org_id = org.id

    # Patch Lago calls
    monkeypatch.setattr(
        "api.services.billing.provisioning.lago_client.upsert_customer",
        _async(f"org-{org_id}"),
    )
    monkeypatch.setattr(
        "api.services.billing.provisioning.lago_client.create_subscription",
        _async(None),
    )
    monkeypatch.setattr(
        "api.services.billing.provisioning.lago_client.get_plan",
        _async({"voice_minutes_free": 1000, "ai_cost_cents_free": 50000}),
    )

    await provision_org(org_id, "growth-test", overage_policy="cap", overage_cap_pct=150)

    # Re-read org state
    await async_session.refresh(org)
    assert org.lago_customer_id == f"org-{org_id}"
    assert org.billing_plan_code == "growth-test"
    assert org.included_voice_minutes == 1000
    assert org.included_ai_cost_cents == 50000
    assert org.overage_policy == "cap"
    assert org.overage_cap_pct == 150

    # Re-read cycle
    from sqlalchemy import select, and_
    result = await async_session.execute(
        select(OrganizationUsageCycleModel).where(
            OrganizationUsageCycleModel.organization_id == org_id
        )
    )
    cycle = result.scalar_one()
    assert cycle.allowance_voice_minutes == 1000
    assert cycle.allowance_ai_cost_cents == 50000
