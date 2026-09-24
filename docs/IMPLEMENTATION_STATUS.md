# Implementation status — 2026-09-10

The owner authorized implementation. This run delivers the first runnable M0/M1 foundation, not a comprehensive Aura implementation or completed milestone acceptance.

**Paused 2026-09-23.** The owner stopped development and prepared the repository for public release as a personal project. The table below is the final state. "Next slice" records what would follow if work resumed. Re-verified that day: Ruff, strict mypy, 13 domain tests, 11 PostgreSQL integration tests and the production web build passed. The browser suite was not re-run.

**Resumed 2026-09-23.** The owner resumed implementation the same day for a pass that uses a synthetic fixture market-data source, with no vendor. Paper-only execution and Observe as the only mode are unchanged. The pause note above is kept as history; the table below tracks the resumed pass.

| Area | Implemented | Remaining |
|---|---|---|
| Runtime | Python 3.12/uv, FastAPI, SQLAlchemy/Alembic, PostgreSQL 17; React/TS/Vite/Tailwind; lockfiles; Windows scripts; parallel-safe integration databases (see [test harness](#test-harness)) | Hosted auth/runtime, CI, production privileges |
| Local security | Random owner key, hashed session tokens/expiry/revocation, HttpOnly SameSite cookie, command/origin checks, loopback, basic login throttling | Hosted HTTPS/Secure cookies, shared throttling/hardening |
| Ledger | $500 funding, balanced immutable transaction-sealed journals, decimal primitives | Production settlement/fee/lot policies; corrections/rebuild/corporate actions |
| Fixture execution | SHADOW-only reservations/fractional FIFO fills; duplicate/conflicting IDs; unknown state retains capacity; no blind resubmit | Real simulator/adapter, durable receipt quarantine, full cancel/reconnect semantics |
| Controls | Persistent Entry Halt/Full Kill, expected-version/idempotent owner commands, audit; pure health-gate function | Real probes, automatic trigger policy, cancellation delivery, verified human re-arm |
| Risk/strategy/regime | Capacity sizing, five unimplemented registry entries, lifecycle and temporal validators | Full risk rules, numeric profiles, persistent scoped qualification/activation, methodologies and regime computation |
| Events | Outbox/inbox acknowledgement with commit-order-gap test; event feed returns the newest 100 events, newest first | Scheduler, substantive consumers, dead-letter/redrive, operational SLIs |
| Orchestration | Durable jobs (migration `0003_durable_jobs`): occurrence key, work class, scope, priority, deadline, attempts under an explicit per-job bound, lease and fencing token. Claims use `FOR UPDATE SKIP LOCKED` and keep reserved capacity for reconciliation and monitoring work; handler writes commit with completion under the fence, so an expired or superseded lease commits nothing. Bounded jittered retries, visible dead letters and deadline expiry; owner reads `GET /api/jobs` and `GET /api/jobs/dead-letters`, and an audited, idempotent `POST /api/jobs/{job_id}/redrive` that keeps job identity. The worker runs jobs after each inbox batch, including under Entry Halt and Full Kill | Recurring schedules (OD-03): nothing is enqueued automatically and no production job kind is registered. Heartbeats, external-effect jobs, queue-length bounds and coalescing, queue metrics, approved lease/backoff/capacity values (only `*_DEV` values exist) |
| UI/contracts | Authenticated overview, Ledger records, readiness and controls; hash-linked pages; SSE/refetch; API routes split into `aura.routes` routers; generated Overview, JournalRecord and EventRecord schemas/types with an OpenAPI drift test; decimal money display without float conversion; Vitest units for formatting and routes; activity panel shows the newest five events | Full proposal/Committee/order/report/research flows; ADIYA-informed refinement |
| Research/knowledge | Explicit unavailable states and architectural specifications | Backtesting, League metrics, corpus/retrieval, prospective strategy evaluation |

## Test harness

- `scripts/test-integration.ps1` migrates and tests a dedicated loopback database named by `AURA_TEST_DATABASE_NAME` (default `aura_test`; it must match `aura_test(_[a-z0-9]+)?`, so the development database `aura` can never be selected). Worktrees sharing the PostgreSQL cluster on port 55432 can run integration tests at the same time with different names, for example `$env:AURA_TEST_DATABASE_NAME = 'aura_test_s00'`. `-Fresh` drops and recreates that database before migrating.
- The shared `db` fixture in `tests/conftest.py` truncates every public table except `alembic_version`, then re-seeds the global gate and the challenge and SHADOW fixture funding. Integration tests use it instead of their own TRUNCATE lists and are marked `integration`.
- Unit tests guard repository invariants: Alembic has a single head; `app.openapi()` equals the committed `packages/contracts/openapi.json` (regenerate with `scripts/contracts.ps1`); every route except `GET /api/health` and `POST /api/session` requires the owner session.
- `scripts/backup_restore_check.py` checks the restored database against Alembic's head rather than a fixed revision.

## Verified

Harness slice (2026-09-23): Ruff, strict mypy, 16 unit tests (13 domain, 3 repository guards) and the production web build passed. 13 PostgreSQL integration tests passed on `aura_test`, again with `-Fresh`, on `aura_test_s00`, and as two concurrent runs on different names. The restore drill verified migration 0002 as read from Alembic. 45 API requests matched the previous single-file app on status, headers, cookies and bodies; the only differences were the two `/api/events` responses, which now list events in reverse (newest-first) order. The browser suite was not run.

Jobs slice (2026-09-23): Ruff, strict mypy, 31 unit tests (15 new orchestration rules), 14 web unit tests and the production web build passed. 33 PostgreSQL integration tests (20 new) passed on `aura_test_s06` with `-Fresh`, including a worker process killed mid-attempt, stale-token and expired-lease commits, a stalled attempt skipped by `SKIP LOCKED`, API redrive of a dead letter and concurrent claims against reserved capacity. Removing each protection in turn (skip-locked claims, the claim lock, the safety reservation, the token check or increment, the lease-expiry check, the claim sweep, occurrence deduplication, the redrive state check and attempt reset) made its test fail. The restore drill was not run: it reads the development database, which stays at migration 0002 until it is migrated.

Local startup correction (2026-09-11): root `npm run dev` now starts PostgreSQL, waits for API/database readiness, and launches worker and web. Previously it launched only Vite, causing refused API connections at sign-in. Frontend-only startup is now explicitly `dev:web`; non-JSON proxy failures display recovery guidance.

Correction verification: backend-only launcher reached API/database readiness against the existing local database and web server. TypeScript/Vite build passed; a Chrome regression test verified the empty proxy-error response, recovery guidance, real owner-key sign-in, $500 overview and logout. A separate browser check confirmed the sign-in page rendered without browser errors.

13 domain tests and 11 real PostgreSQL integration tests passed. Chrome browser verification passed login, $500 state, navigation, SSE, reversible Entry Halt, mobile width and logout without page exceptions. Ruff, strict mypy, TypeScript checks and Vite production build passed. A disposable PostgreSQL restore verified balanced journals, unique funding and migration 0002.

These results do not imply all 32 design acceptance cases pass. Test dependencies emit upstream Starlette/httpx deprecation warnings. One mixed sandbox/user pytest-cache warning occurred; current integration commands disable that cache.

## Explicit assumptions and limitations

`FIFO_FEE_EXPENSE_IMMEDIATE_V1` is SHADOW-only and does not settle OD-11. Foundation decimals support at most eight fractional places and absolute values below 10^12; unsupported precision is rejected. Tests provide quantity increments, fees and price bounds. Real capabilities and approved production accounting rules are still required.

Conflicting identities, out-of-bound fixture fills and oversells raise an error and roll back; this is not a production durable receipt quarantine. No active execution endpoint, mode-switch endpoint, live adapter or model tool exists. Full Kill release is unavailable until dependency/reconciliation evidence can be checked. No cancellation capability is fabricated.

## Next slice

Persist version/horizon-specific qualification and admission races; build durable receipt/quarantine/replay, policy-configured accounting corrections and substantive scheduler consumers. Before real Observe analysis choose an approved data/strategy slice. Active-paper entries require actual qualification/activation and unresolved exit/risk/accounting/adapter policies. Vendors, model names, prices, budgets and final numeric limits remain open.
