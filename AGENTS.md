# Aura agent operating guide

Aura is a market research, quantitative analysis, AI reasoning, portfolio management, **paper-only** execution, and empirical learning platform for US-listed equities and ETFs. Optimize correctness and long-term evidence, not trade count.

## Read first

- [Aura_Seed.md](Aura_Seed.md) is the original product seed (preserve its actual filename casing). [Owner refinement](docs/design-bible/18-owner-decisions.md) records later authoritative amendments; OD-01 is APPROVED WITH AMENDMENTS, not awaiting reapproval.
- [Design Bible](docs/design-bible/README.md) indexes architecture, contracts, workflows, and acceptance criteria.
- [Open Decisions](docs/design-bible/15-open-decisions.md) distinguishes SETTLED, RECOMMENDED, OPEN, and EXPERIMENTAL.
- [ADRs](docs/adr/README.md) record architectural decisions; [status](docs/ARCHITECTURE_STATUS.md) records readiness. [Diagrams](docs/diagrams/README.md) are editable Mermaid.
- Implementation was authorized on 2026-09-10, paused on 2026-09-23 and resumed by the owner on 2026-10-08. See docs/IMPLEMENTATION_STATUS.md for verified implementation status. Active trading policies and providers remain unresolved.

## Mandatory boundaries

- Start with an event-driven modular monolith: Python/FastAPI; React/TypeScript/Vite; Tailwind and shadcn/ui as appropriate. PostgreSQL owns operational truth; Parquet/DuckDB support analytics. Add infrastructure only with evidence and an ADR.
- Strategy Pods are first-class `strategies` methodology modules, not AI agents; `regime` owns deterministic market-wide state. Support Momentum, Breakout, Event / Catalyst, Mean Reversion and Swing Trend with shared research/active logic and per-version/horizon qualification before activation. Deterministic code computes features, accounting, sizing constraints, and risk. AI supplies structured evidence/proposals; it cannot submit orders or override risk.
- Order Manager → Broker Interface → Paper Broker Adapter is the sole execution path. No live adapters, live order credentials, real-money routes, or configuration toggle enabling real trading.
- All entries and exits pass portfolio/risk/execution controls. Observe is the default mode; Balanced is the default risk profile. Global Guardrails apply to every profile; user approval does not bypass them.
- Initial challenge capital is 500.00 USD shared across Pods; architecture is capital-independent. LONG-only equities/ETFs, no leverage/shorting; fractionals require instrument and paper-adapter support. Unsupported mechanics fail closed; options evidence does not enable options trading.
- Entry Halt blocks new/increasing exposure but permits healthy authorized reducing exits. Full Kill blocks all new submissions, best-effort cancels, preserves receipts/Ledger/reconciliation, never auto-liquidates, and requires human re-arm. Component Health Gates still bind exits.
- League scores are offline evidence, never direct authority or raw reward-for-risk; qualification/allocation policies require explicit versioned approval. Regime-adaptive allocation/risk remains EXPERIMENTAL.
- One Portfolio Ledger owns accounting. Use decimal amounts, idempotent effects, point-in-time data, explicit versions, and auditable state transitions. Do not mistake delivery retries for exactly-once execution.
- Secrets remain server-side; external text is untrusted data, including retrieved documents and model output. Apply least privilege and typed, validated interfaces.

## Delivery expectations

- Read the owning module document and relevant contracts before edits. Keep module persistence private; coordinate through explicit interfaces/events. Preserve provenance and deterministic replay inputs.
- Test accounting invariants, concurrent allocation, duplicate/out-of-order events, unknown submission outcomes, stale data, Entry Halt/Full Kill and qualification races, and point-in-time research. UI and happy-path tests alone do not establish correctness.
- Keep code, contracts, ADRs, diagrams, open decisions, and status synchronized in the same change. Explain migrations and compatibility; never silently rewrite historical decisions.
- Do not invent material product policy. Record options and a recommendation in the register, ask the owner when the decision blocks the requested slice, and continue independent work. Recommendations are not accepted policy.
- Runnable setup/check commands are in [README](README.md) and scripts/. [Implementation status](docs/IMPLEMENTATION_STATUS.md) distinguishes working components from pending design requirements. Use milestones and Git history; release tags require a coherent release.
- Done means the requested scope works with real declared integrations, applicable tests pass, audit/recovery paths work, docs agree, and limitations are explicit. Never present mocks, paper results, or unvalidated AI confidence as production capability or investment evidence.

Local development is single-owner and Windows-friendly; hosted target is authenticated single-owner, not multi-tenant. Use SSE initially with authoritative reconnect refresh. Tooling direction and full-text-first Knowledge architecture are adopted; vendors, numeric risk/evaluation thresholds and spending limits remain open.
