"""
TDD tests for usage_emitter.emit_settlement.

Tests:
  (a) Provisioned org (lago_customer_id set): settle_run_usage is called with
      correct meter values and the gate's reservation est (1.0, 25.0)
  (b) Unprovisioned org (lago_customer_id is None): no-op (settle not called)
  (c) cost_info is None: no-op
  (d) org_id cannot be resolved: no-op

Org resolution goes through the shared `resolve_billing_org_id` helper keyed on
the run's workflow_id (same helper the call-start gate uses), so it is
monkeypatched here rather than seeding a DB workflow. The estimate is
RECOMPUTED via estimate_units(None) — these tests assert the canonical
(1.0, 25.0) floor.
"""
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from api.services.billing import usage_emitter as emitter_mod


def _make_run(workflow_id: int = 7, run_id: int = 42):
    return SimpleNamespace(id=run_id, workflow_id=workflow_id)


def _patch_resolver(monkeypatch, org_id):
    async def _resolve(workflow_id):
        return org_id

    monkeypatch.setattr(emitter_mod, "resolve_billing_org_id", _resolve)


COST_INFO = {
    "cost_breakdown": {
        "llm_cost": 0.10,
        "tts_cost": 0.0,
        "stt_cost": 0.0,
        "total": 0.10,
    },
    "call_duration_seconds": 120,
}


class TestEmitSettlement:
    async def test_provisioned_org_calls_settle(self, monkeypatch):
        """emit_settlement calls settle_run_usage with correct meter values and est (1.0, 25.0)."""
        org_id = 99
        _patch_resolver(monkeypatch, org_id)

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

        run = _make_run()
        await emitter_mod.emit_settlement(run, COST_INFO)

        mock_settle.assert_awaited_once()
        kwargs = mock_settle.await_args.kwargs
        # 120s / 60 = 2.0 minutes
        assert kwargs["voice_minutes"] == pytest.approx(2.0)
        # (0.10 + 0 + 0) * 100 = 10.0 cents
        assert kwargs["ai_cost_cents"] == pytest.approx(10.0)
        # Recomputed reservation floor (matches the gate's estimate_units(None)).
        assert kwargs["est_minutes"] == pytest.approx(1.0)
        assert kwargs["est_cents"] == pytest.approx(25.0)
        assert kwargs["run_id"] == 42

    async def test_unprovisioned_org_is_noop(self, monkeypatch):
        """emit_settlement is a no-op when org has no lago_customer_id."""
        org_id = 100
        _patch_resolver(monkeypatch, org_id)

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

        run = _make_run()
        await emitter_mod.emit_settlement(run, COST_INFO)

        mock_settle.assert_not_called()

    async def test_no_cost_info_is_noop(self, monkeypatch):
        """emit_settlement is a no-op when cost_info is None."""
        mock_resolve = AsyncMock()
        monkeypatch.setattr(emitter_mod, "resolve_billing_org_id", mock_resolve)
        mock_get_org = AsyncMock()
        monkeypatch.setattr(
            emitter_mod.db_client, "get_organization_by_id", mock_get_org
        )
        run = _make_run()
        await emitter_mod.emit_settlement(run, None)
        mock_resolve.assert_not_called()
        mock_get_org.assert_not_called()

    async def test_no_org_id_is_noop(self, monkeypatch):
        """emit_settlement is a no-op when org_id cannot be resolved."""
        _patch_resolver(monkeypatch, None)
        mock_get_org = AsyncMock()
        monkeypatch.setattr(
            emitter_mod.db_client, "get_organization_by_id", mock_get_org
        )
        run = _make_run()
        await emitter_mod.emit_settlement(run, COST_INFO)
        mock_get_org.assert_not_called()
