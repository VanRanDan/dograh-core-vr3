# VR3AI Billing Platform (Lago) — Implementation Plan 1 (QA-working billing)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Bill VR3AI customers for voice minutes + AI cost against recurring monthly bundles with configurable overage, using self-hosted Lago as billing truth and the local usage cycle as a fast strongly-consistent gate — delivered end-to-end on QA (rollout steps 0–6).

**Architecture:** Two-tier. Dograh reserves usage at call-start against a local `OrganizationUsageCycle` (two meters: `voice_minutes`, `ai_cost_cents`) and settles to actual post-call, emitting idempotent usage events to Lago via a transactional outbox. A reconciliation cron syncs local↔Lago. Enforcement is provisioned-only so un-provisioned orgs are never blocked.

**Tech Stack:** Python 3, FastAPI, async SQLAlchemy 2.0, Alembic, arq (Redis) for background/cron jobs, httpx for Lago REST, loguru, pytest/pytest-asyncio. Lago OSS self-hosted on Dockge.

**Spec:** `docs/superpowers/specs/2026-06-25-vr3ai-billing-platform-design.md` (rev 2).

**Out of scope (Plan 2):** prod Lago cutover, org backfill at scale, admin UI, Stripe Phase B.

## Global Constraints

- Meter codes are EXACT strings: `voice_minutes`, `ai_cost_cents`. Used as Lago billable-metric codes, outbox `metric_code`, and column suffixes (`used_<meter>`, `included_<meter>`, `allowance_<meter>`).
- `ai_cost_cents = (llm_cost + tts_cost + stt_cost) * 100`. **Telephony is EXCLUDED.** Source keys come from `cost_calculator.calculate_total_cost()` → `{"llm_cost","tts_cost","stt_cost","total"}`.
- `voice_minutes = call_duration_seconds / 60`. **Fractional. Never `ceil` at ingestion.** Rounding happens only in Lago at the charge/invoice level.
- Outbox `transaction_id = f"{run_id}:{metric_code}:{recompute_seq}"`. UNIQUE. Lago dedupes on it.
- Enforcement is **provisioned-only**: `org.lago_customer_id is None` → ALLOW (no gating).
- New DB methods that must be atomic together MUST accept and reuse a caller-passed `session` (BaseDBClient otherwise opens a fresh session per call).
- Money math uses `Decimal` then casts to float for storage (mirror `workflow_run_cost.py`).
- Test command: `cd api && .venv/Scripts/python.exe -m pytest <path> -v`. Local PG+Redis via `docker-compose-local`.
- Column style: `Column(Integer, nullable=False, default=0, server_default=text("0"))`. Enums: `Enum("a","b", name="...")`.
- Commit after every task. Conventional-commit prefixes (`feat:`, `test:`, `chore:`).

## File Structure

| Path | Responsibility | Task |
| --- | --- | --- |
| `api/alembic/versions/<rev>_billing_meters_and_outbox.py` | Add org/cycle columns + outbox table | 2 |
| `api/db/models.py` (modify) | New columns on `OrganizationModel`, `OrganizationUsageCycleModel`; new `BillingEventOutboxModel` | 2 |
| `api/services/billing/__init__.py` | Package marker | 3 |
| `api/services/billing/meters.py` | Pure: `cost_info -> (voice_minutes, ai_cost_cents)` | 3 |
| `api/services/billing/estimator.py` | `estimate_units(workflow) -> (minutes, cents)` reservation floor | 4 |
| `api/db/billing_outbox_client.py` | Outbox CRUD: insert (session), fetch_pending, mark_sent/failed | 5 |
| `api/services/billing/lago_client.py` | Lago REST wrapper | 6 |
| `api/db/organization_billing_client.py` | Two-meter reserve/settle + allowance/policy reads (atomic) | 7,9 |
| `api/services/billing/usage_emitter.py` | Settlement hook: compute, settle, enqueue outbox (shared session) | 7 |
| `api/services/pricing/workflow_run_cost.py` (modify) | Call emitter inside settlement, one transaction | 7 |
| `api/tasks/billing_tasks.py` | arq cron: `drain_billing_outbox`, `reconcile_billing` | 8,11 |
| `api/tasks/arq.py` (modify) | Register functions + `cron_jobs` | 8,11 |
| `api/services/billing/enforcement.py` | `check_and_reserve(org_id, workflow_id) -> QuotaCheckResult` | 9 |
| `api/routes/{agent_stream,public_agent,telephony,campaign}.py` (modify) | Swap gate to enforcement | 10 |
| `api/services/billing/reconciliation.py` | Pull Lago → max/overwrite local + sync allowances | 11 |
| `api/services/billing/provisioning.py` | Create/link Lago customer+subscription, backfill allowance | 12 |
| `api/routes/billing_webhooks.py` | Lago webhooks → suspend/resume | 13 |
| `api/app.py` (modify) | Mount webhook router | 13 |
| `deploy/lago-dev/` | Dockge compose + env template for lago-dev | 1 |

---

## Shared Test Fixtures (create before Task 2's tests run)

Create `api/tests/billing/__init__.py` (empty) and `api/tests/billing/conftest.py` with the fixtures every task's tests reference. Defined once here (DRY); tasks reference them by name.

```python
import asyncio
import pytest
from datetime import datetime, timedelta, timezone

from api.db.base_client import BaseDBClient
from api.db.models import OrganizationModel, OrganizationUsageCycleModel

_db = BaseDBClient()


async def _make_org(**over):
    """Insert an OrganizationModel with billing fields; return it detached."""
    async with _db.async_session() as s:
        org = OrganizationModel(
            provider_id=over.get("provider_id", f"test-{datetime.now().timestamp()}"),
            quota_type="monthly", quota_reset_day=1,
            lago_customer_id=over.get("lago_customer_id"),
            billing_plan_code=over.get("billing_plan_code"),
            overage_policy=over.get("overage_policy", "block"),
            overage_cap_pct=over.get("overage_cap_pct"),
            included_voice_minutes=over.get("included_minutes", 0),
            included_ai_cost_cents=over.get("included_cents", 0),
            billing_suspended=over.get("billing_suspended", False),
        )
        s.add(org)
        await s.commit()
        await s.refresh(org)
        return org


async def _make_cycle(org_id, **over):
    now = datetime.now(timezone.utc)
    async with _db.async_session() as s:
        cycle = OrganizationUsageCycleModel(
            organization_id=org_id,
            period_start=now.replace(day=1, hour=0, minute=0, second=0, microsecond=0),
            period_end=now + timedelta(days=20),
            quota_dograh_tokens=0,
            used_voice_minutes=over.get("used_minutes", 0.0),
            used_ai_cost_cents=over.get("used_cents", 0.0),
            allowance_voice_minutes=over.get("allowance_minutes", 0),
            allowance_ai_cost_cents=over.get("allowance_cents", 0),
        )
        s.add(cycle)
        await s.commit()
        await s.refresh(cycle)
        return cycle


@pytest.fixture
def seed_org():
    created = []
    def _f(**kw):
        org = asyncio.get_event_loop().run_until_complete(_make_org(**kw))
        created.append(org)
        return org
    return _f


@pytest.fixture
def seed_org_with_cycle():
    def _f(**kw):
        org = asyncio.get_event_loop().run_until_complete(_make_org(**kw))
        asyncio.get_event_loop().run_until_complete(_make_cycle(org.id, **kw))
        return org
    return _f


@pytest.fixture
def seed_pending_outbox():
    """Insert one workflow run + a pending voice_minutes outbox row; return run id."""
    from api.db.billing_outbox_client import billing_outbox_client
    async def _setup():
        async with billing_outbox_client.async_session() as s:
            await billing_outbox_client.enqueue_event(
                s, workflow_run_id=_EXISTING_RUN_ID, metric_code="voice_minutes", value=1.5)
            await s.commit()
    asyncio.get_event_loop().run_until_complete(_setup())
    return _EXISTING_RUN_ID
```

Helper coroutines used inside tests (`_get_org`, `_cycle`, `_cycle_usage`, `_async`) are thin reads against `_db`/`OrganizationBillingClient`; define them at the top of each test module that uses them, e.g.:

```python
import asyncio
from api.db.base_client import BaseDBClient
from api.db.models import OrganizationModel, OrganizationUsageCycleModel
from sqlalchemy import select

_db = BaseDBClient()

async def _get_org(org_id):
    async with _db.async_session() as s:
        return await s.get(OrganizationModel, org_id)

def _async(value):
    async def _c(*a, **k): return value
    return _c()
```

`_EXISTING_RUN_ID` must point at a real `workflow_runs` row (the outbox has an FK). Either reuse a run created by the existing test harness or insert a minimal `WorkflowRunModel` in the fixture — match how current `tests/` create runs. If the suite uses an autouse DB-setup/teardown fixture, reuse it rather than adding a second.

> **Note on async fixtures:** the snippets above use `run_until_complete` for brevity. If the existing suite uses `pytest-asyncio` `async def` fixtures or an `anyio` backend, mirror that style instead — match `api/conftest.py`.

---

### Task 1: Stand up lago-dev + define metrics and a test plan

Infra/ops task (no TDD). Deliverable: a reachable lago-dev with two billable metrics and one plan, plus an API key Dograh can use.

**Files:**
- Create: `deploy/lago-dev/docker-compose.yaml`, `deploy/lago-dev/.env.example`, `deploy/lago-dev/README.md`

- [ ] **Step 1: Write the compose stack**

Base it on the official Lago self-host compose (api, front, worker, clock, pg, redis, clickhouse). Bind the stack to the dedicated VR3 macvlan IP **192.11.0.39** (fits the map: Langfuse infra .35/.36, Hermes .38). Key env in `.env.example`:

```env
LAGO_API_URL=http://192.11.0.39:3000     # macvlan IP; how Dograh reaches Lago server-to-server
LAGO_FRONT_URL=https://vr3ai-billing-dev.tapcloud.org
API_URL=https://vr3ai-billing-dev.tapcloud.org/api
LAGO_RSA_PRIVATE_KEY=__base64_generated__
SECRET_KEY_BASE=__generated__
LAGO_ENCRYPTION_PRIMARY_KEY=__generated__
LAGO_ENCRYPTION_DETERMINISTIC_KEY=__generated__
LAGO_ENCRYPTION_KEY_DERIVATION_SALT=__generated__
```

The Dograh stack's `LAGO_API_URL` env points at `http://192.11.0.39:3000`; nginx proxies the public host `vr3ai-billing-dev.tapcloud.org` to 192.11.0.39.

- [ ] **Step 2: Deploy to dev host (192.11.0.36) as a Dockge stack**

Mirror the Langfuse-v3 deploy pattern. Bring up the stack; confirm all 7 services healthy.

Run: `docker compose -p lago-dev ps`
Expected: `lago-api`, `lago-front`, `lago-worker`, `lago-clock`, `lago-pg`, `lago-redis`, `lago-clickhouse` all `Up`/healthy.

- [ ] **Step 3: Provision DNS + nginx vhost**

Add `vr3ai-billing-dev.tapcloud.org` A/CNAME; nginx vhost `/` → lago-front, `/api` → lago-api; Let's Encrypt cert.

Run: `curl -sS -I https://vr3ai-billing-dev.tapcloud.org/ | head -1`
Expected: `HTTP/2 200`. If the UI loads but API calls 404, fix `API_URL`/`LAGO_FRONT_URL` (the SPA base-URL footgun).

- [ ] **Step 4: Create the two billable metrics (Lago admin or API)**

```
voice_minutes : aggregation=sum_agg, field=value, recurring=false
ai_cost_cents : aggregation=sum_agg, field=value, recurring=false
```
Do NOT set per-event rounding on `voice_minutes`.

- [ ] **Step 5: Create one test plan `growth-test`**

Monthly plan, two `package` charges: `voice_minutes` free_units=1000, amount per min; `ai_cost_cents` free_units=50000, amount per cent. Numbers are placeholders.

- [ ] **Step 6: Generate an API key + record internal URL**

Capture `LAGO_API_KEY`; verify Dograh→Lago over the internal network:

Run (from a Dograh container on dev): `curl -sS -H "Authorization: Bearer $LAGO_API_KEY" http://lago-api:3000/api/v1/billable_metrics | head -c 200`
Expected: JSON listing the two metrics.

- [ ] **Step 7: Commit**

```bash
git add deploy/lago-dev/
git commit -m "chore: lago-dev stack, metrics, test plan for billing"
```

---

### Task 2: Schema migrations (org + cycle columns, outbox table)

**Files:**
- Modify: `api/db/models.py` (OrganizationModel ~L118, OrganizationUsageCycleModel ~L613; add `BillingEventOutboxModel`)
- Create: `api/alembic/versions/<rev>_billing_meters_and_outbox.py` (via `alembic revision`)
- Test: `api/tests/billing/test_migration_billing.py`

**Interfaces:**
- Produces: `OrganizationModel.{lago_customer_id, billing_plan_code, overage_policy, overage_cap_pct, included_voice_minutes, included_ai_cost_cents, billing_suspended}`; `OrganizationUsageCycleModel.{used_voice_minutes, used_ai_cost_cents, allowance_voice_minutes, allowance_ai_cost_cents}`; `BillingEventOutboxModel`.

- [ ] **Step 1: Write the failing test (models expose new columns)**

`api/tests/billing/test_migration_billing.py`:

```python
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd api && .venv/Scripts/python.exe -m pytest tests/billing/test_migration_billing.py -v`
Expected: FAIL — `ImportError: cannot import name 'BillingEventOutboxModel'`.

- [ ] **Step 3: Add columns + model in `api/db/models.py`**

On `OrganizationModel` (after `price_per_second_usd`):

```python
    # --- Billing (Lago) ---
    lago_customer_id = Column(String, nullable=True)
    billing_plan_code = Column(String, nullable=True)
    overage_policy = Column(
        Enum("allow", "cap", "block", name="overage_policy"),
        nullable=False,
        default="block",
        server_default=text("'block'::overage_policy"),
    )
    overage_cap_pct = Column(Integer, nullable=True)
    included_voice_minutes = Column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    included_ai_cost_cents = Column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    billing_suspended = Column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
```

On `OrganizationUsageCycleModel` (after `quota_amount_usd`):

```python
    used_voice_minutes = Column(Float, nullable=False, default=0, server_default=text("0"))
    used_ai_cost_cents = Column(Float, nullable=False, default=0, server_default=text("0"))
    allowance_voice_minutes = Column(Integer, nullable=False, default=0, server_default=text("0"))
    allowance_ai_cost_cents = Column(Integer, nullable=False, default=0, server_default=text("0"))
```

New model (end of file, before any trailing code):

```python
class BillingEventOutboxModel(Base):
    """Transactional outbox for at-least-once, idempotent Lago usage events."""

    __tablename__ = "billing_event_outbox"

    id = Column(Integer, primary_key=True, index=True)
    workflow_run_id = Column(Integer, ForeignKey("workflow_runs.id"), nullable=False)
    metric_code = Column(String, nullable=False)
    value = Column(Float, nullable=False)
    recompute_seq = Column(Integer, nullable=False, default=0, server_default=text("0"))
    transaction_id = Column(String, nullable=False, unique=True, index=True)
    status = Column(
        Enum("pending", "sent", "failed", name="outbox_status"),
        nullable=False,
        default="pending",
        server_default=text("'pending'::outbox_status"),
    )
    attempts = Column(Integer, nullable=False, default=0, server_default=text("0"))
    last_error = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(UTC))
    sent_at = Column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        Index("idx_outbox_status_created", "status", "created_at"),
    )
```

(Confirm `workflow_runs` is the `WorkflowRunModel.__tablename__`; adjust the FK target if different.)

- [ ] **Step 4: Run the model tests to verify they pass**

Run: `cd api && .venv/Scripts/python.exe -m pytest tests/billing/test_migration_billing.py -v`
Expected: PASS (3 tests).

- [ ] **Step 5: Generate the Alembic migration**

Run: `cd api && .venv/Scripts/python.exe -m alembic revision --autogenerate -m "billing meters and outbox"`
Then open the generated file and verify it creates the two enums (`overage_policy`, `outbox_status`), the 7 org columns, 4 cycle columns, and `billing_event_outbox`. Remove any unrelated autogen drift.

- [ ] **Step 6: Apply + round-trip the migration**

Run: `cd api && .venv/Scripts/python.exe -m alembic upgrade head && .venv/Scripts/python.exe -m alembic downgrade -1 && .venv/Scripts/python.exe -m alembic upgrade head`
Expected: all three succeed with no error.

- [ ] **Step 7: Commit**

```bash
git add api/db/models.py api/alembic/versions/ api/tests/billing/test_migration_billing.py
git commit -m "feat(billing): schema for two meters + event outbox"
```

---

### Task 3: Meter computation (`meters.py`)

**Files:**
- Create: `api/services/billing/__init__.py` (empty), `api/services/billing/meters.py`
- Test: `api/tests/billing/test_meters.py`

**Interfaces:**
- Produces: `compute_meters(cost_info: dict) -> tuple[float, float]` returning `(voice_minutes, ai_cost_cents)`.

- [ ] **Step 1: Write the failing test**

```python
from api.services.billing.meters import compute_meters

def test_ai_cost_excludes_telephony():
    cost_info = {
        "cost_breakdown": {"llm_cost": 0.10, "tts_cost": 0.05, "stt_cost": 0.05,
                            "telephony_call": 0.20, "total": 0.40},
        "call_duration_seconds": 90,
    }
    minutes, cents = compute_meters(cost_info)
    assert minutes == 1.5                 # 90/60, fractional, no ceil
    assert round(cents, 6) == 20.0        # (0.10+0.05+0.05)*100, telephony excluded

def test_zero_when_missing():
    minutes, cents = compute_meters({})
    assert minutes == 0.0 and cents == 0.0
```

- [ ] **Step 2: Run to verify fail**

Run: `cd api && .venv/Scripts/python.exe -m pytest tests/billing/test_meters.py -v`
Expected: FAIL — module not found.

- [ ] **Step 3: Implement `meters.py`**

```python
from decimal import Decimal


def compute_meters(cost_info: dict | None) -> tuple[float, float]:
    """Derive billing meters from a workflow-run cost_info.

    Returns (voice_minutes, ai_cost_cents). voice_minutes is FRACTIONAL
    (rounding happens in Lago at charge time). ai_cost_cents excludes
    telephony (carrier pass-through already covered by the minutes meter).
    """
    if not cost_info:
        return 0.0, 0.0

    breakdown = cost_info.get("cost_breakdown") or {}
    ai_usd = (
        Decimal(str(breakdown.get("llm_cost", 0)))
        + Decimal(str(breakdown.get("tts_cost", 0)))
        + Decimal(str(breakdown.get("stt_cost", 0)))
    )
    ai_cost_cents = float(ai_usd * Decimal("100"))

    duration = cost_info.get("call_duration_seconds", 0) or 0
    voice_minutes = float(Decimal(str(duration)) / Decimal("60"))

    return voice_minutes, ai_cost_cents
```

- [ ] **Step 4: Run to verify pass**

Run: `cd api && .venv/Scripts/python.exe -m pytest tests/billing/test_meters.py -v`
Expected: PASS (2 tests).

- [ ] **Step 5: Commit**

```bash
git add api/services/billing/__init__.py api/services/billing/meters.py api/tests/billing/test_meters.py
git commit -m "feat(billing): meter computation (fractional minutes, ai cents, telephony excluded)"
```

---

### Task 4: Reservation estimator (`estimator.py`)

**Files:**
- Create: `api/services/billing/estimator.py`
- Test: `api/tests/billing/test_estimator.py`

**Interfaces:**
- Produces: `estimate_units(workflow=None) -> tuple[float, float]` — conservative `(voice_minutes, ai_cost_cents)` reservation floor.

- [ ] **Step 1: Write the failing test**

```python
from api.services.billing.estimator import estimate_units

def test_default_floor():
    minutes, cents = estimate_units(None)
    assert minutes == 1.0          # reserve at least one minute
    assert cents == 25.0           # reserve at least 25 cents of AI cost
```

- [ ] **Step 2: Run to verify fail**

Run: `cd api && .venv/Scripts/python.exe -m pytest tests/billing/test_estimator.py -v`
Expected: FAIL — module not found.

- [ ] **Step 3: Implement `estimator.py`**

```python
RESERVE_FLOOR_MINUTES = 1.0
RESERVE_FLOOR_CENTS = 25.0


def estimate_units(workflow=None) -> tuple[float, float]:
    """Conservative call-start reservation. Fixed floor for now; can later be
    derived from a workflow's historical average call length."""
    return RESERVE_FLOOR_MINUTES, RESERVE_FLOOR_CENTS
```

- [ ] **Step 4: Run to verify pass**

Run: `cd api && .venv/Scripts/python.exe -m pytest tests/billing/test_estimator.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add api/services/billing/estimator.py api/tests/billing/test_estimator.py
git commit -m "feat(billing): call-start reservation estimator (fixed floor)"
```

---

### Task 5: Outbox client (`billing_outbox_client.py`)

**Files:**
- Create: `api/db/billing_outbox_client.py`
- Test: `api/tests/billing/test_outbox_client.py`

**Interfaces:**
- Consumes: `BillingEventOutboxModel` (Task 2), `BaseDBClient` (`api/db/base_client.py`).
- Produces:
  - `async enqueue_event(session, *, workflow_run_id, metric_code, value, recompute_seq=0) -> None` (uses caller session; `on_conflict_do_nothing` on `transaction_id`).
  - `async fetch_pending(limit=100) -> list[BillingEventOutboxModel]`
  - `async mark_sent(outbox_id) -> None`
  - `async mark_failed(outbox_id, error: str) -> None`

- [ ] **Step 1: Write the failing test**

```python
import pytest
from sqlalchemy import select
from api.db.billing_outbox_client import BillingOutboxClient
from api.db.models import BillingEventOutboxModel

@pytest.mark.asyncio
async def test_enqueue_is_idempotent(db_session_factory):  # fixture: see conftest
    client = BillingOutboxClient()
    async with client.async_session() as s:
        await client.enqueue_event(s, workflow_run_id=1, metric_code="voice_minutes", value=1.5)
        await client.enqueue_event(s, workflow_run_id=1, metric_code="voice_minutes", value=1.5)
        await s.commit()
    pending = await client.fetch_pending()
    rows = [p for p in pending if p.workflow_run_id == 1 and p.metric_code == "voice_minutes"]
    assert len(rows) == 1
    assert rows[0].transaction_id == "1:voice_minutes:0"
```

- [ ] **Step 2: Run to verify fail**

Run: `cd api && .venv/Scripts/python.exe -m pytest tests/billing/test_outbox_client.py -v`
Expected: FAIL — module not found.

- [ ] **Step 3: Implement `billing_outbox_client.py`**

```python
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert

from api.db.base_client import BaseDBClient
from api.db.models import BillingEventOutboxModel


class BillingOutboxClient(BaseDBClient):
    async def enqueue_event(
        self, session, *, workflow_run_id: int, metric_code: str,
        value: float, recompute_seq: int = 0,
    ) -> None:
        txn_id = f"{workflow_run_id}:{metric_code}:{recompute_seq}"
        stmt = (
            insert(BillingEventOutboxModel)
            .values(
                workflow_run_id=workflow_run_id,
                metric_code=metric_code,
                value=value,
                recompute_seq=recompute_seq,
                transaction_id=txn_id,
            )
            .on_conflict_do_nothing(index_elements=["transaction_id"])
        )
        await session.execute(stmt)

    async def fetch_pending(self, limit: int = 100) -> list[BillingEventOutboxModel]:
        async with self.async_session() as session:
            result = await session.execute(
                select(BillingEventOutboxModel)
                .where(BillingEventOutboxModel.status == "pending")
                .order_by(BillingEventOutboxModel.created_at)
                .limit(limit)
            )
            return list(result.scalars().all())

    async def mark_sent(self, outbox_id: int) -> None:
        async with self.async_session() as session:
            row = await session.get(BillingEventOutboxModel, outbox_id)
            if row:
                row.status = "sent"
                row.sent_at = datetime.now(UTC)
                await session.commit()

    async def mark_failed(self, outbox_id: int, error: str) -> None:
        async with self.async_session() as session:
            row = await session.get(BillingEventOutboxModel, outbox_id)
            if row:
                row.status = "failed"
                row.attempts += 1
                row.last_error = error[:1000]
                await session.commit()


billing_outbox_client = BillingOutboxClient()
```

- [ ] **Step 4: Run to verify pass**

Run: `cd api && .venv/Scripts/python.exe -m pytest tests/billing/test_outbox_client.py -v`
Expected: PASS. (If `db_session_factory` fixture is absent, add a minimal fixture in `tests/billing/conftest.py` that ensures tables exist against the local test DB — mirror existing DB-touching tests.)

- [ ] **Step 5: Commit**

```bash
git add api/db/billing_outbox_client.py api/tests/billing/
git commit -m "feat(billing): transactional outbox client (idempotent enqueue)"
```

---

### Task 6: Lago REST client (`lago_client.py`)

**Files:**
- Create: `api/services/billing/lago_client.py`
- Modify: `api/constants.py` (read `LAGO_API_URL`, `LAGO_API_KEY` from env)
- Test: `api/tests/billing/test_lago_client.py` (uses `respx` to mock httpx)

**Interfaces:**
- Produces:
  - `async send_event(transaction_id, external_subscription_id, code, value) -> None`
  - `async get_current_usage(external_subscription_id) -> dict` → `{"voice_minutes": float, "ai_cost_cents": float}`
  - `async get_plan(plan_code) -> dict` → `{"voice_minutes_free": int, "ai_cost_cents_free": int}`
  - `async upsert_customer(external_id, **fields) -> str`
  - `async create_subscription(external_customer_id, external_id, plan_code) -> None`

- [ ] **Step 1: Write the failing test**

```python
import pytest, respx, httpx
from api.services.billing.lago_client import LagoClient

@pytest.mark.asyncio
async def test_send_event_posts_expected_payload():
    client = LagoClient(base_url="http://lago-api:3000", api_key="k")
    with respx.mock:
        route = respx.post("http://lago-api:3000/api/v1/events").mock(
            return_value=httpx.Response(200, json={"event": {}})
        )
        await client.send_event(
            transaction_id="7:voice_minutes:0",
            external_subscription_id="org-7",
            code="voice_minutes",
            value=1.5,
        )
        assert route.called
        body = route.calls.last.request.read().decode()
        assert "voice_minutes" in body and "1.5" in body
```

- [ ] **Step 2: Run to verify fail**

Run: `cd api && .venv/Scripts/python.exe -m pytest tests/billing/test_lago_client.py -v`
Expected: FAIL — module not found. (If `respx` missing: `cd api && .venv/Scripts/python.exe -m pip install respx` and add to `requirements.dev.txt`.)

- [ ] **Step 3: Implement `lago_client.py`**

```python
import httpx
from loguru import logger

from api.constants import LAGO_API_KEY, LAGO_API_URL


class LagoClient:
    def __init__(self, base_url: str | None = None, api_key: str | None = None):
        self.base_url = (base_url or LAGO_API_URL).rstrip("/")
        self.api_key = api_key or LAGO_API_KEY

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}

    async def send_event(self, *, transaction_id, external_subscription_id, code, value) -> None:
        payload = {"event": {
            "transaction_id": transaction_id,
            "external_subscription_id": external_subscription_id,
            "code": code,
            "properties": {"value": value},
        }}
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.post(f"{self.base_url}/api/v1/events", json=payload, headers=self._headers())
            r.raise_for_status()

    async def get_current_usage(self, external_subscription_id: str) -> dict:
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.get(
                f"{self.base_url}/api/v1/customers/{external_subscription_id}/current_usage",
                params={"external_subscription_id": external_subscription_id},
                headers=self._headers(),
            )
            r.raise_for_status()
            charges = r.json().get("customer_usage", {}).get("charges_usage", [])
        out = {"voice_minutes": 0.0, "ai_cost_cents": 0.0}
        for ch in charges:
            code = ch.get("billable_metric", {}).get("code")
            if code in out:
                out[code] = float(ch.get("units", 0))
        return out

    async def get_plan(self, plan_code: str) -> dict:
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.get(f"{self.base_url}/api/v1/plans/{plan_code}", headers=self._headers())
            r.raise_for_status()
            charges = r.json().get("plan", {}).get("charges", [])
        free = {"voice_minutes_free": 0, "ai_cost_cents_free": 0}
        for ch in charges:
            code = ch.get("billable_metric_code")
            props = ch.get("properties", {})
            if code == "voice_minutes":
                free["voice_minutes_free"] = int(float(props.get("free_units", 0)))
            elif code == "ai_cost_cents":
                free["ai_cost_cents_free"] = int(float(props.get("free_units", 0)))
        return free

    async def upsert_customer(self, external_id: str, **fields) -> str:
        payload = {"customer": {"external_id": external_id, **fields}}
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.post(f"{self.base_url}/api/v1/customers", json=payload, headers=self._headers())
            r.raise_for_status()
        return external_id

    async def create_subscription(self, *, external_customer_id, external_id, plan_code) -> None:
        payload = {"subscription": {
            "external_customer_id": external_customer_id,
            "external_id": external_id,
            "plan_code": plan_code,
        }}
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.post(f"{self.base_url}/api/v1/subscriptions", json=payload, headers=self._headers())
            r.raise_for_status()


lago_client = LagoClient()
```

Add to `api/constants.py`:

```python
import os
LAGO_API_URL = os.getenv("LAGO_API_URL", "http://lago-api:3000")
LAGO_API_KEY = os.getenv("LAGO_API_KEY", "")
LAGO_WEBHOOK_SECRET = os.getenv("LAGO_WEBHOOK_SECRET", "")
```

- [ ] **Step 4: Run to verify pass**

Run: `cd api && .venv/Scripts/python.exe -m pytest tests/billing/test_lago_client.py -v`
Expected: PASS. (Verify the exact Lago `current_usage` / `plans` JSON shape against the running lago-dev from Task 1 and adjust the parsers if the field names differ.)

- [ ] **Step 5: Commit**

```bash
git add api/services/billing/lago_client.py api/constants.py api/tests/billing/test_lago_client.py api/requirements.dev.txt
git commit -m "feat(billing): Lago REST client (events, usage, plans, customers, subscriptions)"
```

---

### Task 7: Two-meter reserve/settle client + settlement emitter

**Files:**
- Create: `api/db/organization_billing_client.py`, `api/services/billing/usage_emitter.py`
- Modify: `api/services/pricing/workflow_run_cost.py` (call emitter inside settlement)
- Test: `api/tests/billing/test_org_billing_client.py`, `api/tests/billing/test_usage_emitter.py`

**Interfaces:**
- Consumes: `OrganizationUsageCycleModel`, `OrganizationUsageClient._get_or_create_current_cycle_impl` (reuse period logic), `compute_meters` (Task 3), `billing_outbox_client.enqueue_event` (Task 5).
- Produces:
  - `OrganizationBillingClient.reserve(session, org, cycle, meter, est, limit) -> bool` (atomic, row-locked).
  - `OrganizationBillingClient.settle(session, org_id, *, run_id, voice_minutes, ai_cost_cents, est_minutes, est_cents) -> None` (applies `actual - estimate` deltas + enqueues outbox in the SAME session).
  - `usage_emitter.emit_settlement(workflow_run, cost_info) -> None`.

- [ ] **Step 1: Write the failing test (settle applies delta + enqueues outbox atomically)**

```python
import pytest
from api.db.organization_billing_client import OrganizationBillingClient
from api.db.billing_outbox_client import billing_outbox_client

@pytest.mark.asyncio
async def test_settle_applies_delta_and_enqueues(seed_org_with_cycle):
    org = seed_org_with_cycle(used_minutes=1.0, used_cents=25.0)  # estimate already reserved
    client = OrganizationBillingClient()
    await client.settle(
        None, org.id, run_id=42,
        voice_minutes=3.0, ai_cost_cents=80.0,
        est_minutes=1.0, est_cents=25.0,
    )
    usage = await client.get_cycle_usage(org.id)
    assert usage["used_voice_minutes"] == 3.0     # 1.0 + (3.0-1.0)
    assert usage["used_ai_cost_cents"] == 80.0     # 25.0 + (80.0-25.0)
    pending = await billing_outbox_client.fetch_pending()
    codes = {p.metric_code for p in pending if p.workflow_run_id == 42}
    assert codes == {"voice_minutes", "ai_cost_cents"}
```

- [ ] **Step 2: Run to verify fail**

Run: `cd api && .venv/Scripts/python.exe -m pytest tests/billing/test_org_billing_client.py -v`
Expected: FAIL — module not found.

- [ ] **Step 3: Implement `organization_billing_client.py`**

```python
from sqlalchemy import select

from api.db.base_client import BaseDBClient
from api.db.billing_outbox_client import billing_outbox_client
from api.db.models import OrganizationUsageCycleModel
from api.db.organization_usage_client import OrganizationUsageClient

_usage = OrganizationUsageClient()


class OrganizationBillingClient(BaseDBClient):
    async def reserve(self, session, *, cycle_id: int, meter: str, est: float, limit: float) -> bool:
        used_col = getattr(OrganizationUsageCycleModel, f"used_{meter}")
        result = await session.execute(
            select(OrganizationUsageCycleModel)
            .where(OrganizationUsageCycleModel.id == cycle_id, used_col + est <= limit)
            .with_for_update()
        )
        cycle = result.scalar_one_or_none()
        if cycle is None:
            return False
        setattr(cycle, f"used_{meter}", getattr(cycle, f"used_{meter}") + est)
        return True

    async def settle(self, session, org_id: int, *, run_id: int,
                     voice_minutes: float, ai_cost_cents: float,
                     est_minutes: float, est_cents: float) -> None:
        own = session is None
        session = session or self.async_session()
        async with session if own else _nullctx(session) as s:
            cycle = await _usage._get_or_create_current_cycle_impl(org_id, s, commit=False)
            locked = (await s.execute(
                select(OrganizationUsageCycleModel)
                .where(OrganizationUsageCycleModel.id == cycle.id)
                .with_for_update()
            )).scalar_one()
            locked.used_voice_minutes += (voice_minutes - est_minutes)
            locked.used_ai_cost_cents += (ai_cost_cents - est_cents)
            await billing_outbox_client.enqueue_event(
                s, workflow_run_id=run_id, metric_code="voice_minutes", value=voice_minutes)
            await billing_outbox_client.enqueue_event(
                s, workflow_run_id=run_id, metric_code="ai_cost_cents", value=ai_cost_cents)
            if own:
                await s.commit()

    async def get_cycle_usage(self, org_id: int) -> dict:
        async with self.async_session() as s:
            cycle = await _usage._get_or_create_current_cycle_impl(org_id, s, commit=False)
            return {
                "used_voice_minutes": cycle.used_voice_minutes,
                "used_ai_cost_cents": cycle.used_ai_cost_cents,
                "allowance_voice_minutes": cycle.allowance_voice_minutes,
                "allowance_ai_cost_cents": cycle.allowance_ai_cost_cents,
            }


class _nullctx:
    def __init__(self, obj): self.obj = obj
    async def __aenter__(self): return self.obj
    async def __aexit__(self, *a): return False


organization_billing_client = OrganizationBillingClient()
```

- [ ] **Step 4: Run to verify pass**

Run: `cd api && .venv/Scripts/python.exe -m pytest tests/billing/test_org_billing_client.py -v`
Expected: PASS.

- [ ] **Step 5: Write failing test for the emitter**

`api/tests/billing/test_usage_emitter.py`:

```python
import pytest
from types import SimpleNamespace
from api.services.billing import usage_emitter

@pytest.mark.asyncio
async def test_emit_settlement_calls_settle(monkeypatch, seed_org_with_cycle):
    org = seed_org_with_cycle(used_minutes=1.0, used_cents=25.0)
    captured = {}
    async def fake_settle(session, org_id, **kw): captured.update(org_id=org_id, **kw)
    monkeypatch.setattr(usage_emitter.organization_billing_client, "settle", fake_settle)
    run = SimpleNamespace(id=42, workflow=SimpleNamespace(organization_id=org.id))
    cost_info = {"cost_breakdown": {"llm_cost": 0.1, "tts_cost": 0, "stt_cost": 0, "total": 0.1},
                 "call_duration_seconds": 120,
                 "billing_reservation": {"est_minutes": 1.0, "est_cents": 25.0}}
    await usage_emitter.emit_settlement(run, cost_info)
    assert captured["voice_minutes"] == 2.0
    assert round(captured["ai_cost_cents"], 6) == 10.0
    assert captured["est_minutes"] == 1.0
```

- [ ] **Step 6: Implement `usage_emitter.py`**

```python
from loguru import logger

from api.db.organization_billing_client import organization_billing_client
from api.services.billing.meters import compute_meters


async def emit_settlement(workflow_run, cost_info: dict | None) -> None:
    """Settle the call-start reservation to actual and enqueue Lago events.
    No-op if the org is not provisioned or cost_info is missing."""
    if not cost_info:
        return
    org_id = _resolve_org_id(workflow_run)
    if org_id is None:
        return
    voice_minutes, ai_cost_cents = compute_meters(cost_info)
    reservation = cost_info.get("billing_reservation") or {}
    est_minutes = float(reservation.get("est_minutes", 0))
    est_cents = float(reservation.get("est_cents", 0))
    try:
        await organization_billing_client.settle(
            None, org_id, run_id=workflow_run.id,
            voice_minutes=voice_minutes, ai_cost_cents=ai_cost_cents,
            est_minutes=est_minutes, est_cents=est_cents,
        )
    except Exception as e:  # never fail settlement on billing error
        logger.error(f"Billing settle failed for run {workflow_run.id}: {e}")


def _resolve_org_id(workflow_run):
    wf = getattr(workflow_run, "workflow", None)
    org_id = getattr(wf, "organization_id", None)
    if org_id is None and wf and getattr(wf, "user", None):
        org_id = wf.user.selected_organization_id
    return org_id
```

- [ ] **Step 7: Wire emitter into settlement**

In `api/services/pricing/workflow_run_cost.py`, inside `calculate_workflow_run_cost` (after `apply_workflow_run_usage_to_organization`), add:

```python
    from api.services.billing.usage_emitter import emit_settlement
    await emit_settlement(workflow_run, cost_info)
```

(Keep the legacy `apply_workflow_run_usage_to_organization` during transition — it maintains the old `used_dograh_tokens` for the existing UI.)

- [ ] **Step 8: Run emitter tests**

Run: `cd api && .venv/Scripts/python.exe -m pytest tests/billing/test_usage_emitter.py -v`
Expected: PASS.

- [ ] **Step 9: Commit**

```bash
git add api/db/organization_billing_client.py api/services/billing/usage_emitter.py api/services/pricing/workflow_run_cost.py api/tests/billing/
git commit -m "feat(billing): reserve/settle client + settlement emitter (single-session, outbox)"
```

---

### Task 8: Outbox drainer (arq cron)

**Files:**
- Create: `api/tasks/billing_tasks.py`
- Modify: `api/tasks/arq.py` (register `drain_billing_outbox` + cron), `api/tasks/function_names.py` (add name)
- Test: `api/tests/billing/test_outbox_drainer.py`

**Interfaces:**
- Consumes: `billing_outbox_client` (Task 5), `lago_client` (Task 6), org→subscription mapping (use `external_subscription_id = f"org-{organization_id}"`).
- Produces: `async drain_billing_outbox(ctx) -> int` (count sent).

- [ ] **Step 1: Write the failing test**

```python
import pytest
from api.tasks.billing_tasks import drain_billing_outbox

@pytest.mark.asyncio
async def test_drain_sends_pending_and_marks_sent(monkeypatch, seed_pending_outbox):
    sent = []
    async def fake_send(**kw): sent.append(kw)
    monkeypatch.setattr("api.tasks.billing_tasks.lago_client.send_event", fake_send)
    count = await drain_billing_outbox({})
    assert count >= 1
    assert any(s["code"] == "voice_minutes" for s in sent)
```

- [ ] **Step 2: Run to verify fail**

Run: `cd api && .venv/Scripts/python.exe -m pytest tests/billing/test_outbox_drainer.py -v`
Expected: FAIL — module not found.

- [ ] **Step 3: Implement `billing_tasks.py` (drainer portion)**

```python
from loguru import logger

from api.db import db_client
from api.db.billing_outbox_client import billing_outbox_client
from api.services.billing.lago_client import lago_client


def _subscription_id_for_run(run) -> str | None:
    wf = getattr(run, "workflow", None)
    org_id = getattr(wf, "organization_id", None)
    return f"org-{org_id}" if org_id else None


async def drain_billing_outbox(ctx) -> int:
    pending = await billing_outbox_client.fetch_pending(limit=200)
    sent = 0
    for row in pending:
        try:
            run = await db_client.get_workflow_run_by_id(row.workflow_run_id)
            sub_id = _subscription_id_for_run(run)
            if not sub_id:
                await billing_outbox_client.mark_failed(row.id, "no subscription id")
                continue
            await lago_client.send_event(
                transaction_id=row.transaction_id,
                external_subscription_id=sub_id,
                code=row.metric_code,
                value=row.value,
            )
            await billing_outbox_client.mark_sent(row.id)
            sent += 1
        except Exception as e:
            logger.error(f"Outbox drain failed for {row.transaction_id}: {e}")
            await billing_outbox_client.mark_failed(row.id, str(e))
    return sent
```

(Confirm the exact `db_client` getter for a run by id; if it is named differently, use that.)

- [ ] **Step 4: Register the cron in `api/tasks/arq.py`**

```python
from arq import cron
from api.tasks.billing_tasks import drain_billing_outbox, reconcile_billing  # reconcile added in Task 11
```

Add `drain_billing_outbox` (and later `reconcile_billing`) to `WorkerSettings.functions`, and:

```python
    cron_jobs = [
        cron(drain_billing_outbox, second={0, 20, 40}),   # ~every 20s
    ]
```

- [ ] **Step 5: Run to verify pass**

Run: `cd api && .venv/Scripts/python.exe -m pytest tests/billing/test_outbox_drainer.py -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add api/tasks/billing_tasks.py api/tasks/arq.py api/tasks/function_names.py api/tests/billing/test_outbox_drainer.py
git commit -m "feat(billing): outbox drainer arq cron (shadow-mode event emission)"
```

> **Rollout step 2 (shadow) reached here:** events flow to Lago; the gate is unchanged. Validate Lago totals against `OrganizationUsageCycle` on QA traffic before Task 10.

---

### Task 9: Enforcement gate (reserve + policy, provisioned-only)

**Files:**
- Create: `api/services/billing/enforcement.py`
- Test: `api/tests/billing/test_enforcement.py`

**Interfaces:**
- Consumes: `OrganizationBillingClient.reserve` (Task 7), `estimate_units` (Task 4), `QuotaCheckResult` (`api/services/quota_service.py`), `db_client.get_organization_by_id`.
- Produces: `async check_and_reserve(org_id: int, workflow_id: int | None = None) -> QuotaCheckResult`.

- [ ] **Step 1: Write the failing tests (provisioned-only + each policy)**

```python
import pytest
from api.services.billing.enforcement import check_and_reserve

@pytest.mark.asyncio
async def test_unprovisioned_allows(seed_org):
    org = seed_org(lago_customer_id=None)
    res = await check_and_reserve(org.id)
    assert res.has_quota is True

@pytest.mark.asyncio
async def test_block_denies_when_exhausted(seed_org_with_cycle):
    org = seed_org_with_cycle(lago_customer_id="org-1", overage_policy="block",
                              allowance_minutes=10, used_minutes=10)
    res = await check_and_reserve(org.id)
    assert res.has_quota is False
    assert res.error_code == "bundle_exhausted_minutes"

@pytest.mark.asyncio
async def test_suspended_denies(seed_org):
    org = seed_org(lago_customer_id="org-1", billing_suspended=True)
    res = await check_and_reserve(org.id)
    assert res.has_quota is False
    assert res.error_code == "account_suspended"
```

- [ ] **Step 2: Run to verify fail**

Run: `cd api && .venv/Scripts/python.exe -m pytest tests/billing/test_enforcement.py -v`
Expected: FAIL — module not found.

- [ ] **Step 3: Implement `enforcement.py`**

```python
from loguru import logger

from api.db import db_client
from api.db.models import OrganizationUsageCycleModel
from api.db.organization_billing_client import OrganizationBillingClient
from api.db.organization_usage_client import OrganizationUsageClient
from api.services.billing.estimator import estimate_units
from api.services.quota_service import QuotaCheckResult

_billing = OrganizationBillingClient()
_usage = OrganizationUsageClient()

_METERS = [("voice_minutes", "bundle_exhausted_minutes"),
           ("ai_cost_cents", "bundle_exhausted_ai_cost")]


async def check_and_reserve(org_id: int, workflow_id: int | None = None) -> QuotaCheckResult:
    org = await db_client.get_organization_by_id(org_id)
    if org is None or org.lago_customer_id is None:
        return QuotaCheckResult(has_quota=True)          # provisioned-only
    if org.billing_suspended:
        return QuotaCheckResult(has_quota=False, error_code="account_suspended",
                                error_message="Account suspended for billing. Contact support.")

    est_minutes, est_cents = estimate_units(None)
    ests = {"voice_minutes": est_minutes, "ai_cost_cents": est_cents}

    async with _billing.async_session() as session:
        cycle = await _usage._get_or_create_current_cycle_impl(org_id, session, commit=False)
        reserved: list[str] = []
        for meter, code in _METERS:
            allowance = getattr(cycle, f"allowance_{meter}")
            if org.overage_policy == "allow":
                limit = float("inf")
            elif org.overage_policy == "cap":
                limit = allowance * (org.overage_cap_pct or 100) / 100
            else:  # block
                limit = allowance
            ok = await _billing.reserve(session, cycle_id=cycle.id, meter=meter,
                                        est=ests[meter], limit=limit)
            if not ok:
                await session.rollback()
                logger.info(f"Billing gate DENY org={org_id} meter={meter}")
                return QuotaCheckResult(has_quota=False, error_code=code,
                                        error_message=f"Your {meter.replace('_',' ')} bundle is exhausted.")
            reserved.append(meter)
        await session.commit()
    return QuotaCheckResult(has_quota=True)
```

- [ ] **Step 4: Run to verify pass**

Run: `cd api && .venv/Scripts/python.exe -m pytest tests/billing/test_enforcement.py -v`
Expected: PASS (3 tests).

- [ ] **Step 5: Commit**

```bash
git add api/services/billing/enforcement.py api/tests/billing/test_enforcement.py
git commit -m "feat(billing): provisioned-only reserve+gate with allow/cap/block policy"
```

---

### Task 10: Wire enforcement into the 6 call sites + stamp the reservation

**Files:**
- Modify: `api/routes/agent_stream.py:70`, `api/routes/public_agent.py:183`, `api/routes/telephony.py:137`, `api/routes/telephony.py:739`, `api/routes/campaign.py:553`, `api/routes/campaign.py:875`
- Modify: the run-start path that builds `initial_context`/`cost_info` to stamp `billing_reservation` (so settlement knows the estimate).
- Test: `api/tests/billing/test_gate_integration.py` (one representative route, monkeypatched)

**Interfaces:**
- Consumes: `check_and_reserve` (Task 9). Replaces `check_dograh_quota_by_user_id` / `check_dograh_quota` which returned `QuotaCheckResult` — identical contract, so call sites change only the import + call.

- [ ] **Step 1: Write the failing test (telephony route denies on exhausted bundle)**

```python
import pytest
from api.services.quota_service import QuotaCheckResult

@pytest.mark.asyncio
async def test_telephony_blocks_when_gate_denies(monkeypatch, telephony_client):
    async def deny(org_id, workflow_id=None):
        return QuotaCheckResult(has_quota=False, error_code="bundle_exhausted_minutes",
                                error_message="exhausted")
    monkeypatch.setattr("api.routes.telephony.check_and_reserve", deny)
    # Use the same inbound-call request the existing telephony tests post
    # (copy the payload + fixture from tests/test_telephony.py or its conftest).
    resp = await telephony_client.post(INBOUND_CALL_PATH, json=INBOUND_CALL_PAYLOAD)
    assert resp.status_code in (402, 403)
```

- [ ] **Step 2: Run to verify fail**

Run: `cd api && .venv/Scripts/python.exe -m pytest tests/billing/test_gate_integration.py -v`
Expected: FAIL — `check_and_reserve` not imported in `telephony`.

- [ ] **Step 3: Swap each call site**

At each site replace, e.g.:

```python
# old
from api.services.quota_service import check_dograh_quota_by_user_id
quota_result = await check_dograh_quota_by_user_id(user.id, workflow_id=workflow_id)
# new
from api.services.billing.enforcement import check_and_reserve
quota_result = await check_and_reserve(org_id_for(user), workflow_id=workflow_id)
```

`quota_result` keeps the same `.has_quota` / `.error_message` / `.error_code` usage downstream — leave the existing rejection branches intact. Resolve `org_id` from the user's `selected_organization_id` (or the workflow's `organization_id` where already available at the site).

- [ ] **Step 4: Stamp the reservation estimate onto the run**

Where the run record / `cost_info` is initialized at call start, add the estimate so settlement (Task 7) can compute the delta:

```python
from api.services.billing.estimator import estimate_units
est_minutes, est_cents = estimate_units(None)
cost_info = {**(cost_info or {}),
             "billing_reservation": {"est_minutes": est_minutes, "est_cents": est_cents}}
```

(If `cost_info` is only created at settlement, persist `billing_reservation` on the run's `initial_context` instead and have `emit_settlement` read it from there.)

- [ ] **Step 5: Run to verify pass + full gate suite**

Run: `cd api && .venv/Scripts/python.exe -m pytest tests/billing/ -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add api/routes/
git commit -m "feat(billing): route gate to provisioned reserve; stamp reservation on run"
```

> **Rollout step 4 reached.** Deploy with all QA orgs un-provisioned (`lago_customer_id` NULL) → gate is a pass-through. Provision a single test org to exercise block/cap.

---

### Task 11: Reconciliation cron (max open / overwrite closed + allowance sync)

**Files:**
- Create: `api/services/billing/reconciliation.py`
- Modify: `api/tasks/billing_tasks.py` (add `reconcile_billing`), `api/tasks/arq.py` (cron)
- Test: `api/tests/billing/test_reconciliation.py`

**Interfaces:**
- Consumes: `lago_client.get_current_usage`, `lago_client.get_plan`, cycle access.
- Produces: `async reconcile_org(org) -> None`; `async reconcile_billing(ctx) -> int`.

- [ ] **Step 1: Write the failing test (open period never lowered)**

```python
import pytest
from api.services.billing.reconciliation import reconcile_org

@pytest.mark.asyncio
async def test_open_period_uses_max(monkeypatch, seed_org_with_cycle):
    org = seed_org_with_cycle(lago_customer_id="org-1", used_minutes=5.0, used_cents=100.0)
    async def fake_usage(sub): return {"voice_minutes": 3.0, "ai_cost_cents": 150.0}
    async def fake_plan(code): return {"voice_minutes_free": 1000, "ai_cost_cents_free": 50000}
    monkeypatch.setattr("api.services.billing.reconciliation.lago_client.get_current_usage", fake_usage)
    monkeypatch.setattr("api.services.billing.reconciliation.lago_client.get_plan", fake_plan)
    await reconcile_org(org)
    usage = await _cycle_usage(org.id)   # helper reads cycle
    assert usage["used_voice_minutes"] == 5.0     # max(5.0, 3.0): not lowered
    assert usage["used_ai_cost_cents"] == 150.0    # max(100.0, 150.0): raised to Lago
```

- [ ] **Step 2: Run to verify fail**

Run: `cd api && .venv/Scripts/python.exe -m pytest tests/billing/test_reconciliation.py -v`
Expected: FAIL — module not found.

- [ ] **Step 3: Implement `reconciliation.py`**

```python
from loguru import logger
from sqlalchemy import select

from api.db.base_client import BaseDBClient
from api.db.models import OrganizationModel, OrganizationUsageCycleModel
from api.db.organization_usage_client import OrganizationUsageClient
from api.services.billing.lago_client import lago_client

_usage = OrganizationUsageClient()
_db = BaseDBClient()


async def reconcile_org(org) -> None:
    sub_id = f"org-{org.id}"
    lago_usage = await lago_client.get_current_usage(sub_id)
    plan = await lago_client.get_plan(org.billing_plan_code) if org.billing_plan_code else None

    async with _db.async_session() as s:
        cycle = await _usage._get_or_create_current_cycle_impl(org.id, s, commit=False)
        locked = (await s.execute(
            select(OrganizationUsageCycleModel)
            .where(OrganizationUsageCycleModel.id == cycle.id).with_for_update()
        )).scalar_one()
        # OPEN period: never drop below local pending/reserved
        locked.used_voice_minutes = max(locked.used_voice_minutes, lago_usage["voice_minutes"])
        locked.used_ai_cost_cents = max(locked.used_ai_cost_cents, lago_usage["ai_cost_cents"])
        if plan:
            org_row = await s.get(OrganizationModel, org.id)
            org_row.included_voice_minutes = plan["voice_minutes_free"]
            org_row.included_ai_cost_cents = plan["ai_cost_cents_free"]
            if locked.allowance_voice_minutes == 0:
                locked.allowance_voice_minutes = plan["voice_minutes_free"]
            if locked.allowance_ai_cost_cents == 0:
                locked.allowance_ai_cost_cents = plan["ai_cost_cents_free"]
        await s.commit()


async def reconcile_billing(ctx) -> int:
    async with _db.async_session() as s:
        orgs = (await s.execute(
            select(OrganizationModel).where(OrganizationModel.lago_customer_id.isnot(None))
        )).scalars().all()
    n = 0
    for org in orgs:
        try:
            await reconcile_org(org)
            n += 1
        except Exception as e:
            logger.error(f"Reconcile failed org={org.id}: {e}")
    return n
```

Add `reconcile_billing` to `WorkerSettings.functions` and `cron_jobs`:

```python
    cron_jobs = [
        cron(drain_billing_outbox, second={0, 20, 40}),
        cron(reconcile_billing, minute={0, 15, 30, 45}),
    ]
```

(Closed-period hard-overwrite is handled at cycle rollover: when a new cycle is created, the prior cycle is no longer the "current" one returned by `_get_or_create_current_cycle_impl`, so its final Lago number is written by a one-shot rollover pass — implement as a follow-up if closed-period correction is needed before invoicing.)

- [ ] **Step 4: Run to verify pass**

Run: `cd api && .venv/Scripts/python.exe -m pytest tests/billing/test_reconciliation.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add api/services/billing/reconciliation.py api/tasks/billing_tasks.py api/tasks/arq.py api/tests/billing/test_reconciliation.py
git commit -m "feat(billing): reconciliation cron (max open period, sync allowances)"
```

---

### Task 12: Provisioning (org → Lago customer + subscription + allowance backfill)

**Files:**
- Create: `api/services/billing/provisioning.py`
- Test: `api/tests/billing/test_provisioning.py`

**Interfaces:**
- Consumes: `lago_client.upsert_customer/create_subscription/get_plan`, org update.
- Produces: `async provision_org(org_id: int, plan_code: str, *, overage_policy="block", overage_cap_pct=None) -> None` — creates Lago customer + subscription `external_id=f"org-{id}"`, sets `lago_customer_id`, `billing_plan_code`, `overage_policy`, and backfills `included_*` + current cycle `allowance_*` from the plan **before** any policy tightening.

- [ ] **Step 1: Write the failing test**

```python
import pytest
from api.services.billing.provisioning import provision_org

@pytest.mark.asyncio
async def test_provision_sets_customer_and_allowance(monkeypatch, seed_org_with_cycle):
    org = seed_org_with_cycle(lago_customer_id=None, allowance_minutes=0)
    monkeypatch.setattr("api.services.billing.provisioning.lago_client.upsert_customer",
                        lambda *a, **k: _async("org-%d" % org.id))
    monkeypatch.setattr("api.services.billing.provisioning.lago_client.create_subscription",
                        lambda **k: _async(None))
    monkeypatch.setattr("api.services.billing.provisioning.lago_client.get_plan",
                        lambda code: _async({"voice_minutes_free": 1000, "ai_cost_cents_free": 50000}))
    await provision_org(org.id, "growth-test", overage_policy="cap", overage_cap_pct=150)
    refreshed = await _get_org(org.id)
    assert refreshed.lago_customer_id == f"org-{org.id}"
    assert refreshed.billing_plan_code == "growth-test"
    assert refreshed.included_voice_minutes == 1000
    cycle = await _cycle(org.id)
    assert cycle.allowance_voice_minutes == 1000   # backfilled before policy tightens
```

(`_async` is a tiny helper returning a coroutine wrapping a value; define in the test.)

- [ ] **Step 2: Run to verify fail**

Run: `cd api && .venv/Scripts/python.exe -m pytest tests/billing/test_provisioning.py -v`
Expected: FAIL — module not found.

- [ ] **Step 3: Implement `provisioning.py`**

```python
from sqlalchemy import select

from api.db.base_client import BaseDBClient
from api.db.models import OrganizationModel, OrganizationUsageCycleModel
from api.db.organization_usage_client import OrganizationUsageClient
from api.services.billing.lago_client import lago_client

_usage = OrganizationUsageClient()
_db = BaseDBClient()


async def provision_org(org_id: int, plan_code: str, *,
                        overage_policy: str = "block",
                        overage_cap_pct: int | None = None) -> None:
    external_id = f"org-{org_id}"
    await lago_client.upsert_customer(external_id, name=external_id)
    await lago_client.create_subscription(
        external_customer_id=external_id, external_id=external_id, plan_code=plan_code)
    plan = await lago_client.get_plan(plan_code)

    async with _db.async_session() as s:
        org = await s.get(OrganizationModel, org_id)
        org.lago_customer_id = external_id
        org.billing_plan_code = plan_code
        org.included_voice_minutes = plan["voice_minutes_free"]
        org.included_ai_cost_cents = plan["ai_cost_cents_free"]
        # backfill allowance on the current cycle BEFORE applying policy
        cycle = await _usage._get_or_create_current_cycle_impl(org_id, s, commit=False)
        cycle.allowance_voice_minutes = plan["voice_minutes_free"]
        cycle.allowance_ai_cost_cents = plan["ai_cost_cents_free"]
        org.overage_policy = overage_policy
        org.overage_cap_pct = overage_cap_pct
        await s.commit()
```

- [ ] **Step 4: Run to verify pass**

Run: `cd api && .venv/Scripts/python.exe -m pytest tests/billing/test_provisioning.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add api/services/billing/provisioning.py api/tests/billing/test_provisioning.py
git commit -m "feat(billing): org provisioning (Lago customer+subscription, allowance backfill)"
```

> **Rollout step 6:** run `provision_org` for a test org, confirm a call reserves and (when exhausted) blocks, and that an invoice appears in lago-dev.

---

### Task 13: Lago → Dograh webhooks (suspend/resume)

**Files:**
- Create: `api/routes/billing_webhooks.py`
- Modify: `api/app.py` (mount router)
- Test: `api/tests/billing/test_billing_webhooks.py`

**Interfaces:**
- Consumes: `LAGO_WEBHOOK_SECRET` (Task 6), org update by `lago_customer_id`.
- Produces: `POST /billing/webhooks/lago` → set/clear `billing_suspended`.

- [ ] **Step 1: Write the failing test**

```python
import hmac, hashlib, json, pytest

@pytest.mark.asyncio
async def test_payment_failure_suspends(billing_client, seed_org):
    org = seed_org(lago_customer_id="org-9", billing_suspended=False)
    body = json.dumps({"webhook_type": "invoice.payment_failure",
                       "invoice": {"customer": {"external_id": "org-9"}}}).encode()
    sig = hmac.new(b"testsecret", body, hashlib.sha256).hexdigest()
    resp = await billing_client.post("/billing/webhooks/lago", content=body,
                                     headers={"X-Lago-Signature": sig})
    assert resp.status_code == 200
    assert (await _get_org(org.id)).billing_suspended is True
```

- [ ] **Step 2: Run to verify fail**

Run: `cd api && .venv/Scripts/python.exe -m pytest tests/billing/test_billing_webhooks.py -v`
Expected: FAIL — route 404.

- [ ] **Step 3: Implement `billing_webhooks.py`**

```python
import hashlib
import hmac

from fastapi import APIRouter, Header, HTTPException, Request
from loguru import logger
from sqlalchemy import select, update

from api.constants import LAGO_WEBHOOK_SECRET
from api.db.base_client import BaseDBClient
from api.db.models import OrganizationModel

router = APIRouter(prefix="/billing/webhooks", tags=["billing"])
_db = BaseDBClient()

_SUSPEND = {"invoice.payment_failure"}
_RESUME = {"invoice.payment_success"}


def _verify(body: bytes, signature: str | None) -> bool:
    if not LAGO_WEBHOOK_SECRET:
        return True  # dev: no secret set
    if not signature:
        return False
    expected = hmac.new(LAGO_WEBHOOK_SECRET.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)


async def _set_suspended(external_id: str, suspended: bool) -> None:
    async with _db.async_session() as s:
        await s.execute(
            update(OrganizationModel)
            .where(OrganizationModel.lago_customer_id == external_id)
            .values(billing_suspended=suspended)
        )
        await s.commit()


@router.post("/lago")
async def lago_webhook(request: Request, x_lago_signature: str | None = Header(default=None)):
    body = await request.body()
    if not _verify(body, x_lago_signature):
        raise HTTPException(status_code=401, detail="bad signature")
    payload = await request.json()
    wtype = payload.get("webhook_type", "")
    external_id = (
        payload.get("invoice", {}).get("customer", {}).get("external_id")
        or payload.get("subscription", {}).get("external_customer_id")
    )
    if not external_id:
        return {"ok": True}
    if wtype in _SUSPEND or wtype == "subscription.terminated":
        await _set_suspended(external_id, True)
    elif wtype in _RESUME:
        await _set_suspended(external_id, False)
    logger.info(f"Lago webhook {wtype} for {external_id}")
    return {"ok": True}
```

Mount in `api/app.py`:

```python
from api.routes.billing_webhooks import router as billing_webhooks_router
app.include_router(billing_webhooks_router)
```

- [ ] **Step 4: Run to verify pass**

Run: `cd api && .venv/Scripts/python.exe -m pytest tests/billing/test_billing_webhooks.py -v`
Expected: PASS.

- [ ] **Step 5: Configure the webhook in lago-dev → run full suite**

Point lago-dev's webhook endpoint at `https://<qa-dograh>/billing/webhooks/lago` with the shared secret.

Run: `cd api && .venv/Scripts/python.exe -m pytest tests/billing/ -v`
Expected: all PASS.

- [ ] **Step 6: Commit**

```bash
git add api/routes/billing_webhooks.py api/app.py api/tests/billing/test_billing_webhooks.py
git commit -m "feat(billing): Lago webhooks for suspend/resume"
```

---

## Self-Review (spec coverage)

| Spec section | Task(s) |
| --- | --- |
| §5 schema | 2 |
| §6 event flow (fractional min, ai cents, single-session, versioned txn) | 3, 5, 7 |
| §7 Lago metrics/plans/overage | 1, 6 |
| §8 reserve+gate, provisioned-only, policy | 4, 9, 10 |
| §9 reconciliation (max open/overwrite closed, allowance sync) | 11 |
| §10 webhooks | 13 |
| §11 extensibility (string plan code, meter boundary) | 2 (free-string col), 12 |
| §12 deployment | 1 |
| §13 payment Phase A (invoice-only) | Task 1 plan + manual; no card code (correct for Plan 1) |
| §14 rollout 0–6 | shadow at Task 8, gate at Task 10, go-live at Task 12 |
| §15 testing | every task's test step |

**Deferred to Plan 2 (documented, not gaps):** prod cutover (§14 step 7), Stripe Phase B (§13), at-scale org backfill, admin UI, closed-period hard-overwrite rollover pass.

**Verification-before-completion gate:** Plan is done only when `cd api && .venv/Scripts/python.exe -m pytest tests/billing/ -v` is green AND a provisioned test org on lago-dev shows: reserve on call-start, settle to actual post-call, event in Lago, invoice generated, and block/cap enforced.
