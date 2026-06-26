"""
Billing outbox drainer — arq cron task.

Runs every ~20 seconds (second={0, 20, 40}) to drain pending billing events
from the transactional outbox to Lago.

Shadow-mode rollout: events flow to Lago; the call gate is unchanged.
"""

from loguru import logger

from api.db import db_client
from api.services.billing.lago_client import lago_client
from api.services.billing.reconciliation import reconcile_billing  # noqa: F401


async def drain_billing_outbox(ctx) -> int:
    """
    Drain pending billing outbox rows to Lago.

    For each pending row:
      1. Fetch the workflow run to resolve organization_id (no lazy-load;
         each row opens its own session via the facade methods).
      2. Fetch the workflow to get organization_id.
      3. Derive subscription id as f"org-{organization_id}".
      4. Send the event to Lago.
      5. Mark the row sent on success, failed on any exception.

    Returns the number of events successfully sent.
    """
    pending = await db_client.fetch_pending_billing_events(limit=200)
    sent_count = 0

    for row in pending:
        try:
            run = await db_client.get_workflow_run_by_id(row.workflow_run_id)
            if run is None:
                await db_client.mark_billing_event_failed(
                    row.id, f"workflow run {row.workflow_run_id} not found"
                )
                continue

            wf = await db_client.get_workflow_by_id(run.workflow_id)
            if wf is None:
                await db_client.mark_billing_event_failed(
                    row.id, f"workflow {run.workflow_id} not found"
                )
                continue

            org_id = wf.organization_id
            if not org_id:
                await db_client.mark_billing_event_failed(
                    row.id, "no organization for run"
                )
                continue

            sub_id = f"org-{org_id}"

            await lago_client.send_event(
                transaction_id=row.transaction_id,
                external_subscription_id=sub_id,
                code=row.metric_code,
                value=row.value,
            )
            await db_client.mark_billing_event_sent(row.id)
            sent_count += 1

        except Exception as e:
            logger.error(
                f"Outbox drain failed for transaction {row.transaction_id}: {e}"
            )
            await db_client.mark_billing_event_failed(row.id, str(e))

    if pending:
        logger.info(
            f"Billing outbox drain complete: {sent_count}/{len(pending)} sent"
        )

    return sent_count
