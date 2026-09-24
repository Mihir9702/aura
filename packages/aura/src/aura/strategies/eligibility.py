"""Entry eligibility of strategy scopes, checked under the documented lock order.

Every path that decides or changes eligibility locks the global gate first
(aura.operations.locked_gate), then strategy scopes one at a time in sorted id order, so
admission and suspension cannot deadlock. A future portfolio gate slots in between: a caller
locks global, then portfolio, then calls lock_scopes (re-locking global is a no-op). An
admission binds the eligibility generation that was observed when its entry was authorized.
Every status change advances the generation, so an entry authorized before a suspension
cannot be admitted after the suspension commits. Eligibility is decided from the locked rows
only, never from events or read models.

No approved qualification policy exists (OD-15), so a runtime caller has no approved policy
to pass and no scope can be eligible. Tests pass explicit FIXTURE_ policy references.
"""

from collections.abc import Collection, Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from aura.operations import locked_gate
from aura.strategies.lifecycle import Status
from aura.strategies.models import (
    ActivationRow,
    HorizonRow,
    PodVersionRow,
    QualificationRow,
    ScopeRow,
)

UNKNOWN_SCOPE = "UNKNOWN_SCOPE"
STALE_ELIGIBILITY_GENERATION = "STALE_ELIGIBILITY_GENERATION"
SCOPE_NOT_ACTIVE_PAPER = "SCOPE_NOT_ACTIVE_PAPER"
POD_UNIMPLEMENTED = "POD_UNIMPLEMENTED"
HORIZON_UNAPPROVED = "HORIZON_UNAPPROVED"
QUALIFICATION_MISSING = "QUALIFICATION_MISSING"
QUALIFICATION_SCOPE_MISMATCH = "QUALIFICATION_SCOPE_MISMATCH"
QUALIFICATION_POLICY_NOT_APPROVED = "QUALIFICATION_POLICY_NOT_APPROVED"
QUALIFICATION_EXPIRED = "QUALIFICATION_EXPIRED"
ACTIVATION_MISSING = "ACTIVATION_MISSING"
ACTIVATION_SCOPE_MISMATCH = "ACTIVATION_SCOPE_MISMATCH"
ACTIVATION_PORTFOLIO_MISMATCH = "ACTIVATION_PORTFOLIO_MISMATCH"
ACTIVATION_DISABLED = "ACTIVATION_DISABLED"
ACTIVATION_STALE_GENERATION = "ACTIVATION_STALE_GENERATION"


@dataclass(frozen=True)
class ScopeEligibility:
    scope_id: str
    state: str | None
    generation: int | None
    blockers: tuple[str, ...]


@dataclass(frozen=True)
class EligibilityDecision:
    """Per-scope results in lock (sorted id) order. Evidence for one admission, not authority
    that can be reused later: a later admission must check again."""

    scopes: tuple[ScopeEligibility, ...]

    @property
    def permitted(self) -> bool:
        return bool(self.scopes) and all(not scope.blockers for scope in self.scopes)

    @property
    def reasons(self) -> tuple[str, ...]:
        return tuple(f"{s.scope_id}:{code}" for s in self.scopes for code in s.blockers)


def lock_scopes(session: Session, scope_ids: Iterable[str]) -> dict[str, ScopeRow]:
    """Lock the global gate, then each named scope in sorted id order; return those found.

    Rows are re-read under the lock (populate_existing), never taken from the session cache.
    """
    locked_gate(session)
    locked: dict[str, ScopeRow] = {}
    for scope_id in sorted(set(scope_ids)):
        row = session.scalars(
            select(ScopeRow)
            .where(ScopeRow.id == scope_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        ).one_or_none()
        if row is not None:
            locked[scope_id] = row
    return locked


def entry_blockers(
    scope: ScopeRow,
    pod: PodVersionRow | None,
    horizon: HorizonRow | None,
    qualification: QualificationRow | None,
    activation: ActivationRow | None,
    *,
    expected_generation: int,
    portfolio_id: str,
    approved_policies: Collection[str],
    at: datetime,
) -> tuple[str, ...]:
    """Reasons one scope cannot admit an entry now; empty when it can.

    A qualification or activation counts only for the exact scope, Pod version and horizon
    version it was approved for, so an approval cannot be reused by another version or
    horizon. An activation is valid only at the eligibility generation it was granted at.
    """
    blockers: list[str] = []
    if scope.eligibility_generation != expected_generation:
        blockers.append(STALE_ELIGIBILITY_GENERATION)
    if scope.state != Status.ACTIVE_PAPER:
        blockers.append(SCOPE_NOT_ACTIVE_PAPER)
    if pod is None or pod.implementation_status == "UNIMPLEMENTED":
        blockers.append(POD_UNIMPLEMENTED)
    if horizon is None or horizon.approval != "APPROVED":
        blockers.append(HORIZON_UNAPPROVED)
    if qualification is None:
        blockers.append(QUALIFICATION_MISSING)
    else:
        bound = (
            qualification.id,
            qualification.scope_id,
            qualification.pod_id,
            qualification.pod_version,
            qualification.horizon_id,
            qualification.horizon_version,
        )
        if bound != (
            scope.qualification_id,
            scope.id,
            scope.pod_id,
            scope.pod_version,
            scope.horizon_id,
            scope.horizon_version,
        ):
            blockers.append(QUALIFICATION_SCOPE_MISMATCH)
        if qualification.policy_ref not in approved_policies:
            blockers.append(QUALIFICATION_POLICY_NOT_APPROVED)
        if qualification.valid_until is not None and qualification.valid_until <= at:
            blockers.append(QUALIFICATION_EXPIRED)
    if activation is None:
        blockers.append(ACTIVATION_MISSING)
    else:
        if (activation.id, activation.scope_id, activation.qualification_id) != (
            scope.activation_id,
            scope.id,
            scope.qualification_id,
        ):
            blockers.append(ACTIVATION_SCOPE_MISMATCH)
        if activation.portfolio_id != portfolio_id:
            blockers.append(ACTIVATION_PORTFOLIO_MISMATCH)
        if activation.state != "ENABLED":
            blockers.append(ACTIVATION_DISABLED)
        if activation.generation != scope.eligibility_generation:
            blockers.append(ACTIVATION_STALE_GENERATION)
    return tuple(blockers)


def check_entry_eligibility(
    session: Session,
    expected_generations: Mapping[str, int],
    *,
    portfolio_id: str,
    approved_policies: Collection[str],
    at: datetime,
) -> EligibilityDecision:
    """Decide ENTRY_INCREASE eligibility for every scope an entry binds.

    `expected_generations` maps each scope id to the eligibility generation observed when the
    entry was authorized. The locks last until the caller's transaction ends, so the caller
    records its admission in the same transaction. Exits never call this: suspension blocks
    entries only, and held lots keep their monitoring and reducing exits.
    """
    if not expected_generations:
        raise ValueError("An entry binds at least one strategy scope")
    if isinstance(approved_policies, str):
        raise TypeError("approved_policies is a collection of policy references")
    if at.tzinfo is None:
        raise ValueError("A timezone-aware admission time is required")
    approved = frozenset(approved_policies)
    rows = lock_scopes(session, expected_generations)
    results = []
    for scope_id in sorted(expected_generations):
        scope = rows.get(scope_id)
        if scope is None:
            results.append(ScopeEligibility(scope_id, None, None, (UNKNOWN_SCOPE,)))
            continue
        pod = session.get(PodVersionRow, (scope.pod_id, scope.pod_version), populate_existing=True)
        horizon = session.get(
            HorizonRow, (scope.horizon_id, scope.horizon_version), populate_existing=True
        )
        qualification = (
            session.get(QualificationRow, scope.qualification_id, populate_existing=True)
            if scope.qualification_id
            else None
        )
        activation = (
            session.get(ActivationRow, scope.activation_id, populate_existing=True)
            if scope.activation_id
            else None
        )
        blockers = entry_blockers(
            scope,
            pod,
            horizon,
            qualification,
            activation,
            expected_generation=expected_generations[scope_id],
            portfolio_id=portfolio_id,
            approved_policies=approved,
            at=at,
        )
        results.append(
            ScopeEligibility(scope.id, scope.state, scope.eligibility_generation, blockers)
        )
    return EligibilityDecision(tuple(results))
