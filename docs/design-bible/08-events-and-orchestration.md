# Event architecture and orchestration

## Delivery model — Adopted baseline

Use PostgreSQL transactional outbox plus durable consumer inbox/jobs within the monolith. Commit domain changes and event row together. A dispatcher claims batches with leases; publishing/completing a job can repeat after a crash. Consumer effects and inbox deduplication commit together when local. External effects use their own persisted intent/idempotency/reconciliation protocol. No promise of end-to-end exactly-once delivery.

Ordering is per aggregate via expected version/sequence, not global timestamp order. Events can duplicate, arrive late, or precede dependent projections. Consumers either wait/retry missing dependencies, reload authoritative state, or quarantine a true sequence conflict. Unique `(consumer, event_id)` prevents duplicate effects; unique aggregate/version protects concurrent transitions. Store causal and correlation IDs across discovery, committee, risk, execution, and accounting. Payloads carry immutable references rather than large provider documents or secrets.

## Event catalog

| Producer | Events | Principal consumers |
|---|---|---|
| Data | MARKET_DATA_UPDATED, NEWS_RECEIVED, DATA_QUALITY_CHANGED | Features, monitoring, health |
| Features | FEATURES_UPDATED | Scanner, Pods, monitoring |
| Discovery | CANDIDATE_CREATED, CANDIDATE_EXPIRED | Triage, Committee, research |
| Strategies | SIGNAL_DETECTED, STRATEGY_STATUS_CHANGED, STRATEGY_QUALIFIED, STRATEGY_ACTIVATED, STRATEGY_SUSPENDED | Discovery, portfolio/entry gate, monitoring, research |
| Regime | REGIME_UPDATED, REGIME_QUALITY_CHANGED | Pods, Committee evidence, research; never direct allocation |
| Intelligence | ANALYSIS_REQUESTED, ANALYSIS_COMPLETED, ANALYSIS_FAILED, TRADE_PROPOSED | Coordinator, portfolio, UI |
| Risk | RISK_APPROVED, RISK_RESIZED, RISK_REJECTED | Execution workflow, reporting, shadows |
| Execution | PAPER_ORDER_SUBMITTED, PAPER_ORDER_ACKNOWLEDGED, PAPER_ORDER_FILLED, PAPER_ORDER_CANCELED, PAPER_ORDER_UNKNOWN | Ledger, reconciliation, UI |
| Ledger | POSITION_CHANGED, POSITION_CLOSED, LEDGER_RECONCILIATION_FAILED | Monitoring, portfolio, health, research |
| Monitoring | EXIT_TRIGGERED, EXIT_PROPOSED | Portfolio workflow |
| Operations | ENTRY_HALT_CHANGED, FULL_KILL_CHANGED, HEALTH_GATE_CHANGED, MODE_CHANGED | Submission gate, scheduler, UI |
| Research | EXPERIMENT_COMPLETED, EVALUATION_COMPLETED | League, Knowledge, reporting |

`PAPER_ORDER_FILLED` denotes an individual normalized fill receipt, including partial executions; cumulative quantity/final status are payload fields. `POSITION_CHANGED` means Ledger has committed the fill, not merely that Execution received it. Risk events report decisions; submission still requires valid authorization and reservation.

## Durable scheduling

Jobs record job ID, kind, scope, scheduled time, deadline, idempotency key, priority, input version, attempt, lease owner/expiry, fencing token, and terminal result. A scheduler occurrence key `(schedule_version, scope, scheduled_at)` prevents duplicate creation. Workers use leases and fencing for local writes, heartbeat long jobs, and retry transient failures with bounded backoff/jitter. An expired lease alone is not proof an external submission failed; only Order Manager owns that recovery.

Work classes: reconciliation/ledger and health; deterministic position monitoring; market ingest/features; discovery/AI; reporting/research. Reserve capacity for safety work and bound each queue. Coalesce superseded scans/marks, preserving execution facts and exit triggers. Expired analysis jobs terminate visibly. Poison events go to a durable dead-letter state with redrive authorization and preserved identity; redrive never bypasses validation.

Schedule families include session open/close, data refresh, scans by horizon, event-triggered analyses, periodic position checks, reconciliation, end-of-day close, daily/weekly/monthly reports, and research. All frequencies, holiday/session policy, and deadlines remain OD-03/04. Store UTC instants; use an approved exchange calendar/timezone for sessions and daylight-saving changes. Wall-clock synchronization health is a gate; deterministic tests inject a clock. Catch-up policies distinguish essential reconciliation from obsolete scans.

## Recovery and backpressure

Startup order: storage/migrations readiness → process/lease identity → adapter and Ledger reconciliation → required data freshness → gate evaluation → resume eligible jobs. Do not automatically clear Entry Halt or Full Kill on restart. Full Kill requires explicit human re-arm; clearing it does not clear a separate Entry Halt or unhealthy component gate. Database loss prevents new authoritative transitions/submissions; diagnostics may use bounded local redacted logs, later linked to the incident. Broker receipts are recovered through adapter history/cursors. If history cannot close the gap, stay halted.

Report queue age, retries, dead letters, lease conflicts, causal lag, clock skew, and backlog by priority. Bounded model concurrency prevents market bursts from exhausting budget. Data backfills use bulk manifests; do not emit an unbounded job for every historical tick. Replay is explicitly sandboxed and cannot drive the active paper adapter.

Capability and eligibility events carry scope, previous/new state, generation, actor/reason and effective time; regime events reference immutable definition/snapshot/cutoff/quality. Events update read models, never substitute for synchronous authoritative gate reads at dispatch. STRATEGY_QUALIFIED records human/policy evidence, not a League score. All control/qualification changes serialize with relevant admission checks; duplicate/out-of-order events cannot restore old permission. Suspension and Entry Halt retain monitoring/receipts/reconciliation priority.

## Implemented subset — 2026-09-23

Durable jobs implement part of the scheduling baseline above. [Implementation status](../IMPLEMENTATION_STATUS.md) lists the test evidence.

- **Records.** Each `jobs` row (migration `0003_durable_jobs`) carries job ID, kind, work class, scope, correlation ID, payload, input version, priority, scheduled and available time, deadline, attempts with an explicit per-job `max_attempts`, redrive count, lease owner and expiry, fencing token, version, result, terminal reason and last error. The occurrence key is unique, and `occurrence_key(schedule_version, scope, scheduled_at)` builds the scheduler form. A trigger keeps identity immutable and moves the fencing token and version forward only.
- **Claims and capacity.** A claim transaction takes a claim advisory lock, expires overdue jobs, dead-letters crashed final attempts, counts live leases and leases the next job with `FOR UPDATE SKIP LOCKED`, reconciliation and monitoring classes first. `Capacity(slots, safety_reserved)` keeps reserved slots that only those two safety classes may use. The claim commits before the handler runs, so an attempt that kills its worker still counts toward the bound.
- **Fenced effects.** The handler runs in a transaction that locks the job under its fencing token and commits its local writes together with SUCCEEDED. An expired lease or a superseded token commits nothing, so a retry after a crash never duplicates an effect. Handlers may not call external services yet.
- **Failures and dead letters.** Other exceptions retry with capped exponential backoff and deterministic jitter until the bound. `PermanentJobError` dead-letters at once, and deadlines expire jobs. `JOB_DEAD_LETTERED` and `JOB_EXPIRED` are written to the outbox. The owner reads `GET /api/jobs` and `GET /api/jobs/dead-letters`. `POST /api/jobs/{job_id}/redrive` (command ID, expected version, reason) requeues a dead letter with the same identity, restarts its attempts and is audited as `JOB_REDRIVEN`.
- **Worker and controls.** The worker runs up to `JOBS_PER_TICK_DEV` jobs after each inbox batch. `LEASE_DEV`, `BACKOFF_DEV` and `CAPACITY_DEV` are labeled development values, not approved policy. Entry Halt and Full Kill gate submissions at admission, not jobs, so safety jobs keep running under both.
- **Not implemented.** No recurring schedule, cadence, calendar or catch-up policy exists (OD-03). Nothing is enqueued automatically, and no production job kind is registered. Heartbeats, external-effect jobs, queue-length bounds, coalescing, queue metrics and clock-skew gating are pending. Leases use the worker host's clock.
