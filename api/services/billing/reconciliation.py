"""
Reconciliation cron: sync local usage cycle to Lago's authoritative totals
and sync plan allowances.

Open-period rule (the only rule implemented here):
  local = max(local, lago_total)

Rationale: locally we may have buffered/pending events not yet acked by Lago,
so we never drop local below what Lago reports, but we also never drop Lago's
number below our local accumulation.

Closed-period hard-overwrite is explicitly deferred to a separate
cycle-rollover pass (Plan 2). When a cycle closes, _get_or_create_current_cycle_impl
returns a new cycle, and the closed one is never touched by this job — its final
Lago reconciliation will be handled at rollover time.
"""

from loguru import logger
from sqlalchemy import select

from api.db import db_client
from api.db.models import OrganizationModel, OrganizationUsageCycleModel
from api.services.billing.lago_client import lago_client


async def reconcile_org(org) -> None:
    """Reconcile a single org's open usage cycle with Lago and sync plan allowances."""
    sub_id = f"org-{org.id}"
    lago_usage = await lago_client.get_current_usage(sub_id)
    plan = await lago_client.get_plan(org.billing_plan_code) if org.billing_plan_code else None

    async with db_client.async_session() as s:
        # Get or create the current (open) cycle
        cycle = await db_client._get_or_create_current_cycle_impl(org.id, s, commit=False)

        # Row-lock the cycle for atomic update
        locked = (
            await s.execute(
                select(OrganizationUsageCycleModel)
                .where(OrganizationUsageCycleModel.id == cycle.id)
                .with_for_update()
            )
        ).scalar_one()

        # Open-period: never reduce below local pending/reserved values
        locked.used_voice_minutes = max(locked.used_voice_minutes, lago_usage["voice_minutes"])
        locked.used_ai_cost_cents = max(locked.used_ai_cost_cents, lago_usage["ai_cost_cents"])

        if plan:
            # Sync org-level included units from the authoritative plan definition
            org_row = await s.get(OrganizationModel, org.id)
            org_row.included_voice_minutes = plan["voice_minutes_free"]
            org_row.included_ai_cost_cents = plan["ai_cost_cents_free"]

            # Set cycle allowance from plan if not already set
            if locked.allowance_voice_minutes == 0:
                locked.allowance_voice_minutes = plan["voice_minutes_free"]
            if locked.allowance_ai_cost_cents == 0:
                locked.allowance_ai_cost_cents = plan["ai_cost_cents_free"]

        await s.commit()


async def reconcile_billing(ctx) -> int:
    """
    Reconcile all provisioned orgs (those with a Lago customer id).

    Returns the number of orgs successfully reconciled.
    """
    async with db_client.async_session() as s:
        orgs = (
            await s.execute(
                select(OrganizationModel).where(OrganizationModel.lago_customer_id.isnot(None))
            )
        ).scalars().all()

    n = 0
    for org in orgs:
        try:
            await reconcile_org(org)
            n += 1
        except Exception as e:
            logger.error(f"Reconcile failed org={org.id}: {e}")
    return n
