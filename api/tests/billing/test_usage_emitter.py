"""
TDD tests for usage_emitter.emit_settlement.

Tests:
  (a) Provisioned org (lago_customer_id set): settle_run_usage is called with correct meter values
  (b) Unprovisioned org (lago_customer_id is None): no-op (settle not called)
  (c) cost_info is None: no-op
"""
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from api.services.billing import usage_emitter as emitter_mod


def _make_run(org_id: int, run_id: int = 42):
    return SimpleNamespace(
        id=run_id,
        workflow=SimpleNamespace(
            organization_id=org_id,
            user=None,
        ),
    )


COST_INFO = {
    "cost_breakdown": {
        "llm_cost": 0.10,
        "tts_cost": 0.0,
        "stt_cost": 0.0,
        "total": 0.10,
    },
    "call_duration_seconds": 120,
    "billing_reservation": {
        "est_minutes": 1.0,
        "est_cents": 5.0,
    },
}


class TestEmitSettlement:
    async def test_provisioned_org_calls_settle(self, monkeypatch):
        """emit_settlement calls settle_run_usage with correct meter values for provisioned org."""
        org_id = 99

        mock_org = SimpleNamespace(id=org_id, lago_customer_id="cus_abc123")
        monkeypatch.setattr(
            emitter_mod.db_client,
            "get_organization_by_id",
            AsyncMock(return_value=mock_org),
        )
        mock_settle = AsyncMock()
        monkeypatch.setattr(
            emitter_mod.db_client, "settle_run_usage", mock_settle
        )

        run = _make_run(org_id)
        await emitter_mod.emit_settlement(run, COST_INFO)

        mock_settle.assert_awaited_once()
        kwargs = mock_settle.await_args.kwargs
        # 120s / 60 = 2.0 minutes
        assert kwargs["voice_minutes"] == pytest.approx(2.0)
        # (0.10 + 0 + 0) * 100 = 10.0 cents
        assert kwargs["ai_cost_cents"] == pytest.approx(10.0)
        assert kwargs["est_minutes"] == pytest.approx(1.0)
        assert kwargs["est_cents"] == pytest.approx(5.0)
        assert kwargs["run_id"] == 42

    async def test_unprovisioned_org_is_noop(self, monkeypatch):
        """emit_settlement is a no-op when org has no lago_customer_id."""
        org_id = 100

        mock_org = SimpleNamespace(id=org_id, lago_customer_id=None)
        monkeypatch.setattr(
            emitter_mod.db_client,
            "get_organization_by_id",
            AsyncMock(return_value=mock_org),
        )
        mock_settle = AsyncMock()
        monkeypatch.setattr(
            emitter_mod.db_client, "settle_run_usage", mock_settle
        )

        run = _make_run(org_id)
        await emitter_mod.emit_settlement(run, COST_INFO)

        mock_settle.assert_not_called()

    async def test_no_cost_info_is_noop(self, monkeypatch):
        """emit_settlement is a no-op when cost_info is None."""
        mock_get_org = AsyncMock()
        monkeypatch.setattr(
            emitter_mod.db_client, "get_organization_by_id", mock_get_org
        )
        run = _make_run(org_id=1)
        await emitter_mod.emit_settlement(run, None)
        mock_get_org.assert_not_called()

    async def test_no_org_id_is_noop(self, monkeypatch):
        """emit_settlement is a no-op when org_id cannot be resolved."""
        mock_get_org = AsyncMock()
        monkeypatch.setattr(
            emitter_mod.db_client, "get_organization_by_id", mock_get_org
        )
        run = SimpleNamespace(
            id=42,
            workflow=SimpleNamespace(organization_id=None, user=None),
        )
        await emitter_mod.emit_settlement(run, COST_INFO)
        mock_get_org.assert_not_called()
