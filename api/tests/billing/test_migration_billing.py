from api.db.models import (
    BillingEventOutboxModel,
    OrganizationModel,
    OrganizationUsageCycleModel,
)

def test_org_has_billing_columns():
    cols = OrganizationModel.__table__.columns.keys()
    for c in [
        "lago_customer_id", "billing_plan_code", "overage_policy",
        "overage_cap_pct", "included_voice_minutes", "included_ai_cost_cents",
        "billing_suspended",
    ]:
        assert c in cols

def test_cycle_has_meter_columns():
    cols = OrganizationUsageCycleModel.__table__.columns.keys()
    for c in [
        "used_voice_minutes", "used_ai_cost_cents",
        "allowance_voice_minutes", "allowance_ai_cost_cents",
    ]:
        assert c in cols

def test_outbox_unique_transaction_id():
    cols = BillingEventOutboxModel.__table__.columns.keys()
    assert "transaction_id" in cols
    assert "recompute_seq" in cols
    assert BillingEventOutboxModel.__table__.columns["transaction_id"].unique
