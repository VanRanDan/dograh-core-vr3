"""
Two-meter reserve/settle mixin for billing.

Implements atomic row-locked reserve (SELECT ... FOR UPDATE) and
single-session settle (cycle delta + 2 outbox events in one transaction).

Registered as a mixin on DBClient in api/db/db_client.py.
"""
from sqlalchemy import select

from api.db.base_client import BaseDBClient
from api.db.models import OrganizationUsageCycleModel


class OrganizationBillingClient(BaseDBClient):

    async def reserve_meter(
        self, session, *, cycle_id: int, meter: str, est: float, limit: float
    ) -> bool:
        """
        Atomic row-locked reserve.

        SELECT ... FOR UPDATE on the cycle row, checking used_<meter> + est <= limit.
        If row found: bump used_<meter> by est and return True.
        If not found (over limit): return False.
        Caller controls commit (do NOT commit here).
        """
        used_col = getattr(OrganizationUsageCycleModel, f"used_{meter}")
        result = await session.execute(
            select(OrganizationUsageCycleModel)
            .where(
                OrganizationUsageCycleModel.id == cycle_id,
                used_col + est <= limit,
            )
            .with_for_update()
        )
        cycle = result.scalar_one_or_none()
        if cycle is None:
            return False
        setattr(cycle, f"used_{meter}", getattr(cycle, f"used_{meter}") + est)
        return True

    async def settle_run_usage(
        self,
        org_id: int,
        *,
        run_id: int,
        voice_minutes: float,
        ai_cost_cents: float,
        est_minutes: float,
        est_cents: float,
        session=None,
    ) -> None:
        """
        Apply actual-minus-estimate deltas to current cycle + enqueue 2 outbox events.

        ALL in one session/transaction. If session is None, opens and commits;
        if provided, reuses caller's session and does NOT commit.

        Clamps each field to >= 0 to guard against rounding/negative deltas.
        """
        own_session = session is None
        ctx = self.async_session() if own_session else _nullctx(session)
        async with ctx as s:
            cycle = await self._get_or_create_current_cycle_impl(
                org_id, s, commit=False
            )
            # Re-lock the row for the update
            locked_result = await s.execute(
                select(OrganizationUsageCycleModel)
                .where(OrganizationUsageCycleModel.id == cycle.id)
                .with_for_update()
            )
            locked = locked_result.scalar_one()

            locked.used_voice_minutes = max(
                0.0, locked.used_voice_minutes + (voice_minutes - est_minutes)
            )
            locked.used_ai_cost_cents = max(
                0.0, locked.used_ai_cost_cents + (ai_cost_cents - est_cents)
            )

            await self.enqueue_billing_event(
                s,
                workflow_run_id=run_id,
                metric_code="voice_minutes",
                value=voice_minutes,
            )
            await self.enqueue_billing_event(
                s,
                workflow_run_id=run_id,
                metric_code="ai_cost_cents",
                value=ai_cost_cents,
            )

            if own_session:
                await s.commit()

    async def get_cycle_billing_usage(self, org_id: int) -> dict:
        """Return current cycle billing meter values and allowances."""
        async with self.async_session() as s:
            cycle = await self._get_or_create_current_cycle_impl(
                org_id, s, commit=False
            )
            return {
                "used_voice_minutes": cycle.used_voice_minutes,
                "used_ai_cost_cents": cycle.used_ai_cost_cents,
                "allowance_voice_minutes": cycle.allowance_voice_minutes,
                "allowance_ai_cost_cents": cycle.allowance_ai_cost_cents,
            }


class _nullctx:
    """Null context manager for reusing an existing session."""

    def __init__(self, session):
        self._session = session

    async def __aenter__(self):
        return self._session

    async def __aexit__(self, *args):
        return False
