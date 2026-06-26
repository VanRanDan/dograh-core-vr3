"""
Settlement emitter: resolve org, gate on provisioning, delegate to billing client.

Called after workflow run cost is calculated. Applies the reserve-to-actual
delta and enqueues outbox events for Lago. Billing failure never raises.
"""
from loguru import logger

from api.db import db_client
from api.services.billing.meters import compute_meters


async def emit_settlement(workflow_run, cost_info: dict | None) -> None:
    """Settle run usage and enqueue billing events.

    No-op when:
      - cost_info is falsy
      - org_id cannot be resolved from the workflow run
      - org is not found or has no lago_customer_id (not provisioned)

    Billing failure is caught and logged — never propagates.
    """
    if not cost_info:
        return

    org_id = _resolve_org_id(workflow_run)
    if org_id is None:
        return

    # Provisioned-only billing gate
    org = await db_client.get_organization_by_id(org_id)
    if org is None or getattr(org, "lago_customer_id", None) is None:
        return

    voice_minutes, ai_cost_cents = compute_meters(cost_info)
    reservation = cost_info.get("billing_reservation") or {}
    est_minutes = float(reservation.get("est_minutes", 0))
    est_cents = float(reservation.get("est_cents", 0))

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


def _resolve_org_id(workflow_run) -> int | None:
    wf = getattr(workflow_run, "workflow", None)
    org_id = getattr(wf, "organization_id", None)
    if org_id is None and wf is not None:
        user = getattr(wf, "user", None)
        if user is not None:
            org_id = getattr(user, "selected_organization_id", None)
    return org_id
