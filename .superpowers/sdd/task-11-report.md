# Task 11 Report: Reconciliation Cron

## Open-period max logic
`reconcile_org` applies `max(local, lago)` on both meters independently. This ensures locally-buffered events that haven't been acked by Lago are never silently discarded. Lago's value wins when it exceeds local, bringing late-settling events into the local cycle without waiting for the next drainer pass.

## Allowance sync
When `org.billing_plan_code` is set, `get_plan(plan_code)` is called and:
- `org.included_voice_minutes` / `org.included_ai_cost_cents` are always updated to the plan's authoritative free units.
- `cycle.allowance_voice_minutes` / `cycle.allowance_ai_cost_cents` are only set if currently 0 (initialisation, not re-sync), to avoid overwriting admin overrides.

## Why closed-period is deferred
`_get_or_create_current_cycle_impl` always returns the current open cycle. When a period rolls over, the old cycle is no longer returned, so this job never touches it. Closed-period finalisation (writing Lago's terminal total onto the closed cycle) is a separate concern scoped to cycle rollover (Plan 2) and is not implemented here.

## Cron registration
- `reconcile_billing` imported in `billing_tasks.py` (re-exported via `reconcile_billing` from `reconciliation.py`).
- Added to `WorkerSettings.functions` and `cron_jobs` in `arq.py`: `cron(reconcile_billing, minute={0, 15, 30, 45})` — fires at :00, :15, :30, :45 every hour.
- Existing `drain_billing_outbox` cron is unchanged.

## TDD RED → GREEN
- Wrote 7 tests in `api/tests/billing/test_reconciliation.py` covering: max rule (lower-Lago not reduced, higher-Lago raised), allowance init, allowance non-overwrite, no-plan skip, `reconcile_billing` count, lago_id filter, per-org error resilience.
- RED: confirmed module-not-found before implementation.
- GREEN: 7/7 pass after implementation; full billing suite (44 tests) remains green.

## Files changed
- **Created**: `api/services/billing/reconciliation.py`
- **Modified**: `api/tasks/billing_tasks.py` (import re-export)
- **Modified**: `api/tasks/arq.py` (register function + cron)
- **Created**: `api/tests/billing/test_reconciliation.py`

## Self-review
- Used `db_client.async_session()` and `db_client._get_or_create_current_cycle_impl()` as instructed — test harness `db_session` patch routes through correctly.
- Row-lock via `.with_for_update()` matches existing pattern in `update_usage_after_run`.
- `reconcile_billing` loads all orgs into memory before iterating; acceptable for current org counts, but a cursor/batch approach should replace this if orgs grow to thousands.

## Concerns
- **Memory**: `reconcile_billing` loads all provisioned orgs at once. Not a concern at present scale.
- **No retry**: failed orgs are logged and skipped; there is no dead-letter or backoff. A future improvement could add a `reconcile_failures` counter to the outbox or a dedicated retry queue.
- **Closed-period gap**: until Plan 2 (cycle rollover pass) is implemented, a closed cycle's final Lago total is never written back. This is acceptable in the current billing shadow-mode phase.
