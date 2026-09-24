# Formal conceptual contracts

**Adopted conceptual baseline: OD-01 APPROVED WITH AMENDMENTS.** These are non-executable interface specifications, not generated application code. They define a minimum validation surface; storage schemas and OpenAPI will be derived during authorized implementation. Product parameters referenced here remain open in [the register](15-open-decisions.md).

## Common types and rules

`ID = opaque stable identifier`; `Version = positive integer`; `Instant = UTC RFC3339 timestamp`; `Decimal = base-10 string, no NaN/Infinity`; `Money = {amount: Decimal, currency: ISO currency code}`; `Ref = {id: ID, version: Version}`; `ArtifactRef = {manifest_id: ID, content_hash: string}`. Identifier generation choice is an M0 technical detail under the approved baseline. Quantitative feature values may be finite floating point with documented tolerances; money/quantity math uses decimals.

Every consequential record has ID, schema_version, created_at, correlation_id, immutable policy/input references, and provenance. Mutable lifecycle aggregates additionally have expected/current version; their history is append-only. Reject unknown enum values/fields at command boundaries unless explicitly versioned extensions permit them. Optional means explicitly nullable with reason; missing critical evidence is not a default zero. Payload limits, precision and rounding are declared by schema version before implementation.

## MarketObservation

```
id, schema_version, instrument_id, provider, provider_record_id, revision
kind: QUOTE | BAR | TRADE | FUNDAMENTAL | NEWS | CORPORATE_ACTION | MACRO
event_time, published_at?, received_at, available_at, recorded_at
availability_basis: LIVE_RECEIPT | VERIFIED_HISTORICAL | UNKNOWN
raw_artifact: ArtifactRef, quality_flags: string[], supersedes_id?
payload: discriminated record
QUOTE: bid, ask, bid_size, ask_size, currency, venue
BAR: interval_start, interval_end, interval, open, high, low, close,
     volume, final, adjustment_basis, currency
NEWS: document_ref, source_uri, publication_revision
```

Instrument may be null only for explicitly non-instrument data such as macro; applicable scope must then be supplied. Prices/sizes must satisfy kind-specific validation and tick rules; bar OHLC bounds and interval order must hold. UNKNOWN availability cannot support an as-of assertion. Reference content and revisions remain immutable.

## FeatureSnapshot

```
id, instrument_id, as_of, knowledge_cutoff, computed_at
definition_bundle: Ref, input_manifest: ArtifactRef, horizon
values: map<feature_name, {value: finite_number|null, unit, status, reason?}>
quality_flags, calendar_version, adjustment_policy_version
```

Only eligible observations enter the manifest; all values refer to the same declared cutoff semantics. Feature status: READY / NOT_READY / INVALID. Inputs after the cutoff invalidate the snapshot.

## Candidate

```
id, version, pod: Ref, instrument_id, direction: LONG
signal_at, expires_at, horizon: {min_duration?, max_duration, unit}
feature_snapshot_id, regime_snapshot_id?, horizon_spec: Ref, strategy_scope_id,
trigger_ref, thesis_intent, evidence_refs[]
entry_assumptions, invalidation_conditions[], exit_rules[]
deduplication_key, priority, state, state_reason
```

Initial direction is LONG only; reject SHORT, leverage and option-trading commands. Expires_at exceeds signal_at; methodology/horizon is required; state follows [candidate lifecycle](03-market-and-discovery.md). Candidates convey no permission. Deduplication includes parameter hash, horizon specification and strategy scope so simultaneous horizons cannot collapse into one candidate.

## AgentAnalysis

```
id, run_id, candidate: Ref, role, model_call_id, prompt_version
evidence_bundle: ArtifactRef, cutoff, completed_at
claims[]: {claim_id, statement, evidence_refs[], support: SUPPORT|CONTRADICT|UNCERTAIN}
counterevidence[], assumptions[], uncertainties[], invalidation_conditions[]
confidence: {value: number[0,1], meaning, calibration_ref?}
quantitative_refs[], status: VALID | INVALID | FAILED | TIMED_OUT
validation_results[], usage_ref
```

Claims require authorized existing citations or an explicit unsupported flag disqualifying them as evidence. INVALID/FAILED analyses are not adjudication evidence. Model-call metadata includes selected model and routing-policy version.

## CommitteeResult / Adjudication

```
id, candidate: Ref, analysis_policy: Ref, required_roles[], analysis_ids[]
missing_roles[], disagreement_summary[], evidence_assessment[]
outcome: PROPOSE | DEFER | NO_TRADE
rationale, uncertainties[], adjudicator_call_id, proposal_id?
```

PROPOSE requires a validated TradeProposal and required-role completion under the approved policy. No vote count determines approval. DEFER has a reason/deadline; it does not stay pending forever.

## TradeProposal

```
id, version, candidate: Ref, adjudication_id, instrument_id, pod: Ref
action: OPEN | INCREASE; direction: LONG
thesis, evidence_refs[], counterevidence_refs[], quantitative_refs[]
confidence: {value, meaning, calibration_ref?}
horizon: {max_duration, unit}, thesis_started_at?
entry: {order_type, trigger?, limit_price?, price_bound, valid_until, session_policy_ref}
invalidation_conditions[]: {kind, typed_parameters, evidence_ref?}
exit_rules[]: {kind, typed_parameters, priority}
reward_risk: {estimate_ref?, reward?, risk?, unit?, assumptions[], unavailable_reason?}
source_cutoff, source_timestamps[], feature_snapshot_id, regime_snapshot_id?,
horizon_spec: Ref, strategy_scope_id, analysis_ids[]
created_at, expires_at, supersedes_proposal_id?
```

`order_type`/rule enums must come from the approved OD-05 policy, not free-form executable text. All automated triggers use an allowlisted declarative rule grammar; narrative thesis is explanatory only. A zero/unknown risk denominator cannot yield a fabricated ratio. No quantity here is authorized; allocation supplies requested size. Edits create a new revision and invalidate prior approvals.

## PortfolioState and AllocationIntent

```
PortfolioState: portfolio_id, version, environment: ACTIVE_PAPER | SHADOW | BACKTEST,
  as_of, journal_watermark, cash_buckets[], position_refs[], marks[],
  equity: Money, realized_pnl: Money, unrealized_pnl: Money, fees: Money,
  reservations[], exposures[], valuation_quality, reconciliation_status
AllocationIntent: id, proposal: Ref, portfolio_id, portfolio_version,
  requested_quantity: Decimal, estimated_cost: Money, price_bound,
  allocation_policy: Ref, competing_intent_refs[], rationale, expires_at
```

PortfolioState is a Ledger projection plus reservation query, not writable client truth. Exposure methods and marks have versions. Snapshot staleness causes reevaluation.

## RiskDecision and ExecutionAuthorization

```
RiskDecision: id, proposal: Ref, allocation_id, portfolio_id, portfolio_version,
  policy: Ref, profile: CONSERVATIVE | BALANCED | AGGRESSIVE,
  input_manifest: ArtifactRef, evaluated_at, valid_until,
  outcome: APPROVE | RESIZE | REJECT, requested_quantity, allowed_quantity,
  rules[]: {code, observed, bound, result, evidence_ref}, reason_codes[]
ExecutionAuthorization: id, risk_decision_id, reservation_id, order_intent_id,
  proposal: Ref, mode_version, approval_id?, gate_generation, policy_version,
  permitted_quantity, price_bound, valid_until, state,
  action_class: ENTRY_INCREASE | REDUCE_EXIT, capability_snapshot_ref,
  strategy_eligibility_generation?, qualification_ref?, activation_ref?,
  instrument_capability_ref, adapter_capability_ref
```

REJECT allows zero and no authorization. RESIZE is strictly positive and below requested quantity. Authorization is server-created only, bound to the current gate, and consumed by one logical order. Entry eligibility/qualification is required for ENTRY_INCREASE; REDUCE_EXIT instead binds existing lots and valid exit permission, so suspension cannot orphan holdings. All entries/exits use this contract; proposal reference may refer to TradeProposal or ExitProposal with explicit kind.

## PaperOrder and Fill

```
PaperOrder: id, version, client_order_id, portfolio_id, proposal_kind, proposal: Ref,
  authorization_id, instrument_id, side: BUY | SELL, quantity: Decimal,
  action_class: ENTRY_INCREASE | REDUCE_EXIT, capability_refs[],
  order_type, limit_price?, stop_price?, time_in_force, session_policy_ref,
  adapter_id, adapter_account_id, environment: PAPER, adapter_order_id?,
  state, cumulative_filled_quantity, created_at, transitions[]
Fill: id, adapter_id, adapter_account_id, provider_execution_id, revision,
  order_id, instrument_id, side, quantity: Decimal, price: Decimal,
  currency, fees: Money, executed_at, received_at, source_ref,
  reference_price?, diagnostic_slippage?, simulator_version?, correction_of?
```

Fill quantities/prices are positive; cumulative effective fills cannot exceed order quantity without a reconciliation incident. Unique adapter/account/execution/revision prevents duplicate application; corrections explicitly reverse/replace earlier economic facts. Fill receipt is distinct from Ledger application. Fees specify currency and convention. PAPER is mandatory; no LIVE enum is provided.

## Position and ExitProposal

```
Position: id, portfolio_id, version, instrument_id, quantity: Decimal,
  lots[]: {lot_id, quantity, cost_basis: Money, proposal: Ref, pod: Ref,
           horizon, horizon_spec: Ref, strategy_scope_id, originating_qualification_ref?,
           invalidation_rules, exit_rules},
  mark_ref, realized_pnl: Money, unrealized_pnl: Money, journal_watermark
ExitProposal: id, version, position_id, position_version, lot_refs[],
  action: REDUCE | CLOSE, requested_quantity: Decimal, trigger_ref,
  reason: STOP | TARGET | TIME | THESIS | RISK_POLICY | USER,
  original_proposal_refs[], entry_execution_refs[], market_snapshot_ref,
  required_health_policy_ref, exit_permission_ref?, exit_order_rules, created_at, expires_at, analysis_ids?[], deduplication_key
```

Exit must refer to owned available quantity and original thesis. A close cannot reverse direction. All initial SELL orders must reduce existing LONG holdings within unreserved quantity; fractional support requires both instrument and adapter capability. An EXIT proposal does not modify original holding horizon. Lot attribution and cost-basis conventions are OD-11.

## StrategyExperiment, ShadowTrade, EvaluationResult

```
StrategyExperiment: id, version, hypothesis, pod_versions[], preregistered_at,
  baseline_ref, data_manifest, split_spec, metric_spec, termination_rules,
  cost_model_ref, simulator_ref, policy_refs[], budget_ref,
  code_commit, random_seed, state, run_refs[], promotion_approval_ref?
ShadowTrade: id, shadow_portfolio_id, shadow_policy: Ref, source_proposal: Ref,
  source_decision_ref, rejection_reason?, inclusion_rule, decision_time,
  simulated_order_refs[], simulator_ref, status, unevaluable_reason?
EvaluationResult: id, experiment_ref, target_ref, dataset_manifest, period,
  metric_version, metrics[]: {name, value|null, unit, interval?, undefined_reason?},
  sample_size, effective_sample_size?, regime_breakdown[], benchmark_ref?,
  decision_time_regime_manifest?, no_ai_baseline_ref?, shadow_comparison_refs[],
  costs_ref, limitations[], split_role, generated_at, artifact_refs[]
```

Evaluation records are offline governance evidence only: no direct order authorization, raw score-to-size feedback, or automatic promotion. Shadow IDs cannot be used with active-paper authorizations. Missing data/ambiguous paths are reported, not selectively removed without accounting for exclusions.

## EventEnvelope

```
event_id, event_type, schema_version, producer, aggregate_type, aggregate_id,
aggregate_version, occurred_at, recorded_at, correlation_id, causation_id?,
environment: ACTIVE_PAPER | SHADOW | BACKTEST | SYSTEM,
payload: {typed immutable references and small facts}, trace_id?
```

Commands are distinct from events and include command_id/idempotency_key, actor, expected_version, and deadline. Event timestamps do not create total order. Producers and consumers validate known versions; incompatible versions quarantine visibly. Additive optional fields require compatibility tests; semantic changes require a new version. Secrets and full untrusted documents do not belong in envelopes.

## Application ports

| Interface | Result and failure semantics |
|---|---|
| Data.read_as_of(query, cutoff, manifest?) | Eligible immutable records or explicit unavailable/quality result |
| Features.compute(definition, manifest, cutoff) | Snapshot with READY/NOT_READY/INVALID features |
| Pod.evaluate(snapshot, config) | Candidates or reasoned no-signal; no side effects on portfolio |
| ModelRouter.analyze(workload, evidence, budget) | Validated result or typed failure with usage |
| Portfolio.allocate(proposal, state, policy) | Intent or decline/defer |
| Risk.evaluate(bound_inputs) | Deterministic decision; no network calls |
| Execution.authorize_and_queue(intent, mode_permission) | Atomic decision/reservation/order or conflict/rejection |
| PaperAdapter.submit(order, client_id) | Ack/reject/unknown, never assume timeout means rejection |
| PaperAdapter.query(client_id), cancel(id), executions(cursor) | Reconciliation facts and capability-specific errors |
| Ledger.apply_fill(fill), reverse(source), snapshot(id) | Idempotent journal application or reconciliation incident |
| Monitor.evaluate(position, snapshot, clock) | Exit trigger/proposal or no-change |
| Research.run(experiment) | Isolated result; no active-paper effects |

All application mutations return entity/version and correlation ID. Error categories distinguish validation, conflict, stale input, policy rejection, unavailable dependency, rate/budget limit, and unknown external outcome; retries depend on category, not generic exception swallowing.

## StrategyPod, scope status, qualification and activation

```
StrategyPod: id, name: MOMENTUM | BREAKOUT | EVENT_CATALYST | MEAN_REVERSION | SWING_TREND,
  version, implementation_status: UNIMPLEMENTED | IMPLEMENTED | VERIFIED,
  code_ref, parameter_hash, methodology, horizon_specs: Ref[],
  required_features[], regime_requirement: REQUIRED | OPTIONAL | UNUSED,
  regime_definition_refs[], evidence_requirements[], entry_rules, exit_rules,
  supported_mechanics, experiment_refs[]
StrategyScopeState: id, pod: Ref, horizon_spec: Ref, universe_scope_ref,
  state: DEVELOPMENT | BACKTESTING | PAPER_SHADOW | QUALIFIED | ACTIVE_PAPER | SUSPENDED,
  version, eligibility_generation, qualification_ref?, activation_ref?,
  previous_state?, reason, changed_at, actor_ref
StrategyQualification: id, scope_ref, pod: Ref, horizon_spec: Ref, parameter_hash,
  qualification_policy: Ref, evaluation_refs[], data_capability_refs[],
  execution_assumption_refs[], universe_scope_ref, regime_coverage[],
  approved_by, approved_at, valid_until?, limitations[], revocation_ref?
StrategyActivation: id, qualification_ref, portfolio_id, scope_ref,
  approved_by, approved_at, generation, state: ENABLED | DISABLED
```

No automatic progression from implementation, evaluation score or model output. QUALIFIED requires an immutable human approval under a versioned qualification policy. ACTIVE_PAPER additionally requires enabled activation and current qualification at admission. A materially different version/horizon/scope cannot reuse approval. Resume follows the lifecycle in [Strategies](17-strategies-and-regime.md); stale events cannot lower eligibility generation. Suspension blocks entry/increase, preserving monitoring and authorized reducing exits.

## RegimeDefinition and RegimeSnapshot

```
RegimeDefinition: id, version, market_scope_ref, horizon, feature_requirements[],
  transformation_version, taxonomy?, thresholds_ref?, fitted_parameters_ref?,
  training_cutoff?, missing_data_policy_ref, quality_policy_ref
RegimeSnapshot: id, definition: Ref, market_scope_ref, as_of, knowledge_cutoff,
  computed_at, input_manifest: ArtifactRef, feature_snapshot_refs[],
  components: map<name, {value|null, unit, status}>,
  classification?, estimator_uncertainty?, status: READY | NOT_READY | INVALID,
  quality_reasons[], availability_provenance, supersedes_id?
```

All component observations and fitted parameters must be causally available at the declared cutoff. Approved macro context uses original release availability. Snapshot corrections never overwrite history. No LLM writer or subjective narrative field determines canonical classification. Required invalid regime blocks affected Pod evaluation. Optional regime absence is explicit. Adaptive risk/allocation is EXPERIMENTAL and cannot activate from REGIME_UPDATED.

## CapabilityControl, HealthGate and effective snapshot

```
CapabilityControl: id, scope: GLOBAL | PORTFOLIO, portfolio_id?,
  kind: ENTRY_HALT | FULL_KILL, active: boolean, version, generation,
  actor_ref, reason_codes[], changed_at, incident_ref?, rearm_record_ref?
HealthGate: id, component, scope_ref, version,
  capability: INGEST | ANALYZE | MONITOR | AUTHORIZE_ENTRY | SUBMIT_ENTRY |
    AUTHORIZE_EXIT | SUBMIT_EXIT | RECEIVE | POST_LEDGER | RECONCILE | CANCEL,
  status: HEALTHY | DEGRADED | HALTED | RECOVERING,
  observed_at, valid_until, reason_codes[], dependency_policy_ref
CapabilitySnapshot: id, control_refs[], health_gate_refs[], mode_ref,
  action_class: ENTRY_INCREASE | REDUCE_EXIT,
  permitted: boolean, blocking_reasons[], evaluated_at, generation_refs[]
```

Effective permission is the intersection of mode/exit permission, applicable control/health gates and risk authorization, plus qualification for entries. Full Kill blocks both actions; Entry Halt blocks ENTRY_INCREASE only. REDUCE_EXIT still needs healthy required data/Ledger/adapter/policy, owned unreserved quantity and valid exit permission. Unknown required health is blocking. Snapshot is evidence, not reusable authority: recheck current generations at dispatch. Full Kill re-arm requires a human record and never clears other controls. Controls and gate states persist across restart.

Event payloads for ENTRY_HALT_CHANGED/FULL_KILL_CHANGED/HEALTH_GATE_CHANGED and STRATEGY_STATUS_CHANGED carry scope, prior/new version, generation, actor and reason. REGIME_UPDATED references snapshot/definition/cutoff/status; these facts never grant execution authority.

## Instrument/adapter capabilities and capital

```
TradingCapabilities: id, version, instrument_id, adapter_id, observed_at, valid_until,
  instrument_type: EQUITY | ETF, long_only: true, leverage_allowed: false,
  fractional_supported: boolean, quantity_increment: Decimal,
  minimum_quantity: Decimal, minimum_notional: Money?, allowed_order_types[],
  sessions[], currency, source_refs[]
ChallengeCapital: challenge_id, portfolio_id, initial_capital: {amount: "500.00", currency: "USD"},
  journal_source_id, recorded_at
```

Effective mechanics are the intersection of instrument and adapter capabilities. Reject unsupported/unknown mechanics; do not round quantity up, silently borrow, enable options trading from options evidence, or reset cash on restart. Runtime caps are capital-independent functions of state/policy; the initial funding journal is exactly once. Test fixtures may use other explicitly labeled capital to prove generality.

Additional ports: Strategies.evaluate(pod_version, horizon, frozen_inputs) → candidates/no-signal; Strategies.qualification(scope) → authoritative eligibility; Regime.compute(definition, manifest, cutoff) → immutable snapshot; Operations.effective_capability(action, scope, current_state) → blocking reasons. Research uses the same Strategies/Regime ports but isolated portfolio/execution identities.

## Implemented subset — 2026-09-10

[OpenAPI](../../packages/contracts/openapi.json) and generated frontend types now cover the implemented local API, including Overview and control commands. Since 2026-09-23 the journal and event feeds are typed as `JournalRecord` and `EventRecord`, the event feed lists the newest events first, and a unit test fails when the committed OpenAPI drifts from the application. The complete trading/research contracts above remain target specifications. See [implementation status](../IMPLEMENTATION_STATUS.md) before treating any conceptual interface as implemented.
