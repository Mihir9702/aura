# Strategies and Market Regime Engine

Status: adopted OD-01 baseline as amended by the [owner refinement](18-owner-decisions.md). Exact signal rules, regime taxonomy/thresholds and qualification criteria remain open under OD-03/08/15/17.

## First-class Strategies domain

`aura.strategies` owns methodology definitions, immutable Pod versions, horizon specifications, strategy lifecycle, qualification and activation records. Discovery owns broad scanning/routing and candidate intake/deduplication; it invokes Strategies through a typed port. Research invokes the **same Pod version and evaluation logic**, with isolated clock/data/execution ports. Never fork a more optimistic research-only implementation of the active methodology.

Initial comprehensive scope structurally supports **Momentum, Breakout, Event / Catalyst, Mean Reversion, and Swing Trend**. Track implementation status separately (UNIMPLEMENTED, IMPLEMENTED, VERIFIED) from research/activation status. Registry entries alone do not constitute implemented Pods; runnable code alone does not constitute qualification. Multiple versions/horizons can be evaluated concurrently, but active bindings must be explicit.

Pod evaluation consumes frozen deterministic FeatureSnapshots, RegimeSnapshot where required, authorized evidence references, a versioned parameter set, and an injected clock. It emits candidates or a typed no-signal/invalid-input result. It has no mutable account balance, allocation authority, broker access, or self-promotion tool. Candidate generation records methodology/horizon versions, regime evidence, scope and data cutoffs. A missing required regime snapshot yields NOT_READY, not an LLM fallback.

## Lifecycle per Pod version / horizon / scope

| State | Meaning | Transition authority/evidence |
|---|---|---|
| DEVELOPMENT | Methodology being defined/implemented; may generate labeled test/Observe candidates | Reviewed immutable version; implementation status remains separate |
| BACKTESTING | Registered isolated historical evaluation | Frozen experiment/manifests and bias controls |
| PAPER_SHADOW | Prospective isolated evaluation under registered shadow policy | Backtest/out-of-sample evidence sufficient for approved experiment policy; no active-paper order authority |
| QUALIFIED | Evidence satisfies a versioned owner-approved qualification policy for a stated scope | Explicit human approval and immutable qualification record |
| ACTIVE_PAPER | Qualified scope explicitly enabled for the shared active paper portfolio | Current qualification plus owner activation record; mode/risk/capability checks still apply |
| SUSPENDED | No new/increasing exposure for this scope | Owner action or approved deterministic health/qualification rule, with reason |

Normal progression is DEVELOPMENT → BACKTESTING → PAPER_SHADOW → QUALIFIED → ACTIVE_PAPER. Failed evaluations are retained; a revised methodology creates a new immutable version in DEVELOPMENT. QUALIFIED may remain inactive indefinitely. Suspension is allowed from any state; record prior state, cause, affected permissions and outstanding orders. Returning from SUSPENDED requires an explicit reviewed resume transition; if evidence/policy/scope changed, repeat affected evaluation and qualification. No direct DEVELOPMENT → ACTIVE_PAPER or automatic score-based promotion.

Activation scope binds Pod version, parameter hash, horizon, eligible universe version/rules, required data/adapter capabilities, qualified execution/cost assumptions, applicable regime coverage, and qualification-policy version. Broader horizons, material parameter changes, new mechanics or materially changed assumptions require reviewed qualification, not inheritance by strategy name. Qualification may expire or require re-evaluation under a declared policy; dates/thresholds remain open. Owner approval must not waive Global Guardrails.

Active-paper entry/increase requires ACTIVE_PAPER plus a current qualification and activation binding in both Assisted and Autonomous modes. Unqualified prospective testing occurs in PAPER_SHADOW, not by treating a manual click as qualification. Default Observe can show unqualified research candidates clearly labeled, without active reservations. A suspended/expired Pod's existing holdings remain monitored against their preserved rules and may exit through healthy authorized reducing paths; qualification loss must never orphan positions.

## Shared-capital and suspension concurrency

All Pods/horizons share the canonical portfolio's **500.00 USD initial capital**, not 500 per Pod. Alternative capitals in isolated research/shadow portfolios must be labeled; they do not top up active paper cash. Portfolio Manager compares intents, and Risk reserves cash/quantity atomically including other Pods' commitments. There is no private Pod balance that can independently authorize spending.

Qualification/suspension changes serialize against final entry admission. Persist a versioned eligibility generation and lock/check it in the same coordination protocol as the portfolio gate. Use one documented lock order across paths (global controls → portfolio gate → sorted strategy eligibility scopes); do not hold database locks during network calls. Read models/events are not the authority for eligibility. An already admitted order can still fill after suspension; best-effort cancel increasing working orders and reconcile before releasing capacity. Monitor/exit rules survive suspension. Deterministic competition/tie-breaking and allocation limits remain OD-07; League scores are not allocation inputs without an explicit approved policy.

## Implemented subset — strategy scopes (2026-09-23)

Migration `s04_strategy_scopes` persists the registry in PostgreSQL, owned by `aura.strategies`. The five Pods are at version 0, a placeholder meaning no reviewed methodology exists (implementation UNIMPLEMENTED); reviewed methodology versions start at 1, the contracts' positive-integer Version. One horizon, `DAILY_MULTI_SESSION_DEV` version 1, is a development label marked UNAPPROVED under OD-03; it defines no durations, sessions or cadence. Each Pod has one scope binding its Pod version to that horizon version, with `universe_ref` null while OD-02 is open. Scopes start in DEVELOPMENT at version 1, eligibility generation 1.

Lifecycle commands take expected version and command_id and record one STRATEGY_STATUS_CHANGED outbox event per applied change, carrying prior/new state, version and generation, actor and reason. Replaying a command_id returns the recorded result; reusing it with a different body, command or scope is refused (COMMAND_REUSED). SUSPENDED is allowed from any state. Resume returns a suspended scope to DEVELOPMENT only and clears its qualification and activation references; the reviewed resume to an earlier evaluated state waits for OD-15 policy. QUALIFIED and ACTIVE_PAPER are refused with QUALIFICATION_POLICY_MISSING, and BACKTESTING and PAPER_SHADOW with EVALUATION_EVIDENCE_MISSING until Research registers experiments. Every applied change advances the eligibility generation.

Qualification and activation record tables exist, but no command writes them. Composite foreign keys bind a qualification to one scope's Pod version and horizon version and an activation to that qualification and scope, so an approval cannot be reused by another version or horizon; qualification records are append-only. Schema guards keep scope identity immutable, never delete a scope, and refuse lowering the version or generation or changing state or records without advancing both.

The entry eligibility check locks the global gate, then scopes one at a time in sorted id order, the same order Suspend and Resume use; the global gate still serializes these paths, as in ADR-0023. It admits only a scope that is ACTIVE_PAPER at the generation the entry bound, with an implemented Pod, an approved horizon, a qualification bound to that scope that is unexpired and under a policy the caller passes as approved, and an enabled activation for the portfolio at the current generation. Events and read models are never consulted. No runtime caller has an approved policy to pass and no entry path calls the check yet; tests exercise it with FIXTURE rows, including the AC-24 admission/suspension race. Owner-only routes read scopes and their history and issue Suspend and Resume under `/api/strategies/scopes`; there is no qualify or activate route. The Overview still lists the static registry; the Strategy Pods page will read scopes.

## Market Regime Engine

`aura.regime` owns deterministic versioned market-wide regime computation. It consumes trend, volatility, breadth, sector dispersion, liquidity and **approved** macro context from point-in-time Data/Features interfaces. Each RegimeDefinition specifies universe, sampling horizon/window, transformations, thresholds/taxonomy, source dependencies, missing/stale policy, warm-up, and any fitted-parameter training window. Exact definitions/thresholds remain OD-17.

RegimeSnapshot contains definition/version, market scope, event as-of, knowledge cutoff, computed time, immutable input manifests/feature references, component values/units, classification if defined, quality/status, and provenance. Status is READY / NOT_READY / INVALID. An optional statistical probability/uncertainty must come from a defined deterministic estimator, not AI confidence. Classification names must come from the approved definition, not hard-coded subjective adjectives.

Snapshots are immutable; late macro revisions or corrected breadth inputs create new snapshots and never rewrite earlier candidate evidence. Historical membership includes delistings and sector classification as known then. Macro values respect original release/availability rather than revision-final values. Fitted thresholds require train-only fitting and frozen out-of-sample application. Backtests use the same Regime Engine logic and as-of joins as active operation. Missing/invalid required inputs cannot be filled by an LLM narrative.

Pods may consume regime as evidence or a qualified methodology input. Committee may interpret the recorded regime snapshot but cannot write canonical regime state. **Regime-adaptive capital allocation/risk remains EXPERIMENTAL**: it has no active configuration route until empirical qualification and an explicit owner-approved versioned allocation/risk policy. A regime event alone never changes limits, profile, qualification or authorization.
