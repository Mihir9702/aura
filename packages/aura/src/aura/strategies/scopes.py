"""Persisted strategy scopes: the owner's lifecycle commands and audited reads.

A command locks the global gate and then its scope (the lock order of
aura.strategies.eligibility), checks command_id and expected_version, applies the lifecycle
rules and records STRATEGY_STATUS_CHANGED in the outbox in the same transaction. That event is
the audit record and the command's idempotency key. Every applied change advances the scope
version and eligibility generation, which invalidates entries authorized before it.
"""

from typing import Any

from sqlalchemy import and_, select
from sqlalchemy.orm import Session

from aura.common import Conflict, now
from aura.events import Event, emit
from aura.strategies.contracts import StrategyScope, StrategyScopeStatus, StrategyStatusChange
from aura.strategies.eligibility import lock_scopes
from aura.strategies.lifecycle import REFUSALS, Status, scope_transition_refusal
from aura.strategies.models import HorizonRow, PodVersionRow, ScopeRow

STATUS_CHANGED = "STRATEGY_STATUS_CHANGED"
VERSION_CONFLICT = "VERSION_CONFLICT"
COMMAND_REUSED = "COMMAND_REUSED"


class StrategyRefusal(Conflict):
    """A refused scope command. `code` is machine-readable; the message starts with it."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code


class UnknownScope(LookupError):
    """No strategy scope has the requested id."""


def transition(
    session: Session,
    scope_id: str,
    target: Status,
    *,
    expected_version: int,
    command_id: str,
    reason: str,
    actor: str,
) -> StrategyScopeStatus:
    """Apply one lifecycle command to a scope, or replay the result it already recorded.

    QUALIFIED and ACTIVE_PAPER are refused with QUALIFICATION_POLICY_MISSING (OD-15);
    SUSPENDED is allowed from any state; DEVELOPMENT is the resume target of a suspended
    scope only. Refusals raise StrategyRefusal and record nothing.
    """
    if not (command_id and reason.strip() and actor.strip()):
        raise ValueError("A command_id, reason and actor are required")
    # Gate, then scope, before the replay lookup, so identical commands serialize.
    scope = lock_scopes(session, [scope_id]).get(scope_id)
    recorded = session.scalars(
        select(Event).where(Event.correlation_id == command_id).order_by(Event.sequence)
    ).first()
    if recorded is not None:
        return _replay(recorded, scope_id, target, expected_version, reason, actor)
    if scope is None:
        raise UnknownScope(scope_id)
    if scope.version != expected_version:
        raise StrategyRefusal(VERSION_CONFLICT, "Scope changed; refresh before trying again")
    code = scope_transition_refusal(Status(scope.state), target)
    if code is not None:
        raise StrategyRefusal(code, REFUSALS[code])
    prior_state, prior_version = scope.state, scope.version
    prior_generation = scope.eligibility_generation
    scope.previous_state = prior_state
    scope.state = target.value
    scope.version = prior_version + 1
    scope.eligibility_generation = prior_generation + 1
    scope.reason = reason
    scope.actor = actor
    scope.changed_at = now()
    if target == Status.DEVELOPMENT:
        # A resumed scope starts the lifecycle again; earlier approvals do not carry over.
        scope.qualification_id = None
        scope.activation_id = None
    emit(
        session,
        STATUS_CHANGED,
        scope.id,
        scope.version,
        command_id,
        {
            "scope_id": scope.id,
            "pod": {"id": scope.pod_id, "version": scope.pod_version},
            "horizon": {"id": scope.horizon_id, "version": scope.horizon_version},
            "prior_state": prior_state,
            "new_state": scope.state,
            "prior_version": prior_version,
            "version": scope.version,
            "prior_generation": prior_generation,
            "generation": scope.eligibility_generation,
            "actor": actor,
            "reason": reason,
        },
    )
    session.flush()
    return StrategyScopeStatus.model_validate(
        {
            "scope_id": scope.id,
            "state": scope.state,
            "version": scope.version,
            "eligibility_generation": scope.eligibility_generation,
        }
    )


def suspend(
    session: Session,
    scope_id: str,
    *,
    expected_version: int,
    command_id: str,
    reason: str,
    actor: str,
) -> StrategyScopeStatus:
    """Block new or increasing exposure for the scope; held lots keep monitoring and exits."""
    return transition(
        session,
        scope_id,
        Status.SUSPENDED,
        expected_version=expected_version,
        command_id=command_id,
        reason=reason,
        actor=actor,
    )


def resume(
    session: Session,
    scope_id: str,
    *,
    expected_version: int,
    command_id: str,
    reason: str,
    actor: str,
) -> StrategyScopeStatus:
    """Return a suspended scope to DEVELOPMENT; it must progress and qualify again."""
    return transition(
        session,
        scope_id,
        Status.DEVELOPMENT,
        expected_version=expected_version,
        command_id=command_id,
        reason=reason,
        actor=actor,
    )


def _replay(
    event: Event, scope_id: str, target: Status, expected_version: int, reason: str, actor: str
) -> StrategyScopeStatus:
    payload = event.payload
    command = (scope_id, target.value, expected_version, reason, actor)
    if event.event_type != STATUS_CHANGED or command != (
        payload.get("scope_id"),
        payload.get("new_state"),
        payload.get("prior_version"),
        payload.get("reason"),
        payload.get("actor"),
    ):
        raise StrategyRefusal(COMMAND_REUSED, "Command identity reused with different content")
    return StrategyScopeStatus.model_validate(
        {
            "scope_id": scope_id,
            "state": payload["new_state"],
            "version": payload["version"],
            "eligibility_generation": payload["generation"],
        }
    )


def _scopes(session: Session, scope_id: str | None = None) -> list[StrategyScope]:
    query = (
        select(ScopeRow, PodVersionRow, HorizonRow)
        .join(
            PodVersionRow,
            and_(
                PodVersionRow.id == ScopeRow.pod_id, PodVersionRow.version == ScopeRow.pod_version
            ),
        )
        .join(
            HorizonRow,
            and_(
                HorizonRow.id == ScopeRow.horizon_id,
                HorizonRow.version == ScopeRow.horizon_version,
            ),
        )
        .order_by(ScopeRow.id)
    )
    if scope_id is not None:
        query = query.where(ScopeRow.id == scope_id)
    return [_view(scope, pod, horizon) for scope, pod, horizon in session.execute(query).tuples()]


def _view(scope: ScopeRow, pod: PodVersionRow, horizon: HorizonRow) -> StrategyScope:
    fields: dict[str, Any] = {
        "id": scope.id,
        "pod": {
            "id": pod.id,
            "version": pod.version,
            "name": pod.name,
            "display_name": pod.display_name,
            "implementation_status": pod.implementation_status,
        },
        "horizon": {
            "id": horizon.id,
            "version": horizon.version,
            "approval": horizon.approval,
            "decision_ref": horizon.decision_ref,
            "description": horizon.description,
        },
        "universe_ref": scope.universe_ref,
        "state": scope.state,
        "previous_state": scope.previous_state,
        "version": scope.version,
        "eligibility_generation": scope.eligibility_generation,
        "qualification_id": scope.qualification_id,
        "activation_id": scope.activation_id,
        "reason": scope.reason,
        "actor": scope.actor,
        "changed_at": scope.changed_at,
    }
    return StrategyScope.model_validate(fields)


def list_scopes(session: Session) -> list[StrategyScope]:
    """Every scope, ordered by id (the lock order)."""
    return _scopes(session)


def get_scope(session: Session, scope_id: str) -> StrategyScope:
    found = _scopes(session, scope_id)
    if not found:
        raise UnknownScope(scope_id)
    return found[0]


def scope_history(session: Session, scope_id: str) -> list[StrategyStatusChange]:
    """The scope's STRATEGY_STATUS_CHANGED events, oldest (lowest version) first."""
    if session.get(ScopeRow, scope_id) is None:
        raise UnknownScope(scope_id)
    events = session.scalars(
        select(Event)
        .where(Event.event_type == STATUS_CHANGED, Event.aggregate_id == scope_id)
        .order_by(Event.aggregate_version, Event.sequence)
    ).all()
    return [
        StrategyStatusChange.model_validate(
            {
                "sequence": event.sequence,
                "event_id": event.event_id,
                "command_id": event.correlation_id,
                "scope_id": event.aggregate_id,
                "prior_state": event.payload["prior_state"],
                "new_state": event.payload["new_state"],
                "prior_version": event.payload["prior_version"],
                "version": event.payload["version"],
                "prior_generation": event.payload["prior_generation"],
                "generation": event.payload["generation"],
                "actor": event.payload["actor"],
                "reason": event.payload["reason"],
                "recorded_at": event.recorded_at,
            }
        )
        for event in events
    ]
