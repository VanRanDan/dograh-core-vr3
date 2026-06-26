# VR3AI Billing Platform — Design

- **Date:** 2026-06-25
- **Repo:** dograh-core-vr3 (VanRan fork)
- **Status:** Approved design, pre-implementation-plan
- **Goal:** Track per-customer agent usage minutes and AI token usage, package them into recurring bundles, and bill overage — using a self-hosted billing engine, keeping usage/customer data on-prem.

## 1. Summary

VR3AI already meters usage (the dograh-token cycle survived the billing removal). What is
missing is the **money layer**: bundle/plan definitions, overage billing, invoicing, and payment.

This design adds that layer by integrating **Lago** (self-hosted, on VR3 Dockge infra) as the
billing source-of-truth, while keeping a **local fast meter** for real-time call gating. Two
billable meters — `voice_minutes` and `ai_tokens` — back recurring monthly bundle plans with
configurable overage policy.

### Decisions locked (brainstorming)

| Decision | Choice |
|---|---|
| Billing engine | **Self-hosted Lago** (on-prem; data does not leave VR3) |
| Integration pattern | **Two-tier**: local gate + Lago billing truth + reconciliation (Approach A) |
| Pricing units | **Two separate meters**: `voice_minutes` + `ai_tokens` |
| `ai_tokens` scope | **All AI units** (LLM + STT + TTS), normalized via existing `cost_calculator`; telephony excluded |
| Bundle shape | **Recurring monthly plans** (included allowance + overage at period end) |
| Overage policy | **Per-plan/per-org enum**: `allow` \| `cap` \| `block` |
| Payment rails | **Both, phased**: invoice-only first (Phase A), Stripe card auto-charge later (Phase B) |
| Public host | `https://vr3ai-billing.tapcloud.org/` (to be provisioned; not yet in DNS) |

## 2. Existing foundation (reused, not rebuilt)

- `OrganizationModel` (`api/db/models.py:101-118`) — `quota_type`, `quota_dograh_tokens`,
  `quota_reset_day`, `quota_start_date`, `quota_enabled`, `price_per_second_usd`.
- `OrganizationUsageCycleModel` (`api/db/models.py:594`) — per-period bucket:
  `used_dograh_tokens`, `total_duration_seconds`, `used_amount_usd`, `quota_amount_usd`.
- `OrganizationUsageClient` (`api/db/organization_usage_client.py`) — atomic
  `check_and_reserve_quota` + `update_usage_after_run` (row-locked, concurrency-safe),
  period calculation, usage history/reporting.
- `services/pricing/` — `cost_calculator.calculate_total_cost(usage_info)` returns a per-modality
  `cost_breakdown` (LLM/STT/TTS/telephony USD); `workflow_run_cost.py` builds and persists
  `cost_info` per run.
- **Settlement seam (single hook point):** `apply_workflow_run_usage_to_organization(workflow_run, cost_info)`
  (`api/services/pricing/workflow_run_cost.py:160`) — every run's cost lands here.
- **Gate seams (call-start, 6 call sites):** `check_dograh_quota_*` in `routes/agent_stream.py:70`,
  `routes/public_agent.py:183`, `routes/telephony.py:137`, `routes/telephony.py:739`,
  `routes/campaign.py:553`, `routes/campaign.py:875`. These currently call the **dead**
  hosted-Dograh path (`mps_service_key_client`) and will be replaced.

## 3. Architecture (Approach A — two-tier)

```
                         VR3 on-prem (Dockge)
  +---------------------------------------------------------------+
  |  Dograh API/worker                    Lago stack (new)        |
  |  +------------------+    events   +----------------------+    |
  |  | run settlement   |--outbox---->| api/worker/clock     |    |
  |  | (cost_info)      |             | pg + redis + clickhse|    |
  |  +--------+---------+             |  metrics/plans/      |    |
  |           | writes                |  subscriptions/      |    |
  |           v                       |  wallets/invoices    |    |
  |  +------------------+  reconcile  +---------+------------+    |
  |  | OrganizationUsage|<---pull truth---------+               |
  |  | Cycle (2 meters) |             ^ webhooks (invoice/pay)   |
  |  +--------+---------+-------------+                          |
  |           | reserve (fast gate)                             |
  |           v                                                 |
  |     call-start gate -- allow/cap/block per policy           |
  +---------------------------------------------------------------+
                                   | Phase B: card capture
                                   v
                           Stripe gateway (payments only)
```

- **Lago** = billing truth: plans, overage math, invoices, payment connectors.
- **Local `OrganizationUsageCycle`** = fast real-time gate (Lago cannot sit in the call hot-path:
  async aggregation + adds latency + availability risk).
- **Reconciliation** keeps local in sync with Lago (overwrite, not increment).

### Why not the alternatives
- **Lago-direct (Lago is the only meter):** puts Lago in the per-call hot-path → latency on every
  call setup, and a Lago outage blocks all calls. Rejected for a voice product.
- **Batch-only (push events, bill at period end, no gate):** cannot enforce `cap`/`block` overage
  modes in real time. Rejected because overage policy requires a real-time gate.

## 4. New components — `api/services/billing/`

| Module | Responsibility |
|---|---|
| `lago_client.py` | Thin Lago REST wrapper: events, customers, subscriptions, plans, current-usage fetch. |
| `usage_emitter.py` | Hook in settlement seam; computes meter values; writes outbox rows in-txn. |
| `outbox_drainer.py` | Worker task: drain outbox → `POST /api/v1/events`; idempotent; retry/backoff. |
| `enforcement.py` | Two-meter local reserve + overage policy; replaces dead `quota_service` path. |
| `reconciliation.py` | Periodic: pull Lago truth → overwrite local usage; sync allowances/policy. |
| `provisioning.py` | Org ↔ Lago customer/subscription lifecycle on plan assignment/change. |
| `routes/billing_webhooks.py` | Lago → Dograh: invoice/payment/subscription status → suspend/resume. |

## 5. Data model (Postgres migrations)

`OrganizationModel` adds:

```
lago_customer_id            str   null   # external customer link
billing_plan_code           str   null   # active plan (FREE STRING, not enum)
overage_policy              enum('allow','cap','block')  default 'block'
overage_cap_pct             int   null   # only for 'cap' mode, e.g. 150
included_voice_minutes      int   default 0   # synced from plan by reconciliation
included_ai_tokens          int   default 0   # synced from plan by reconciliation
billing_suspended           bool  default false  # set by webhook on past_due/terminated
```

`OrganizationUsageCycleModel` adds:

```
used_voice_minutes      float  default 0
used_ai_tokens          float  default 0
allowance_voice_minutes int    default 0   # snapshot at cycle start
allowance_ai_tokens     int    default 0   # snapshot at cycle start
```

New `BillingEventOutboxModel` (transactional outbox — at-least-once, idempotent):

```
id, workflow_run_id, metric_code, value,
transaction_id  UNIQUE  = f"{run_id}:{metric_code}"
status enum('pending','sent','failed'), attempts, created_at, sent_at
```

Legacy `quota_dograh_tokens` / `used_dograh_tokens` / `total_duration_seconds` are **retained**
through the transition for back-compat and the existing usage-history UI; deprecated once the
two-meter UI lands.

## 6. Event flow (settlement → Lago)

1. Call ends → existing `calculate_workflow_run_cost(run_id)` builds `cost_info`.
2. **Same DB transaction** as `save_workflow_run_cost_info` + local cycle update:
   - `voice_minutes = call_duration_seconds / 60`
   - `ai_tokens = (LLM + STT + TTS USD) * 100` — **telephony excluded** (pass-through carrier cost,
     already metered as minutes; counting it in both meters would double-bill). `cost_breakdown`
     already separates `telephony_call`, so the split is free.
   - bump local cycle `used_voice_minutes` / `used_ai_tokens`
   - insert 2 outbox rows (`{run_id}:voice_minutes`, `{run_id}:ai_tokens`)
3. `outbox_drainer` worker → `POST /api/v1/events` to Lago; Lago dedupes on `transaction_id`;
   mark `sent`.

**Why an outbox, not a direct POST in the call path:** a synchronous push that fails on a network
blip consumes the customer's minutes but loses the billing record (silent revenue leak). Writing
the event to Postgres in the same transaction that records the run, then draining async, makes
billing emission survive Lago downtime. `transaction_id` dedupe makes retries safe.

## 7. Lago configuration

**Billable metrics:**

```
voice_minutes   aggregation=sum_agg   field=value   rounding=ceil
ai_tokens       aggregation=sum_agg   field=value
```

**Plans** = monthly bundles; one charge per metric using Lago's `package` model
(included `free_units` + per-unit overage):

```
Plan "growth" (monthly, $BASE):
  charge voice_minutes: package { free_units: 1000,      package_size: 1,    amount: $0.04 }
  charge ai_tokens:     package { free_units: 2_000_000, package_size: 1000, amount: $Y }
Plans "starter", "scale", ...: same shape, different numbers
```

Plan numbers live in **Lago** (admin-editable), never hardcoded in Dograh. Reconciliation pulls
`free_units` into `included_*`.

**Overage policy split** — Lago always *bills* overage (package model charges past `free_units`);
the allow/cap/block decision is **local** (Lago cannot block a call):

- `allow` → gate never blocks; Lago invoices all overage.
- `cap`   → gate blocks past `allowance * overage_cap_pct/100`; Lago invoices overage up to where
  calls stopped.
- `block` → gate blocks at `allowance`; Lago overage charge effectively never triggers.

## 8. Enforcement gate (call-start)

Replace the 6 `check_dograh_quota_*` call sites with `enforcement.check_call_allowed(org_id)`:

```
get_or_create_current_cycle(org)
if org.billing_suspended:                 -> DENY "account suspended (payment)"
for meter in (voice_minutes, ai_tokens):
    used      = cycle.used_<meter>
    allowance = cycle.allowance_<meter>
    limit = match org.overage_policy:
        block -> allowance
        cap   -> allowance * overage_cap_pct/100
        allow -> +inf
    if used >= limit:                     -> DENY f"{meter} bundle exhausted"
return ALLOW
```

- Preserves the atomic row-lock pattern from `check_and_reserve_quota`. Cheap (one indexed cycle
  row), safe in the hot path.
- Denials return structured `error_code` (`bundle_exhausted_minutes` / `bundle_exhausted_tokens` /
  `account_suspended`) matching the existing `QuotaCheckResult` contract the call sites consume.

## 9. Reconciliation job (periodic ~15 min + on cycle rollover)

```
for each org with lago_customer_id:
    lago_usage = lago_client.get_current_usage(subscription)   # authoritative
    cycle.used_voice_minutes = lago_usage.voice_minutes        # OVERWRITE drift
    cycle.used_ai_tokens     = lago_usage.ai_tokens
    plan = lago_client.get_plan(org.billing_plan_code)
    org.included_voice_minutes = plan.voice_minutes.free_units # sync allowance
    org.included_ai_tokens     = plan.ai_tokens.free_units
    if new cycle: snapshot allowance_* from current plan
```

Lago is source of truth → **overwrite, not add**. Heals outbox gaps, double-counts, and manual
Lago adjustments. Local is a fast cache of Lago's truth, never an independent ledger.

## 10. Lago → Dograh webhooks (`routes/billing_webhooks.py`)

```
invoice.payment_failure / subscription past_due  -> org.billing_suspended = True
invoice.payment_success / subscription active    -> org.billing_suspended = False
subscription.terminated                          -> suspend + clear plan
```

HMAC-verify Lago's signature. Suspension flips the gate to DENY without touching usage data.

## 11. Adding bundles / extensibility

- **New bundle, same two meters → zero code (pure Lago config).** Create the plan in Lago, assign
  an org to it. Works because `billing_plan_code` is a **free string, not a DB enum** (no migration
  per tier), allowances flow in via reconciliation, and Dograh holds no list of valid bundles.
- **New bundle needing a NEW meter** (e.g. SMS, concurrent-agent seats) → code: new Lago metric +
  outbox emission + `used_/allowance_` columns + gate branch. **The meter set is the extensibility
  boundary** — adding a tier is config; adding a new thing to measure is a small code change.
- **Mid-cycle plan swap** (upgrade/downgrade) → `provisioning` updates the Lago subscription, Lago
  handles proration, reconciliation re-snapshots allowance. "New bundle" includes moving an
  existing customer onto one.
- **Prepaid top-up packs** (deferred option) → Lago wallets layer on additively later
  (provisioning + balance-aware gate branch). Not built now; design leaves room.

## 12. Deployment (Dockge stack on VR3)

Mirror the Langfuse v3 pattern (prod 192.11.0.35, dev/qa 192.11.0.36, ClickHouse already in ops
vocabulary).

```
Dockge stack "lago" (prod) / "lago-dev" (dev/qa)
  services: lago-api, lago-front, lago-worker, lago-clock,
            lago-pg, lago-redis, lago-clickhouse
  secrets (stack .env, not committed): LAGO_RSA_PRIVATE_KEY, LAGO_API_KEY,
            webhook HMAC, encryption secrets

Public host (to provision; not yet in DNS):
  vr3ai-billing.tapcloud.org
    /      -> lago-front (admin UI)
    /api   -> lago-api   (browser-facing API; same cert, path-based)
  TLS: Let's Encrypt via existing nginx flow
  DNS:  new A/CNAME record required

Dograh -> Lago (server-to-server): internal docker/macvlan only
  LAGO_API_URL=http://lago-api:3000   # NEVER the public host
Dograh stack env: LAGO_API_URL, LAGO_API_KEY, LAGO_WEBHOOK_SECRET
```

- Lago gets its **own** Postgres/Redis/ClickHouse (do not share Langfuse's CH — different schemas,
  blast-radius isolation).
- **Two URL planes:** public host serves humans (admin UI + the SPA's browser API calls); Dograh's
  billing traffic stays internal. Do not expose the Lago API publicly merely to reach it from Dograh.
- **Lago front/api URL env footgun** (same shape as the Langfuse `basePath` issue): the SPA bakes
  its API base at boot via `LAGO_API_URL`/`API_URL`/`LAGO_FRONT_URL`; if these don't match the
  public host+path exactly, the UI loads but every API call 404s. Configure explicitly.
- Dev-first: stand up `lago-dev` on .36, validate against QA Dograh, then prod.

## 13. Payment phasing

- **Phase A — invoice-only:** Lago generates invoices, emailed PDF (reuse SMTP relay
  192.11.0.30:25), pay out-of-band (ACH/wire/check), mark paid in Lago. No card data anywhere.
  Unblocks billing immediately, zero PCI scope.
- **Phase B — card auto-charge:** add a Lago → Stripe gateway connector for self-serve accounts.
  Stripe touches **payment data only**, never usage. Enterprise stays on manual invoice. Pure
  addition, no rework of A.

## 14. Rollout sequence (each step shippable + reversible)

```
0. Migrations: add columns/outbox (nullable, dormant) — no behavior change (expand/contract)
1. Stand up lago-dev; define metrics + 1 test plan
2. usage_emitter + outbox + drainer -> events flow to Lago (SHADOW: emit only, gate unchanged)
3. Reconciliation + allowance sync -> verify Lago totals == local on live traffic
4. Flip enforcement: replace check_dograh_quota_* with check_call_allowed
   (override overage_policy='allow' during observation -> never blocks;
    schema default is 'block', tighten per-org in step 6)
5. Webhooks + billing_suspended
6. Per-org: set real plan, switch policy to cap/block
7. Prod: lago stack, migrate orgs, go live (invoice-only)
8. Phase B: Stripe gateway
```

**Shadow mode (steps 2→4) is the risk killer:** billing emission and call-gating are decoupled in
the rollout, so Lago's numbers are validated against real traffic before any gate can deny a call.
A billing bug then costs accuracy, never a blocked customer.

## 15. Testing

- **Unit:** `ai_tokens` normalization (telephony excluded); overage math per policy
  (allow/cap/block boundary conditions); outbox idempotency (duplicate `transaction_id` → one Lago
  event).
- **Integration** (local Lago via `docker-compose-local`): run settles → event lands → invoice
  reflects it; reconciliation heals an injected drift; webhook flips `billing_suspended`.
- Reuse existing pytest harness: `cd api && .venv/Scripts/python.exe -m pytest` (local PG+Redis via
  `docker-compose-local`).

## 16. Open items for the implementation plan

- Concrete plan tiers + numbers (starter/growth/scale: base price, included minutes/tokens,
  overage rates) — config in Lago, owned by business; design does not fix the numbers.
- Lago version pin + exact compose (model on Langfuse v3 floating-tag approach).
- Outbox drainer cadence + retry/backoff + dead-letter handling.
- Reconciliation cadence vs cost; whether to also reconcile on-demand at gate when local looks stale.
- Migration/backfill: map existing orgs' `quota_dograh_tokens` → initial plan assignment.
- Admin/UI surface for plan assignment + usage-vs-allowance view (minimal first).
