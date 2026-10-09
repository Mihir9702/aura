"""Persisted strategy scopes on PostgreSQL: seeds, lifecycle commands, idempotency, schema
guards and approval binding. Qualified or active scopes exist here only as FIXTURE rows."""

from datetime import UTC, datetime
from uuid import uuid4

import pytest
import strategy_fixtures as fx
from aura.events import Event, emit
from aura.strategies import (
    SEEDED_SCOPE_IDS,
    STATUS_CHANGED,
    Status,
    StrategyRefusal,
    UnknownScope,
    check_entry_eligibility,
    list_scopes,
    scope_history,
    suspend,
    transition,
)
from aura.strategies import eligibility as el
from aura.strategies.models import ActivationRow, QualificationRow, ScopeRow
from aura.worker import acknowledge_batch
from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError, IntegrityError

pytestmark = pytest.mark.integration

MOMENTUM = SEEDED_SCOPE_IDS[0]
ADVANCE = "version = version + 1, eligibility_generation = eligibility_generation + 1"


def command(db, target, scope=MOMENTUM, *, version, command_id=None, reason="owner review"):
    with db.transaction() as s:
        return transition(
            s,
            scope,
            target,
            expected_version=version,
            command_id=command_id or str(uuid4()),
            reason=reason,
            actor="owner",
        )


def status_events(db, scope=MOMENTUM):
    with db.transaction() as s:
        return s.scalars(
            select(Event)
            .where(Event.event_type == STATUS_CHANGED, Event.aggregate_id == scope)
            .order_by(Event.sequence)
        ).all()


def eligibility(db, scope_id, generation, *, policies=(fx.FIXTURE_POLICY,)):
    with db.transaction() as s:
        return check_entry_eligibility(
            s,
            {scope_id: generation},
            portfolio_id=fx.PORTFOLIO,
            approved_policies=policies,
            at=datetime.now(UTC),
        )


def execute(db, statement, **params):
    with db.transaction() as s:
        s.execute(text(statement), params)


def test_seeded_registry(db):
    with db.transaction() as s:
        scopes = list_scopes(s)
    assert sorted(scope.id for scope in scopes) == sorted(SEEDED_SCOPE_IDS)
    assert {scope.pod.name for scope in scopes} == {
        "MOMENTUM",
        "BREAKOUT",
        "EVENT_CATALYST",
        "MEAN_REVERSION",
        "SWING_TREND",
    }
    for scope in scopes:
        assert (scope.pod.version, scope.pod.implementation_status) == (0, "UNIMPLEMENTED")
        assert (scope.horizon.id, scope.horizon.approval, scope.horizon.decision_ref) == (
            "DAILY_MULTI_SESSION_DEV",
            "UNAPPROVED",
            "OD-03",
        )
        assert (scope.state, scope.version, scope.eligibility_generation) == ("DEVELOPMENT", 1, 1)
        assert (scope.universe_ref, scope.qualification_id, scope.activation_id) == (
            None,
            None,
            None,
        )


@pytest.mark.parametrize("target", [Status.QUALIFIED, Status.ACTIVE_PAPER])
def test_qualification_states_are_refused_without_an_approved_policy(db, target):
    with pytest.raises(StrategyRefusal) as refused:
        command(db, target, version=1)
    assert refused.value.code == "QUALIFICATION_POLICY_MISSING"
    assert str(refused.value).startswith("QUALIFICATION_POLICY_MISSING: ")
    assert status_events(db) == []
    with db.transaction() as s:
        scope = s.get(ScopeRow, MOMENTUM)
        assert (scope.state, scope.version, scope.eligibility_generation) == ("DEVELOPMENT", 1, 1)


def test_evaluation_states_need_research_evidence(db):
    for target in (Status.BACKTESTING, Status.PAPER_SHADOW):
        with pytest.raises(StrategyRefusal, match="EVALUATION_EVIDENCE_MISSING"):
            command(db, target, version=1)
    assert status_events(db) == []


@pytest.mark.parametrize("state", list(Status))
def test_suspension_from_any_state_advances_the_generation(db, state):
    with db.transaction() as s:
        scope = fx.fixture_scope(s)
        if state == Status.QUALIFIED:
            fx.qualify(s, scope)
        elif state == Status.ACTIVE_PAPER:
            fx.activate(s, scope)
        elif state != Status.DEVELOPMENT:
            fx.set_state(s, scope, state.value)
        scope_id, version, generation = scope.id, scope.version, scope.eligibility_generation
        assert scope.state == state
    change = command(db, Status.SUSPENDED, scope_id, version=version, reason="incident review")
    assert (change.state, change.version, change.eligibility_generation) == (
        "SUSPENDED",
        version + 1,
        generation + 1,
    )
    event = status_events(db, scope_id)[-1]
    assert (event.aggregate_version, event.payload["pod"], event.payload["horizon"]) == (
        version + 1,
        {"id": fx.FIXTURE_POD, "version": 1},
        {"id": fx.FIXTURE_HORIZON, "version": 1},
    )
    assert {k: v for k, v in event.payload.items() if k not in ("pod", "horizon")} == {
        "scope_id": scope_id,
        "prior_state": state.value,
        "new_state": "SUSPENDED",
        "prior_version": version,
        "version": version + 1,
        "prior_generation": generation,
        "generation": generation + 1,
        "actor": "owner",
        "reason": "incident review",
    }
    with db.transaction() as s:
        assert s.get(ScopeRow, scope_id).previous_state == state


def test_resume_returns_a_suspended_scope_to_development_only(db):
    with pytest.raises(StrategyRefusal, match="RESUME_REQUIRES_SUSPENDED"):
        command(db, Status.DEVELOPMENT, version=1)
    with db.transaction() as s:
        scope = fx.fixture_scope(s)
        fx.activate(s, scope)
        scope_id, version, generation = scope.id, scope.version, scope.eligibility_generation
    command(db, Status.SUSPENDED, scope_id, version=version)
    with db.transaction() as s:
        suspended = s.get(ScopeRow, scope_id)
        # Suspension keeps the records for audit; they no longer make the scope eligible.
        assert suspended.qualification_id and suspended.activation_id
    change = command(db, Status.DEVELOPMENT, scope_id, version=version + 1)
    assert (change.state, change.version, change.eligibility_generation) == (
        "DEVELOPMENT",
        version + 2,
        generation + 2,
    )
    with db.transaction() as s:
        resumed = s.get(ScopeRow, scope_id)
        assert (resumed.previous_state, resumed.qualification_id, resumed.activation_id) == (
            "SUSPENDED",
            None,
            None,
        )
    # Back in DEVELOPMENT the scope must qualify again, which needs an approved policy.
    with pytest.raises(StrategyRefusal, match="QUALIFICATION_POLICY_MISSING"):
        command(db, Status.QUALIFIED, scope_id, version=version + 2)


def test_command_replay_is_idempotent_and_reuse_is_refused(db):
    command_id = str(uuid4())
    first = command(db, Status.SUSPENDED, version=1, command_id=command_id)
    # A retried delivery after commit returns the recorded result without applying again.
    assert command(db, Status.SUSPENDED, version=1, command_id=command_id) == first
    assert len(status_events(db)) == 1
    reuses = [
        {"target": Status.SUSPENDED, "version": 1, "reason": "a different reason"},
        {"target": Status.SUSPENDED, "version": 2},
        {"target": Status.DEVELOPMENT, "version": 1},
        {"target": Status.SUSPENDED, "scope": SEEDED_SCOPE_IDS[1], "version": 1},
    ]
    for reuse in reuses:
        with pytest.raises(StrategyRefusal) as refused:
            command(db, command_id=command_id, **reuse)
        assert refused.value.code == "COMMAND_REUSED"
    # A command id that audited another command (here a control change) cannot be reused.
    with db.transaction() as s:
        emit(s, "ENTRY_HALT_CHANGED", "global", 2, "control-command", {"active": True})
    with pytest.raises(StrategyRefusal, match="COMMAND_REUSED"):
        command(db, Status.SUSPENDED, version=2, command_id="control-command")
    # A new command against the old version is stale, not a replay.
    with pytest.raises(StrategyRefusal, match="VERSION_CONFLICT"):
        command(db, Status.SUSPENDED, version=1)
    assert [e.aggregate_version for e in status_events(db)] == [2]
    with pytest.raises(UnknownScope), db.transaction() as s:
        suspend(
            s, "no-such-scope", expected_version=1, command_id=str(uuid4()), reason="x", actor="o"
        )


def test_history_lists_status_changes_oldest_first(db):
    command(db, Status.SUSPENDED, version=1, reason="first")
    command(db, Status.DEVELOPMENT, version=2, reason="second")
    with db.transaction() as s:
        history = scope_history(s, MOMENTUM)
        assert scope_history(s, SEEDED_SCOPE_IDS[1]) == []
        with pytest.raises(UnknownScope):
            scope_history(s, "no-such-scope")
    assert [(h.prior_state, h.new_state, h.generation, h.reason) for h in history] == [
        ("DEVELOPMENT", "SUSPENDED", 2, "first"),
        ("SUSPENDED", "DEVELOPMENT", 3, "second"),
    ]


def test_schema_guards_generation_identity_and_records(db):
    guarded = [
        # Stale writers cannot lower the generation or the version.
        ("UPDATE strategy_scopes SET eligibility_generation = 0 WHERE id = :scope", "decrease"),
        ("UPDATE strategy_scopes SET version = version - 1 WHERE id = :scope", "decrease"),
        # A state change must advance both.
        (
            "UPDATE strategy_scopes SET state = 'SUSPENDED', version = version + 1 "
            "WHERE id = :scope",
            "advance",
        ),
        ("UPDATE strategy_scopes SET pod_version = 1 WHERE id = :scope", "immutable"),
        ("DELETE FROM strategy_scopes WHERE id = :scope", "never deleted"),
        # QUALIFIED and ACTIVE_PAPER need their records.
        (
            f"UPDATE strategy_scopes SET state = 'ACTIVE_PAPER', {ADVANCE} WHERE id = :scope",
            "strategy_scope_records",
        ),
    ]
    for statement, message in guarded:
        with pytest.raises(DBAPIError, match=message):
            execute(db, statement, scope=MOMENTUM)
    with db.transaction() as s:
        qualification = fx.qualify(s, fx.fixture_scope(s))
    for statement in (
        "UPDATE strategy_qualifications SET policy_ref = 'OTHER' WHERE id = :id",
        "DELETE FROM strategy_qualifications WHERE id = :id",
    ):
        with pytest.raises(DBAPIError, match="append-only"):
            execute(db, statement, id=qualification.id)


def test_approval_cannot_be_reused_for_another_version_or_horizon(db):
    with db.transaction() as s:
        approved = fx.fixture_scope(s)
        qualification, activation = fx.activate(s, approved)
        others = [
            fx.fixture_scope(s, pod_version=2),
            fx.fixture_scope(s, horizon_id=fx.OTHER_FIXTURE_HORIZON),
        ]
    for other in others:
        # Attaching another scope's approval violates the binding foreign key.
        with pytest.raises(IntegrityError, match="strategy_scope_qualification"):
            execute(
                db,
                "UPDATE strategy_scopes SET state = 'QUALIFIED', qualification_id = :q, "
                f"{ADVANCE} WHERE id = :scope",
                q=qualification.id,
                scope=other.id,
            )
        # A copy of the approval claiming this scope must match its Pod version and horizon.
        with pytest.raises(IntegrityError), db.transaction() as s:
            copy = fx.qualification_for(approved)
            copy.scope_id = other.id
            s.add(copy)
        # An activation cannot point at another scope's qualification.
        with pytest.raises(IntegrityError), db.transaction() as s:
            s.add(
                ActivationRow(
                    id=f"FIXTURE_REUSE_{uuid4().hex}",
                    qualification_id=qualification.id,
                    scope_id=other.id,
                    portfolio_id=fx.PORTFOLIO,
                    generation=2,
                    state="ENABLED",
                    approved_by=fx.FIXTURE_ACTOR,
                    approved_at=datetime.now(UTC),
                )
            )
        blockers = eligibility(db, other.id, 1).scopes[0].blockers
        assert el.QUALIFICATION_MISSING in blockers and el.ACTIVATION_MISSING in blockers
    assert eligibility(db, approved.id, approved.eligibility_generation).permitted
    with db.transaction() as s:
        assert s.scalar(select(func.count()).select_from(QualificationRow)) == 1
        assert s.get(ActivationRow, activation.id).scope_id == approved.id


def test_duplicate_or_out_of_order_status_events_cannot_restore_eligibility(db):
    with db.transaction() as s:
        scope = fx.fixture_scope(s)
        fx.activate(s, scope)
        scope_id, version, generation = scope.id, scope.version, scope.eligibility_generation
    assert eligibility(db, scope_id, generation).permitted
    suspend_id = str(uuid4())
    command(db, Status.SUSPENDED, scope_id, version=version, command_id=suspend_id)
    active = next(
        e for e in status_events(db, scope_id) if e.payload["new_state"] == "ACTIVE_PAPER"
    )

    # A duplicate delivery of the ACTIVE_PAPER event cannot be recorded again.
    with pytest.raises(IntegrityError, match="outbox_strategy_status_version"):
        with db.transaction() as s:
            emit(s, STATUS_CHANGED, scope_id, active.aggregate_version, "dup", active.payload)
    # A stale ACTIVE_PAPER fact arriving out of order is only a fact: consumers acknowledge
    # it, and eligibility is still read from the locked scope row.
    with db.transaction() as s:
        late = {**active.payload, "version": version + 5}
        emit(s, STATUS_CHANGED, scope_id, version + 5, "late", late)
    with db.transaction() as s:
        assert acknowledge_batch(s) > 0
    for bound in (generation, generation + 1):
        blockers = eligibility(db, scope_id, bound).scopes[0].blockers
        assert el.SCOPE_NOT_ACTIVE_PAPER in blockers
    # Replaying the suspension changes nothing, and a stale command cannot win.
    command(db, Status.SUSPENDED, scope_id, version=version, command_id=suspend_id)
    with pytest.raises(StrategyRefusal, match="VERSION_CONFLICT"):
        command(db, Status.SUSPENDED, scope_id, version=version)
    # A writer restoring the old row must advance the generation (the schema refuses
    # otherwise), and that leaves the old activation stale.
    with pytest.raises(DBAPIError, match="advance"):
        execute(
            db,
            "UPDATE strategy_scopes SET state = 'ACTIVE_PAPER' WHERE id = :scope",
            scope=scope_id,
        )
    execute(
        db,
        f"UPDATE strategy_scopes SET state = 'ACTIVE_PAPER', {ADVANCE} WHERE id = :scope",
        scope=scope_id,
    )
    decision = eligibility(db, scope_id, generation + 2)
    assert decision.scopes[0].blockers == (el.ACTIVATION_STALE_GENERATION,)


def test_seeded_placeholders_cannot_become_eligible(db):
    # Even with FIXTURE records, the v0 placeholder on the unapproved horizon stays blocked.
    with db.transaction() as s:
        scope = s.get(ScopeRow, MOMENTUM)
        fx.activate(s, scope)
        generation = scope.eligibility_generation
    decision = eligibility(db, MOMENTUM, generation)
    assert decision.scopes[0].blockers == (el.POD_UNIMPLEMENTED, el.HORIZON_UNAPPROVED)
    # Without an approved qualification policy nothing is eligible (OD-15).
    no_policy = eligibility(db, MOMENTUM, generation, policies=())
    assert el.QUALIFICATION_POLICY_NOT_APPROVED in no_policy.scopes[0].blockers
    assert eligibility(db, "no-such-scope", 1).scopes[0].blockers == (el.UNKNOWN_SCOPE,)
