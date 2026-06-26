from api.db import db_client
from api.db.models import OrganizationModel, OrganizationUsageCycleModel
from api.services.billing.lago_client import lago_client


async def provision_org(
    org_id: int,
    plan_code: str,
    *,
    overage_policy: str = "block",
    overage_cap_pct: int | None = None,
) -> None:
    external_id = f"org-{org_id}"
    await lago_client.upsert_customer(external_id, name=external_id)
    await lago_client.create_subscription(
        external_customer_id=external_id,
        external_id=external_id,
        plan_code=plan_code,
    )
    plan = await lago_client.get_plan(plan_code)

    async with db_client.async_session() as s:
        org = await s.get(OrganizationModel, org_id)
        org.lago_customer_id = external_id
        org.billing_plan_code = plan_code
        org.included_voice_minutes = plan["voice_minutes_free"]
        org.included_ai_cost_cents = plan["ai_cost_cents_free"]
        # backfill allowance on current cycle BEFORE applying policy
        cycle = await db_client._get_or_create_current_cycle_impl(org_id, s, commit=False)
        cycle.allowance_voice_minutes = plan["voice_minutes_free"]
        cycle.allowance_ai_cost_cents = plan["ai_cost_cents_free"]
        # policy applied AFTER allowance is set
        org.overage_policy = overage_policy
        org.overage_cap_pct = overage_cap_pct
        await s.commit()
