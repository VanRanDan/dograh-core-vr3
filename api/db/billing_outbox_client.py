"""
Transactional outbox client mixin for billing events.

Provides idempotent enqueue (ON CONFLICT DO NOTHING on transaction_id),
fetch of pending events, and status-update helpers (mark_sent / mark_failed).

Registered as a mixin on DBClient in api/db/db_client.py so all access
goes through the shared facade: `from api.db import db_client`.
"""

from datetime import UTC, datetime

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert

from api.db.base_client import BaseDBClient
from api.db.models import BillingEventOutboxModel


class BillingOutboxClient(BaseDBClient):
    async def enqueue_billing_event(
        self,
        session,
        *,
        workflow_run_id: int,
        metric_code: str,
        value: float,
        recompute_seq: int = 0,
    ) -> None:
        """
        Insert a billing event into the outbox atomically with the caller's session.

        Uses ON CONFLICT DO NOTHING on the unique transaction_id so that calling
        this method twice with the same (workflow_run_id, metric_code, recompute_seq)
        is idempotent — only one row is ever written.

        The caller is responsible for committing (or flushing) the session.
        """
        transaction_id = f"{workflow_run_id}:{metric_code}:{recompute_seq}"
        stmt = (
            insert(BillingEventOutboxModel)
            .values(
                workflow_run_id=workflow_run_id,
                metric_code=metric_code,
                value=value,
                recompute_seq=recompute_seq,
                transaction_id=transaction_id,
                status="pending",
                attempts=0,
            )
            .on_conflict_do_nothing(index_elements=["transaction_id"])
        )
        await session.execute(stmt)

    async def fetch_pending_billing_events(
        self, limit: int = 100
    ) -> list[BillingEventOutboxModel]:
        """
        Return up to `limit` pending outbox events ordered by created_at ascending.

        Opens its own session (safe to call outside a caller transaction).
        """
        async with self.async_session() as session:
            result = await session.execute(
                select(BillingEventOutboxModel)
                .where(BillingEventOutboxModel.status == "pending")
                .order_by(BillingEventOutboxModel.created_at)
                .limit(limit)
            )
            return list(result.scalars().all())

    async def mark_billing_event_sent(self, outbox_id: int) -> None:
        """Mark a single outbox row as sent and record the sent_at timestamp."""
        async with self.async_session() as session:
            await session.execute(
                update(BillingEventOutboxModel)
                .where(BillingEventOutboxModel.id == outbox_id)
                .values(status="sent", sent_at=datetime.now(UTC))
            )
            await session.commit()

    async def mark_billing_event_failed(self, outbox_id: int, error: str) -> None:
        """
        Mark a single outbox row as failed.

        Increments the attempt counter and stores up to 1000 chars of the error.
        """
        async with self.async_session() as session:
            row = await session.get(BillingEventOutboxModel, outbox_id)
            if row:
                row.status = "failed"
                row.attempts = (row.attempts or 0) + 1
                row.last_error = error[:1000]
                await session.commit()
