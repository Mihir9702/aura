# Implementation status — 2026-09-10

The owner authorized implementation. This run delivers the first runnable M0/M1 foundation, not a comprehensive Aura implementation or completed milestone acceptance.

**Paused 2026-09-23.** The owner stopped development and prepared the repository for public release as a personal project. The table below is the final state. "Next slice" records what would follow if work resumed. Re-verified that day: Ruff, strict mypy, 13 domain tests, 11 PostgreSQL integration tests and the production web build passed. The browser suite was not re-run.

| Area | Implemented | Remaining |
|---|---|---|
| Runtime | Python 3.12/uv, FastAPI, SQLAlchemy/Alembic, PostgreSQL 17; React/TS/Vite/Tailwind; lockfiles; Windows scripts | Hosted auth/runtime, CI, production privileges |
| Local security | Random owner key, hashed session tokens/expiry/revocation, HttpOnly SameSite cookie, command/origin checks, loopback, basic login throttling | Hosted HTTPS/Secure cookies, shared throttling/hardening |
| Ledger | $500 funding, balanced immutable transaction-sealed journals, decimal primitives | Production settlement/fee/lot policies; corrections/rebuild/corporate actions |
| Fixture execution | SHADOW-only reservations/fractional FIFO fills; duplicate/conflicting IDs; unknown state retains capacity; no blind resubmit | Real simulator/adapter, durable receipt quarantine, full cancel/reconnect semantics |
| Controls | Persistent Entry Halt/Full Kill, expected-version/idempotent owner commands, audit; pure health-gate function | Real probes, automatic trigger policy, cancellation delivery, verified human re-arm |
| Market data fixture | `synthetic-us-equities-v1`, a SYNTHETIC_FIXTURE dataset (not market data): 12 fictional instruments, 380 FIXTURE_WEEKDAY_V1 sessions, 4,388 unadjusted daily bars, point-in-time reference files and 13 declared traps. The deterministic SplitMix64/Decimal generator regenerates byte-identical files; 30 tests check hashes, OHLC validity and every trap ([ADR-0024](adr/0024-synthetic-fixture-market-data.md)) | Real provider selection and qualification (OD-04), exchange calendar (OD-03), corporate-action support, Parquet publication |
| Risk/strategy/regime | Capacity sizing, five unimplemented registry entries, lifecycle and temporal validators | Full risk rules, numeric profiles, persistent scoped qualification/activation, methodologies and regime computation |
| Events | Outbox/inbox acknowledgement with commit-order-gap test | Scheduler, substantive consumers, dead-letter/redrive, operational SLIs |
| UI/contracts | Authenticated overview, Ledger records, readiness and controls; SSE/refetch; generated Overview schema/types | Full proposal/Committee/order/report/research flows; ADIYA-informed refinement |
| Research/knowledge | Explicit unavailable states and architectural specifications | Backtesting, League metrics, corpus/retrieval, prospective strategy evaluation |

## Verified

Local startup correction (2026-09-11): root `npm run dev` now starts PostgreSQL, waits for API/database readiness, and launches worker and web. Previously it launched only Vite, causing refused API connections at sign-in. Frontend-only startup is now explicitly `dev:web`; non-JSON proxy failures display recovery guidance.

Correction verification: backend-only launcher reached API/database readiness against the existing local database and web server. TypeScript/Vite build passed; a Chrome regression test verified the empty proxy-error response, recovery guidance, real owner-key sign-in, $500 overview and logout. A separate browser check confirmed the sign-in page rendered without browser errors.

13 domain tests and 11 real PostgreSQL integration tests passed. Chrome browser verification passed login, $500 state, navigation, SSE, reversible Entry Halt, mobile width and logout without page exceptions. Ruff, strict mypy, TypeScript checks and Vite production build passed. A disposable PostgreSQL restore verified balanced journals, unique funding and migration 0002.

These results do not imply all 32 design acceptance cases pass. Test dependencies emit upstream Starlette/httpx deprecation warnings. One mixed sandbox/user pytest-cache warning occurred; current integration commands disable that cache.

## Explicit assumptions and limitations

`FIFO_FEE_EXPENSE_IMMEDIATE_V1` is SHADOW-only and does not settle OD-11. Foundation decimals support at most eight fractional places and absolute values below 10^12; unsupported precision is rejected. Tests provide quantity increments, fees and price bounds. Real capabilities and approved production accounting rules are still required.

Conflicting identities, out-of-bound fixture fills and oversells raise an error and roll back; this is not a production durable receipt quarantine. No active execution endpoint, mode-switch endpoint, live adapter or model tool exists. Full Kill release is unavailable until dependency/reconciliation evidence can be checked. No cancellation capability is fabricated.

## Next slice

Persist version/horizon-specific qualification and admission races; build durable receipt/quarantine/replay, policy-configured accounting corrections and substantive scheduler consumers. Before real Observe analysis choose an approved data/strategy slice. Active-paper entries require actual qualification/activation and unresolved exit/risk/accounting/adapter policies. Vendors, model names, prices, budgets and final numeric limits remain open.
