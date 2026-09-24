"""Strategy scope rules that need no database: lifecycle refusals, contracts, seeds and
the approval binding checked at entry admission."""

import importlib.util
from dataclasses import fields
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import ModuleType
from typing import get_args

import pytest
from aura.strategies import PODS, Pod, Status, registry, validate_transition
from aura.strategies import contracts as scope_contracts
from aura.strategies import eligibility as el
from aura.strategies.lifecycle import (
    EVALUATION_EVIDENCE_MISSING,
    QUALIFICATION_POLICY_MISSING,
    RESUME_REQUIRES_SUSPENDED,
    scope_transition_refusal,
)
from aura.strategies.models import (
    ActivationRow,
    HorizonRow,
    PodVersionRow,
    QualificationRow,
    ScopeRow,
)

ROOT = Path(__file__).resolve().parents[1]
AT = datetime(2026, 9, 23, 15, tzinfo=UTC)
POLICY = "FIXTURE_QUALIFICATION_POLICY_V1"


def load_migration() -> ModuleType:
    (path,) = (ROOT / "infra/migrations/versions").glob("*strategy_scopes*.py")
    spec = importlib.util.spec_from_file_location("strategy_scopes_migration", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_package_keeps_the_single_module_exports():
    # The overview serializes Pod with dataclasses.asdict against a strict contract.
    assert [f.name for f in fields(Pod)] == [
        "id",
        "name",
        "description",
        "status",
        "implementation",
    ]
    assert [p.id for p in PODS] == [
        "momentum",
        "breakout",
        "event-catalyst",
        "mean-reversion",
        "swing-trend",
    ]
    with pytest.raises(ValueError):
        validate_transition(
            Status.SUSPENDED, Status.DEVELOPMENT, evidence=True, human_approval=True
        )


@pytest.mark.parametrize("current", list(Status))
def test_scope_transitions_while_policy_is_open(current):
    for target in (Status.QUALIFIED, Status.ACTIVE_PAPER):
        assert scope_transition_refusal(current, target) == QUALIFICATION_POLICY_MISSING
    for target in (Status.BACKTESTING, Status.PAPER_SHADOW):
        assert scope_transition_refusal(current, target) == EVALUATION_EVIDENCE_MISSING
    assert scope_transition_refusal(current, Status.SUSPENDED) is None
    resume = scope_transition_refusal(current, Status.DEVELOPMENT)
    assert resume == (None if current == Status.SUSPENDED else RESUME_REQUIRES_SUSPENDED)


def test_contract_enums_match_the_domain():
    assert set(get_args(scope_contracts.ScopeState)) == {s.value for s in Status}
    assert set(get_args(scope_contracts.PodName)) == {n.value for n in registry.PodName}
    assert [registry.pod_name(p.id) for p in PODS] == list(registry.PodName)


def test_migration_seeds_match_the_registry():
    migration = load_migration()
    assert migration.down_revision == "0002"
    assert "strategy_scopes" in migration.revision
    assert migration.POD_VERSION == registry.PLACEHOLDER_POD_VERSION == 0
    assert migration.PODS == tuple(
        (p.id, registry.pod_name(p.id).value, p.name, p.description) for p in PODS
    )
    assert (
        migration.HORIZON_ID,
        migration.HORIZON_VERSION,
        migration.HORIZON_APPROVAL,
        migration.HORIZON_DECISION,
        migration.HORIZON_DESCRIPTION,
    ) == (
        "DAILY_MULTI_SESSION_DEV",
        registry.DEV_HORIZON_VERSION,
        "UNAPPROVED",
        "OD-03",
        registry.DEV_HORIZON_DESCRIPTION,
    )
    assert migration.SCOPES == registry.SEEDED_SCOPE_IDS
    assert len(set(migration.SCOPES)) == 5
    assert (migration.SEED_STATE, migration.SEED_ACTOR, migration.SEED_REASON) == (
        "DEVELOPMENT",
        registry.SEED_ACTOR,
        registry.SEED_REASON,
    )


def rows(
    *, pod_version: int = 1, horizon: str = "FIXTURE_HORIZON_A", generation: int = 3
) -> tuple[ScopeRow, PodVersionRow, HorizonRow, QualificationRow, ActivationRow]:
    """An eligible ACTIVE_PAPER FIXTURE scope with its records, built in memory."""
    scope_id = registry.scope_id("fixture-momentum", pod_version, horizon, 1)
    scope = ScopeRow(
        id=scope_id,
        pod_id="fixture-momentum",
        pod_version=pod_version,
        horizon_id=horizon,
        horizon_version=1,
        state="ACTIVE_PAPER",
        version=3,
        eligibility_generation=generation,
        qualification_id=f"q:{scope_id}",
        activation_id=f"a:{scope_id}",
    )
    pod = PodVersionRow(
        id="fixture-momentum", version=pod_version, implementation_status="IMPLEMENTED"
    )
    horizon_row = HorizonRow(id=horizon, version=1, approval="APPROVED")
    qualification = QualificationRow(
        id=f"q:{scope_id}",
        scope_id=scope_id,
        pod_id="fixture-momentum",
        pod_version=pod_version,
        horizon_id=horizon,
        horizon_version=1,
        policy_ref=POLICY,
        valid_until=None,
    )
    activation = ActivationRow(
        id=f"a:{scope_id}",
        qualification_id=f"q:{scope_id}",
        scope_id=scope_id,
        portfolio_id="challenge",
        generation=generation,
        state="ENABLED",
    )
    return scope, pod, horizon_row, qualification, activation


def blockers(scope, pod, horizon, qualification, activation, *, expected=3, policies=(POLICY,)):
    return el.entry_blockers(
        scope,
        pod,
        horizon,
        qualification,
        activation,
        expected_generation=expected,
        portfolio_id="challenge",
        approved_policies=policies,
        at=AT,
    )


def test_eligible_fixture_has_no_blockers():
    assert blockers(*rows()) == ()


def test_approval_cannot_be_reused_for_another_version_or_horizon():
    _, _, _, approval_v1, activation_v1 = rows(pod_version=1)
    # The same approval and activation presented for version 2, then for another horizon.
    for scope, pod, horizon, _, _ in (rows(pod_version=2), rows(horizon="FIXTURE_HORIZON_B")):
        scope.qualification_id, scope.activation_id = approval_v1.id, activation_v1.id
        reasons = blockers(scope, pod, horizon, approval_v1, activation_v1)
        assert el.QUALIFICATION_SCOPE_MISMATCH in reasons
        assert el.ACTIVATION_SCOPE_MISMATCH in reasons


@pytest.mark.parametrize(
    "change,reason",
    [
        (lambda s, p, h, q, a: setattr(s, "eligibility_generation", 4), None),
        (lambda s, p, h, q, a: setattr(s, "state", "SUSPENDED"), el.SCOPE_NOT_ACTIVE_PAPER),
        (
            lambda s, p, h, q, a: setattr(p, "implementation_status", "UNIMPLEMENTED"),
            el.POD_UNIMPLEMENTED,
        ),
        (lambda s, p, h, q, a: setattr(h, "approval", "UNAPPROVED"), el.HORIZON_UNAPPROVED),
        (
            lambda s, p, h, q, a: setattr(q, "policy_ref", "FIXTURE_OTHER_POLICY"),
            el.QUALIFICATION_POLICY_NOT_APPROVED,
        ),
        (lambda s, p, h, q, a: setattr(q, "valid_until", AT), el.QUALIFICATION_EXPIRED),
        (
            lambda s, p, h, q, a: setattr(a, "portfolio_id", "fixture"),
            el.ACTIVATION_PORTFOLIO_MISMATCH,
        ),
        (lambda s, p, h, q, a: setattr(a, "state", "DISABLED"), el.ACTIVATION_DISABLED),
        (lambda s, p, h, q, a: setattr(a, "generation", 2), el.ACTIVATION_STALE_GENERATION),
    ],
)
def test_each_stale_or_missing_fact_blocks_entry(change, reason):
    scope, pod, horizon, qualification, activation = rows()
    change(scope, pod, horizon, qualification, activation)
    found = blockers(scope, pod, horizon, qualification, activation)
    if reason is None:
        # A generation bump alone makes the authorization and the activation stale.
        assert found == (el.STALE_ELIGIBILITY_GENERATION, el.ACTIVATION_STALE_GENERATION)
    else:
        assert found == (reason,)


def test_missing_records_and_policies_block_entry():
    scope, pod, horizon, qualification, activation = rows()
    assert blockers(scope, None, None, None, None) == (
        el.POD_UNIMPLEMENTED,
        el.HORIZON_UNAPPROVED,
        el.QUALIFICATION_MISSING,
        el.ACTIVATION_MISSING,
    )
    # No approved qualification policy exists, so the runtime has none to pass (OD-15).
    assert blockers(scope, pod, horizon, qualification, activation, policies=()) == (
        el.QUALIFICATION_POLICY_NOT_APPROVED,
    )
    assert blockers(*rows(), expected=2) == (el.STALE_ELIGIBILITY_GENERATION,)
    valid = rows()
    valid[3].valid_until = AT + timedelta(seconds=1)
    assert blockers(*valid) == ()
