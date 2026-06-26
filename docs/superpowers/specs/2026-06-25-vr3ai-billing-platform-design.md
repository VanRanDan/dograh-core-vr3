# VR3AI Billing Platform — Design

- **Date:** 2026-06-25
- **Repo:** dograh-core-vr3 (VanRan fork)
- **Status:** Approved design (rev 2 — folds in code-review fixes), pre-implementation-plan
- **Goal:** Track per-customer agent usage minutes and AI cost, package them into recurring bundles, and bill overage — using a self-hosted billing engine, keeping usage/customer data on-prem.

## 1. Summary

VR3AI already meters usage (the dograh-token cycle survived the billing removal). What is
missing is the **money layer**: bundle/plan definitions, overage billing, invoicing, and payment.

This design adds that layer by integrating **Lago** (self-hosted, on VR3 Dockge infra) as the
billing source-of-truth, while keeping a **local fast meter** for real-time call gating. Two
billable meters — `voice_minutes` and `ai_cost_cents` — back recurring monthly bundle plans with
configurable overage policy.

### Decisions locked (brainstorming)

| Decision | Choice |
| --- | --- |
| Billing engine | **Self-hosted Lago** (on-prem; data does not leave VR3) |
| Integration pattern | **Two-tier**: local gate + Lago billing truth + reconciliation (Approach A) |
| Pricing units | **Two separate meters**: `voice_minutes` + `ai_cost_cents` |
| `ai_cost_cents` scope | **All AI units** (LLM + STT + TTS) summed as USD cents via existing `cost_calculator`; telephony excluded |
| Bundle shape | **Recurring monthly plans** (included allowance + overage at period end) |
| Overage policy | **Per-plan/per-org enum**: `allow` \| `cap` \| `block` |
| Payment rails | **Both, phased**: invoice-only first (Phase A), Stripe card auto-charge later (Phase B) |
| Public host | `https://vr3ai-billing.tapcloud.org/` (to be provisioned; not yet in DNS) |

### Rev-2 changes (from code review)

1. **Gate reserves, not just checks** — call-start atomically reserves an estimate; settlement
   corrects to actual. Prevents cap/block overshoot under concurrent/long calls (§8).
2. **Round minutes once, at the invoice** — meter stores fractional minutes; no per-event ceil
   (per-event ceil systematically over-bills) (§6/§7).
3. **Enforcement is provisioned-only** — gate enforces only when the org has a Lago customer +
   allowance; un-provisioned orgs fall through to ALLOW so the rollout can't brick existing orgs (§8/§14).
4. **Single-session atomicity** — cost_info save + reservation settle + outbox insert share one DB
   transaction (§6).
5. **Meter renamed** `ai_tokens` → `ai_cost_cents` — unit is USD cents (1 unit = $0.01), not tokens (§7).
6. **Versioned `transaction_id`** so a cost recompute emits a correcting event instead of a deduped
   no-op (§6).
7. **Reconciliation never lowers the open period below local pending** — `max(local, lago)` for the
   open cycle, hard-overwrite only closed cycles (§9).

## 2. Existing foundation (reused, not rebuilt)

- `OrganizationModel` (`api/db/models.py:93-118`) — `quota_type`, `quota_dograh_tokens`,
  `quota_reset_day`, `quota_start_date`, `quota_enabled`, `price_per_second_usd`.
  Column style: `Column(Integer, nullable=False, default=0, server_default=text("0"))`;
  enums via `Enum("a","b", name="...")` + `server_default=text("'a'::name")`.
- `OrganizationUsageCycleModel` (`api/db/models.py:594`) — per-period bucket:
  `used_dograh_tokens`, `total_duration_seconds`, `used_amount_usd`, `quota_amount_usd`,
  `UniqueConstraint(organization_id, period_start, period_end)`.
- `OrganizationUsageClient` (`api/db/organization_usage_client.py`) — atomic
  `check_and_reserve_quota` (row-locked, `with_for_update`) + `update_usage_after_run`,
  period calculation, usage history/reporting. **The reservation pattern to mirror per-meter.**
- `services/pricing/cost_calculator.py` — `calculate_total_cost(usage_info)` returns
  `{"llm_cost", "tts_cost", "stt_cost", "total"}` (USD floats). Telephony is added separately
  by `workflow_run_cost.py` as `telephony_call` / `{provider}_call` — NOT inside llm/tts/stt.
- **Settlement seam (single hook point):** `calculate_workflow_run_cost(workflow_run_id)`
  (`api/services/pricing/workflow_run_cost.py:198`) → builds `cost_info`, saves it, and calls
  `apply_workflow_run_usage_to_organization`. Invoked from the `process_workflow_completion`
  arq task (`api/tasks/s3_upload.py:174`). **Async, post-call.**
- **Gate seams (call-start, 6 call sites):** `check_dograh_quota_*` in `routes/agent_stream.py:70`,
  `routes/public_agent.py:183`, `routes/telephony.py:137`, `routes/telephony.py:739`,
  `routes/campaign.py:553`, `routes/campaign.py:875`. Currently call the **dead** hosted-Dograh
  path (`mps_service_key_client`). Return contract: `QuotaCheckResult(has_quota, error_message, error_code)`.
- **Background jobs:** `arq` (Redis) — `api/tasks/arq.py` `WorkerSettings.functions` + `cron_jobs`
  (currently `[]`). New periodic jobs (outbox drainer, reconciliation) register as `cron_jobs`.
- **DB clients:** `BaseDBClient` (`api/db/base_client.py`) opens its **own** `async_session` per
  method. The `db_client` facade (`api/db/db_client.py`) aggregates clients. New billing methods
  that must be atomic with each other MUST accept and share a caller-passed `session`.

## 3. Architecture (Approach A — two-tier)

```text
                         VR3 on-prem (Dockge)
  +---------------------------------------------------------------+
  |  Dograh API/worker                    Lago stack (new)        |
  |  +------------------+    events   +----------------------+    |
  |  | run settlement   |--outbox---->| api/worker/clock     |    |
  |  | (cost_info)      |             | pg + redis + clickhse|    |
  |  +--------+---------+             |  metrics/plans/      |    |
  |           | settle reservation    |  subscriptions/      |    |
  |           v                       |  wallets/invoices    |    |
  |  +------------------+  reconcile  +---------+------------+    |
  |  | OrganizationUsage|<---pull truth---------+               |
  |  | Cycle (2 meters) |             ^ webhooks (invoice/pay)   |
  |  +--------+---------+-------------+                          |
  |           | reserve+gate (fast, strong-consistent)          |
  |           v                                                 |
  |     call-start gate -- allow/cap/block per policy           |
  +---------------------------------------------------------------+
                                   | Phase B: card capture
                                   v
                           Stripe gateway (payments only)
```

- **Lago** = billing truth: plans, overage math, invoices, payment connectors.
- **Local `OrganizationUsageCycle`** = fast, strongly-consistent gate. Lago cannot sit in the call
  hot-path (async aggregation + latency + availability). The gate **reserves** at call-start;
  settlement corrects to actual.
- **Reconciliation** syncs local to Lago (`max` for open period, overwrite for closed).

### Why not the alternatives

- **Lago-direct (Lago is the only meter):** puts Lago in the per-call hot-path → latency on every
  call setup, and a Lago outage blocks all calls. Rejected for a voice product.
- **Batch-only (push events, bill at period end, no gate):** cannot enforce `cap`/`block` overage
  modes in real time. Rejected because overage policy requires a real-time gate.

## 4. New components — `api/services/billing/`

| Module | Responsibility |
| --- | --- |
| `lago_client.py` | Thin Lago REST wrapper: events, customers, subscriptions, plans, current-usage fetch. |
| `usage_emitter.py` | In settlement seam: compute meter values; settle reservation; write outbox rows (shared session). |
| `outbox_drainer.py` | arq cron job: drain outbox → `POST /api/v1/events`; idempotent; retry/backoff/dead-letter. |
| `enforcement.py` | Per-meter atomic **reserve**-and-gate + overage policy; replaces dead `quota_service` path. |
| `reconciliation.py` | arq cron job: pull Lago truth → sync local usage (`max`/overwrite) + allowances/policy. |
| `provisioning.py` | Org ↔ Lago customer/subscription lifecycle on plan assignment/change. |
| `estimator.py` | `estimate_units(workflow) -> (voice_minutes, ai_cost_cents)` — conservative call-start reservation. |
| `routes/billing_webhooks.py` | Lago → Dograh: invoice/payment/subscription status → suspend/resume. |

## 5. Data model (Postgres migrations)

`OrganizationModel` adds:

```text
lago_customer_id            str   null   # external customer link; NULL = un-provisioned
billing_plan_code           str   null   # active plan (FREE STRING, not enum)
overage_policy              enum('allow','cap','block')  default 'block'
overage_cap_pct             int   null   # only for 'cap' mode, e.g. 150
included_voice_minutes      int   default 0   # synced from plan by reconciliation
included_ai_cost_cents      int   default 0   # synced from plan by reconciliation
billing_suspended           bool  default false  # set by webhook on past_due/terminated
```

`OrganizationUsageCycleModel` adds:

```text
used_voice_minutes      float  default 0   # fractional minutes (reserved + settled)
used_ai_cost_cents      float  default 0   # USD cents (reserved + settled)
allowance_voice_minutes int    default 0   # snapshot at cycle start
allowance_ai_cost_cents int    default 0   # snapshot at cycle start
```

New `BillingEventOutboxModel` (transactional outbox — at-least-once, idempotent):

```text
id, workflow_run_id, metric_code, value, recompute_seq (int, default 0),
transaction_id  UNIQUE  = f"{run_id}:{metric_code}:{recompute_seq}"
status enum('pending','sent','failed'), attempts, last_error, created_at, sent_at
```

Legacy `quota_dograh_tokens` / `used_dograh_tokens` / `total_duration_seconds` are **retained**
through the transition for back-compat and the existing usage-history UI; deprecated once the
two-meter UI lands.

## 6. Event flow (settlement → Lago)

1. Call ends → `process_workflow_completion` arq task → `calculate_workflow_run_cost(run_id)` builds
   `cost_info` (`{llm_cost, tts_cost, stt_cost, total, telephony_call?, call_duration_seconds, ...}`).
2. **Single DB transaction (one session threaded through):**
   - `voice_minutes = call_duration_seconds / 60`  — **fractional; never ceil here** (round at invoice).
   - `ai_cost_cents = (llm_cost + tts_cost + stt_cost) * 100` — **telephony excluded** (carrier
     pass-through, already metered as minutes; counting it in both meters double-bills).
   - **settle the call-start reservation to actual** on the local cycle:
     `used_<meter> += (actual - reserved_estimate)` (the estimate was added at call-start).
   - save `cost_info`.
   - insert 2 outbox rows (`{run_id}:voice_minutes:{seq}`, `{run_id}:ai_cost_cents:{seq}`).
   All four succeed or roll back together — a crash can't bill without recording, or vice-versa.
3. `outbox_drainer` cron → `POST /api/v1/events` to Lago; Lago dedupes on `transaction_id`;
   mark `sent`. On recompute with changed values, bump `recompute_seq` → new `transaction_id` →
   Lago records a correcting event instead of silently deduping.

**Why an outbox, not a direct POST in the call path:** a synchronous push that fails on a network
blip consumes the customer's minutes but loses the billing record (silent revenue leak). Writing
the event to Postgres in the same transaction that records the run, then draining async, makes
billing emission survive Lago downtime. `transaction_id` dedupe makes retries safe.

## 7. Lago configuration

**Billable metrics:**

```text
voice_minutes   aggregation=sum_agg   field=value   # fractional minutes; round at charge, NOT per event
ai_cost_cents   aggregation=sum_agg   field=value   # unit = USD cents (1 unit = $0.01)
```

**Plans** = monthly bundles; one charge per metric using Lago's `package` model
(included `free_units` + per-unit overage). **Numbers below are illustrative placeholders** —
real tiers are business-owned and live in Lago:

```text
Plan "growth" (monthly, $BASE):
  charge voice_minutes: package { free_units: 1000,   package_size: 1,  amount: $0.04/min }
  charge ai_cost_cents: package { free_units: 50_000, package_size: 1,  amount: $0.012/cent }
     # free_units 50_000 cents = $500 of included AI cost; overage marks up each cent past it
Plans "starter", "scale", ...: same shape, different numbers
```

Plan numbers live in **Lago** (admin-editable), never hardcoded in Dograh. Reconciliation pulls
`free_units` into `included_*`.

**Overage policy split** — Lago always *bills* overage (package charges past `free_units`); the
allow/cap/block decision is **local** (Lago cannot block a call):

- `allow` → gate never blocks; Lago invoices all overage.
- `cap`   → gate blocks past `allowance * overage_cap_pct/100`; Lago invoices overage up to where
  calls stopped.
- `block` → gate blocks at `allowance`; Lago overage charge effectively never triggers.

## 8. Enforcement gate (call-start) — reserve + gate

Replace the 6 `check_dograh_quota_*` call sites with `enforcement.check_and_reserve(org_id, workflow_id)`:

```text
if org.lago_customer_id is None:          -> ALLOW   # un-provisioned: billing not active for this org
if org.billing_suspended:                 -> DENY  error_code=account_suspended
cycle = get_or_create_current_cycle(org)
est_minutes, est_cents = estimate_units(workflow)     # conservative floor, e.g. 1 min + 25c
for (meter, est) in [(voice_minutes, est_minutes), (ai_cost_cents, est_cents)]:
    limit = match org.overage_policy:
        block -> cycle.allowance_<meter>
        cap   -> cycle.allowance_<meter> * org.overage_cap_pct / 100
        allow -> +inf
    # atomic, row-locked (mirror check_and_reserve_quota): succeed only if
    #   used_<meter> + est <= limit, then used_<meter> += est
    if not reserve(cycle, meter, est, limit):
        rollback any reservation already taken this call
        -> DENY  error_code in {bundle_exhausted_minutes, bundle_exhausted_ai_cost}
return ALLOW
```

- Mirrors the existing atomic `with_for_update` reserve. Cheap (one indexed cycle row), safe in the
  hot path.
- Settlement (§6) corrects `used_<meter>` from the reserved estimate to the actual amount, so the
  reservation is self-healing whether the call ran long or short.
- Denials map to the existing `QuotaCheckResult(has_quota=False, error_code=...)` contract the call
  sites already consume — surface a clear message to telephony/UI.

## 9. Reconciliation job (arq cron ~15 min + on cycle rollover)

```text
for each org with lago_customer_id:
    lago_usage = lago_client.get_current_usage(subscription)   # authoritative
    if open period:                       # never drop below local pending/reserved
        cycle.used_voice_minutes = max(cycle.used_voice_minutes, lago_usage.voice_minutes)
        cycle.used_ai_cost_cents = max(cycle.used_ai_cost_cents, lago_usage.ai_cost_cents)
    else:                                 # closed period: Lago is final truth
        cycle.used_voice_minutes = lago_usage.voice_minutes
        cycle.used_ai_cost_cents = lago_usage.ai_cost_cents
    plan = lago_client.get_plan(org.billing_plan_code)
    org.included_voice_minutes = plan.voice_minutes.free_units
    org.included_ai_cost_cents = plan.ai_cost_cents.free_units
    if new cycle: snapshot allowance_* from current plan
```

`max` on the open period prevents the async drain/aggregation lag from resetting `used_*` *down*
(which would make the gate too lenient). Closed periods hard-overwrite to Lago's final number.
Local is a fast cache of Lago's truth, never an independent ledger.

## 10. Lago → Dograh webhooks (`routes/billing_webhooks.py`)

```text
invoice.payment_failure / subscription past_due  -> org.billing_suspended = True
invoice.payment_success / subscription active    -> org.billing_suspended = False
subscription.terminated                          -> suspend + clear plan
```

HMAC-verify Lago's signature. Suspension flips the gate to DENY without touching usage data.
**Note:** payment_* webhooks only fire once an automated gateway exists (Phase B). In Phase A
(manual invoicing), `billing_suspended` is set manually; subscription.* webhooks still apply.

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

```text
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

**Telephony cost note:** carrier (Twilio/Vonage) per-minute cost is a pass-through not billed on
either meter directly — it must be recovered by setting the `voice_minutes` overage/base rate above
carrier cost. Confirm rate-setting covers carrier margin when defining plan numbers.

## 14. Rollout sequence (each step shippable + reversible)

```text
0. Migrations: add columns/outbox (nullable, dormant) — no behavior change (expand/contract)
1. Stand up lago-dev; define metrics + 1 test plan
2. usage_emitter + outbox + drainer -> events flow to Lago (SHADOW: emit only, gate unchanged)
3. Reconciliation + allowance sync -> verify Lago totals == local on live traffic
4. Add enforcement.check_and_reserve at the 6 sites, BUT gate is provisioned-only
   (lago_customer_id None -> ALLOW), so existing un-provisioned orgs are unaffected
5. Webhooks + billing_suspended
6. Per-org go-live: provision Lago customer + subscription, backfill allowance_*,
   THEN set overage_policy (cap/block). Order matters — allowance before policy.
7. Prod: lago stack, migrate orgs, go live (invoice-only)
8. Phase B: Stripe gateway
```

**Shadow mode (steps 2→4) is the risk killer:** billing emission and call-gating are decoupled in
the rollout, so Lago's numbers are validated against real traffic before any gate can deny a call.
**Provisioned-only enforcement (step 4)** means flipping the gate on cannot brick any org that
hasn't been explicitly provisioned + allowance-backfilled in step 6.

## 15. Testing

- **Unit:** `ai_cost_cents` normalization (telephony excluded); fractional-minute math (no per-event
  ceil); overage gate per policy (allow/cap/block boundary conditions); **reservation** (reserve at
  start, settle delta at end, short/long call); outbox idempotency (duplicate `transaction_id` → one
  Lago event; bumped `recompute_seq` → correcting event); provisioned-only gate (NULL customer → ALLOW).
- **Integration** (local Lago via `docker-compose-local`): run settles → event lands → invoice
  reflects it; reconciliation `max` does not lower open-period usage; reconciliation overwrites a
  closed period; webhook flips `billing_suspended`.
- Reuse existing pytest harness: `cd api && .venv/Scripts/python.exe -m pytest` (local PG+Redis via
  `docker-compose-local`).

## 16. Open items for the implementation plan

- Concrete plan tiers + numbers (starter/growth/scale: base price, included minutes/cents, overage
  rates) — config in Lago, business-owned; this design does not fix the numbers.
- `estimate_units` reservation floor (fixed vs workflow-derived) — start with a conservative fixed
  floor (e.g. 1 voice-minute + 25 cents), tune later.
- Lago version pin + exact compose (model on the Langfuse v3 floating-tag approach).
- Outbox drainer cadence + retry/backoff + dead-letter handling.
- Reconciliation cadence vs cost; whether to also reconcile on-demand at the gate when local looks stale.
- Migration/backfill: map existing orgs' `quota_dograh_tokens` → initial plan assignment + allowance_*.
- Admin/UI surface for plan assignment + usage-vs-allowance view (minimal first).
