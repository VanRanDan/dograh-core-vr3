"""
Call-start enforcement gate (provisioned-only).

Checks org billing status and atomically reserves meters before a call starts.
Provisioned-only: orgs without lago_customer_id are never gated.
"""
from loguru import logger

from api.db import db_client
from api.services.billing.estimator import estimate_units
from api.services.quota_service import QuotaCheckResult

_METERS = [
    ("voice_minutes", "bundle_exhausted_minutes"),
    ("ai_cost_cents", "bundle_exhausted_ai_cost"),
]


async def check_and_reserve(org_id: int, workflow_id: int | None = None) -> QuotaCheckResult:
    """
    Provisioned-only enforcement gate.

    1. Un-provisioned org (no lago_customer_id) → always allow.
    2. billing_suspended → deny immediately.
    3. Reserve both meters atomically; rollback on first failure.
    """
    org = await db_client.get_organization_by_id(org_id)
    if org is None or org.lago_customer_id is None:
        return QuotaCheckResult(has_quota=True)

    if org.billing_suspended:
        return QuotaCheckResult(
            has_quota=False,
            error_code="account_suspended",
            error_message="Account suspended for billing. Contact support.",
        )

    est_minutes, est_cents = estimate_units(workflow_id)
    ests = {"voice_minutes": est_minutes, "ai_cost_cents": est_cents}

    async with db_client.async_session() as session:
        cycle = await db_client._get_or_create_current_cycle_impl(org_id, session, commit=False)

        for meter, code in _METERS:
            allowance = getattr(cycle, f"allowance_{meter}")

            if org.overage_policy == "allow":
                limit = float("inf")
            elif org.overage_policy == "cap":
                limit = allowance * (org.overage_cap_pct or 100) / 100
            else:  # block (default)
                limit = allowance

            ok = await db_client.reserve_meter(
                session, cycle_id=cycle.id, meter=meter, est=ests[meter], limit=limit
            )
            if not ok:
                await session.rollback()
                logger.info(
                    f"Billing gate DENY org={org_id} meter={meter} policy={org.overage_policy}"
                )
                return QuotaCheckResult(
                    has_quota=False,
                    error_code=code,
                    error_message=f"Your {meter.replace('_', ' ')} bundle is exhausted.",
                )

        await session.commit()

    return QuotaCheckResult(has_quota=True)
