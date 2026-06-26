"""TDD tests for check_and_reserve enforcement gate."""
import uuid
import pytest
from sqlalchemy import select, update

from api.db.models import OrganizationModel, OrganizationUsageCycleModel


def _uid() -> str:
    return uuid.uuid4().hex[:8]


@pytest.fixture
async def make_org(async_session):
    """Factory: create an org with given kwargs. Returns the ORM object."""
    async def _make(**kwargs):
        org = OrganizationModel(provider_id=f"enf-{_uid()}", **kwargs)
        async_session.add(org)
        await async_session.flush()
        return org
    return _make


@pytest.fixture
async def make_org_with_cycle(db_session, async_session):
    """Factory: create org + cycle with given allowances/used values."""
    async def _make(*, lago_customer_id, overage_policy, allowance_minutes,
                    used_minutes, allowance_cents=10_000, used_cents=0,
                    overage_cap_pct=None, billing_suspended=False):
        uid = _uid()
        org = OrganizationModel(
            provider_id=f"enf-{uid}",
            lago_customer_id=lago_customer_id,
            overage_policy=overage_policy,
            overage_cap_pct=overage_cap_pct,
            billing_suspended=billing_suspended,
        )
        async_session.add(org)
        await async_session.flush()

        cycle = await db_session.get_or_create_current_cycle(org.id, session=async_session)
        await async_session.execute(
            update(OrganizationUsageCycleModel)
            .where(OrganizationUsageCycleModel.id == cycle.id)
            .values(
                allowance_voice_minutes=allowance_minutes,
                used_voice_minutes=used_minutes,
                allowance_ai_cost_cents=allowance_cents,
                used_ai_cost_cents=used_cents,
            )
        )
        await async_session.flush()
        await async_session.refresh(cycle)
        return org, cycle
    return _make


@pytest.mark.asyncio
async def test_unprovisioned_allows(db_session, make_org):
    """Un-provisioned org (lago_customer_id=None) always passes."""
    from api.services.billing.enforcement import check_and_reserve
    org = await make_org(lago_customer_id=None)
    res = await check_and_reserve(org.id)
    assert res.has_quota is True


@pytest.mark.asyncio
async def test_suspended_denies(db_session, make_org):
    """billing_suspended=True → has_quota False, error_code=account_suspended."""
    from api.services.billing.enforcement import check_and_reserve
    org = await make_org(lago_customer_id="org-susp-1", billing_suspended=True)
    res = await check_and_reserve(org.id)
    assert res.has_quota is False
    assert res.error_code == "account_suspended"


@pytest.mark.asyncio
async def test_block_policy_denies_when_exhausted(db_session, make_org_with_cycle):
    """policy=block, used==allowance (no room for est=1.0) → denied."""
    from api.services.billing.enforcement import check_and_reserve
    org, cycle = await make_org_with_cycle(
        lago_customer_id="org-block-1",
        overage_policy="block",
        allowance_minutes=10,
        used_minutes=10,  # exactly at limit; est=1.0 would put it at 11 > 10
    )
    res = await check_and_reserve(org.id)
    assert res.has_quota is False
    assert res.error_code == "bundle_exhausted_minutes"


@pytest.mark.asyncio
async def test_allow_policy_never_blocks(db_session, async_session, make_org_with_cycle):
    """policy=allow with allowance=0 and high used → still allows (limit=inf)."""
    from api.services.billing.enforcement import check_and_reserve
    org, cycle = await make_org_with_cycle(
        lago_customer_id="org-allow-1",
        overage_policy="allow",
        allowance_minutes=0,
        used_minutes=9999,
        allowance_cents=0,
        used_cents=9999,
    )
    res = await check_and_reserve(org.id)
    assert res.has_quota is True
    # Verify reserve bumped used_voice_minutes by est (1.0)
    await async_session.refresh(cycle)
    assert cycle.used_voice_minutes >= 10000.0  # was 9999, now >=10000


@pytest.mark.asyncio
async def test_cap_policy_allows_under_ceiling(db_session, make_org_with_cycle):
    """policy=cap, overage_cap_pct=150: allowance=10, used=14 → allows (ceiling=15)."""
    from api.services.billing.enforcement import check_and_reserve
    org, cycle = await make_org_with_cycle(
        lago_customer_id="org-cap-1",
        overage_policy="cap",
        overage_cap_pct=150,
        allowance_minutes=10,
        used_minutes=14,  # 14 + 1 = 15 <= 15 ceiling
    )
    res = await check_and_reserve(org.id)
    assert res.has_quota is True


@pytest.mark.asyncio
async def test_cap_policy_denies_at_ceiling(db_session, make_org_with_cycle):
    """policy=cap, overage_cap_pct=150: allowance=10, used=15 → denies (ceiling=15)."""
    from api.services.billing.enforcement import check_and_reserve
    org, cycle = await make_org_with_cycle(
        lago_customer_id="org-cap-2",
        overage_policy="cap",
        overage_cap_pct=150,
        allowance_minutes=10,
        used_minutes=15,  # 15 + 1 = 16 > 15 ceiling
    )
    res = await check_and_reserve(org.id)
    assert res.has_quota is False
    assert res.error_code == "bundle_exhausted_minutes"
