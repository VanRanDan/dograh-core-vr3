"""
Settlement emitter: resolve org, gate on provisioning, delegate to billing client.

Called after workflow run cost is calculated. Applies the reserve-to-actual
delta and enqueues outbox events for Lago. Billing failure never raises.
"""
from loguru import logger

from api.db import db_client
from api.services.billing.enforcement import resolve_billing_org_id
from api.services.billing.estimator import estimate_units
from api.services.billing.meters import compute_meters


async def emit_settlement(workflow_run, cost_info: dict | None) -> None:
    """Settle run usage and enqueue billing events.

    No-op when:
      - cost_info is falsy
      - org_id cannot be resolved from the workflow run
      - org is not found or has no lago_customer_id (not provisioned)

    Org resolution and the reservation estimate MUST match the call-start gate
    (`check_and_reserve`): the org comes from the shared `resolve_billing_org_id`
    helper keyed on the run's workflow_id, and the est is RECOMPUTED via
    `estimate_units(None)` — the same deterministic value the gate reserved —
    so `settle_run_usage` nets the cycle to the actual.

    Billing failure is caught and logged — never propagates.
    """
    if not cost_info:
        return

    org_id = await resolve_billing_org_id(workflow_run.workflow_id)
    if org_id is None:
        return

    # Provisioned-only billing gate
    org = await db_client.get_organization_by_id(org_id)
    if org is None or getattr(org, "lago_customer_id", None) is None:
        return

    voice_minutes, ai_cost_cents = compute_meters(cost_info)
    # Recompute the estimate the gate reserved, keyed on the same workflow_id
    # the gate used — keeps the two aligned if the estimator becomes workflow-aware.
    est_minutes, est_cents = estimate_units(workflow_run.workflow_id)

    try:
        await db_client.settle_run_usage(
            org_id,
            run_id=workflow_run.id,
            voice_minutes=voice_minutes,
            ai_cost_cents=ai_cost_cents,
            est_minutes=est_minutes,
            est_cents=est_cents,
        )
    except Exception as exc:
        logger.error(
            f"Billing settle failed for run {workflow_run.id}: {exc}"
        )
