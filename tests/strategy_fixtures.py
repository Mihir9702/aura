"""FIXTURE strategy rows for integration tests.

No command can qualify or activate a scope while no qualification policy is approved (OD-15).
These helpers write the rows a future approved path would write, labeled FIXTURE, directly in
the test database, and follow the schema guards: every status change advances the scope
version and eligibility generation and records STRATEGY_STATUS_CHANGED. Nothing here is a
runtime default or an approved policy.
"""

from datetime import UTC, datetime
from uuid import uuid4

from aura.events import emit
from aura.strategies import STATUS_CHANGED, scope_id
from aura.strategies.models import (
    ActivationRow,
    HorizonRow,
    PodVersionRow,
    QualificationRow,
    ScopeRow,
)
from sqlalchemy.orm import Session

FIXTURE_POLICY = "FIXTURE_QUALIFICATION_POLICY_V1"
FIXTURE_POD = "fixture-momentum"
FIXTURE_HORIZON = "FIXTURE_HORIZON_A"
OTHER_FIXTURE_HORIZON = "FIXTURE_HORIZON_B"
FIXTURE_ACTOR = "fixture"
PORTFOLIO = "challenge"


def fixture_scope(
    session: Session,
    *,
    pod_version: int = 1,
    horizon_id: str = FIXTURE_HORIZON,
    universe_ref: str | None = None,
) -> ScopeRow:
    """A DEVELOPMENT scope of an IMPLEMENTED FIXTURE Pod version and APPROVED FIXTURE horizon."""
    if session.get(PodVersionRow, (FIXTURE_POD, pod_version)) is None:
        session.add(
            PodVersionRow(
                id=FIXTURE_POD,
                version=pod_version,
                name="MOMENTUM",
                display_name="FIXTURE Momentum",
                description="FIXTURE: test-only Pod version",
                implementation_status="IMPLEMENTED",
            )
        )
    if session.get(HorizonRow, (horizon_id, 1)) is None:
        session.add(
            HorizonRow(
                id=horizon_id,
                version=1,
                approval="APPROVED",
                decision_ref="FIXTURE",
                description="FIXTURE: test-only horizon",
            )
        )
    session.flush()
    identity = scope_id(FIXTURE_POD, pod_version, horizon_id, 1)
    scope = ScopeRow(
        id=identity if universe_ref is None else f"{identity}:{universe_ref}",
        pod_id=FIXTURE_POD,
        pod_version=pod_version,
        horizon_id=horizon_id,
        horizon_version=1,
        universe_ref=universe_ref,
        state="DEVELOPMENT",
        version=1,
        eligibility_generation=1,
        reason="FIXTURE scope",
        actor=FIXTURE_ACTOR,
        changed_at=datetime.now(UTC),
    )
    session.add(scope)
    session.flush()
    return scope


def set_state(
    session: Session,
    scope: ScopeRow,
    state: str,
    *,
    qualification_id: str | None = None,
    activation_id: str | None = None,
) -> None:
    """Simulate a status change the way a command records it."""
    prior = (scope.state, scope.version, scope.eligibility_generation)
    scope.previous_state = scope.state
    scope.state = state
    scope.qualification_id = qualification_id
    scope.activation_id = activation_id
    scope.version += 1
    scope.eligibility_generation += 1
    scope.reason = "FIXTURE transition"
    scope.actor = FIXTURE_ACTOR
    scope.changed_at = datetime.now(UTC)
    emit(
        session,
        STATUS_CHANGED,
        scope.id,
        scope.version,
        f"fixture-{uuid4()}",
        {
            "scope_id": scope.id,
            "pod": {"id": scope.pod_id, "version": scope.pod_version},
            "horizon": {"id": scope.horizon_id, "version": scope.horizon_version},
            "prior_state": prior[0],
            "new_state": state,
            "prior_version": prior[1],
            "version": scope.version,
            "prior_generation": prior[2],
            "generation": scope.eligibility_generation,
            "actor": FIXTURE_ACTOR,
            "reason": "FIXTURE transition",
        },
    )
    session.flush()


def qualification_for(
    scope: ScopeRow, *, policy: str = FIXTURE_POLICY, valid_until: datetime | None = None
) -> QualificationRow:
    return QualificationRow(
        id=f"FIXTURE_QUALIFICATION_{uuid4().hex}",
        scope_id=scope.id,
        pod_id=scope.pod_id,
        pod_version=scope.pod_version,
        horizon_id=scope.horizon_id,
        horizon_version=scope.horizon_version,
        policy_ref=policy,
        evidence_refs=["FIXTURE_EVALUATION"],
        limitations=["FIXTURE: not investment evidence"],
        approved_by=FIXTURE_ACTOR,
        approved_at=datetime.now(UTC),
        valid_until=valid_until,
    )


def qualify(
    session: Session, scope: ScopeRow, *, valid_until: datetime | None = None
) -> QualificationRow:
    """Record a FIXTURE approval for the scope and move it to QUALIFIED."""
    qualification = qualification_for(scope, valid_until=valid_until)
    session.add(qualification)
    session.flush()
    set_state(session, scope, "QUALIFIED", qualification_id=qualification.id)
    return qualification


def activate(
    session: Session, scope: ScopeRow, *, valid_until: datetime | None = None
) -> tuple[QualificationRow, ActivationRow]:
    """Qualify, then activate, a scope for the challenge portfolio (FIXTURE records)."""
    qualification = qualify(session, scope, valid_until=valid_until)
    activation = ActivationRow(
        id=f"FIXTURE_ACTIVATION_{uuid4().hex}",
        qualification_id=qualification.id,
        scope_id=scope.id,
        portfolio_id=PORTFOLIO,
        generation=scope.eligibility_generation + 1,  # the generation activation creates
        state="ENABLED",
        approved_by=FIXTURE_ACTOR,
        approved_at=datetime.now(UTC),
    )
    session.add(activation)
    session.flush()
    set_state(
        session,
        scope,
        "ACTIVE_PAPER",
        qualification_id=qualification.id,
        activation_id=activation.id,
    )
    return qualification, activation
