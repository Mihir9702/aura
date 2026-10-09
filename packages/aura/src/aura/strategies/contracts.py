"""API contracts of strategy scopes: reads, audit history and owner commands.

A scope binds one Pod version to one horizon version (and, once OD-02 is decided, a
universe). Its lifecycle state and eligibility generation are authoritative only in
PostgreSQL; these are read models. There is no qualify or activate command because no
approved qualification policy exists (OD-15).
"""

from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

Reason = Annotated[str, StringConstraints(strip_whitespace=True, min_length=4, max_length=500)]
ScopeState = Literal[
    "DEVELOPMENT", "BACKTESTING", "PAPER_SHADOW", "QUALIFIED", "ACTIVE_PAPER", "SUSPENDED"
]
PodName = Literal["MOMENTUM", "BREAKOUT", "EVENT_CATALYST", "MEAN_REVERSION", "SWING_TREND"]
ImplementationStatus = Literal["UNIMPLEMENTED", "IMPLEMENTED", "VERIFIED"]
HorizonApproval = Literal["UNAPPROVED", "APPROVED"]


class _Contract(BaseModel):
    model_config = ConfigDict(extra="forbid")


class StrategyPodVersion(_Contract):
    """A Pod methodology version. Version 0 is the unimplemented placeholder."""

    id: str
    version: int
    name: PodName
    display_name: str
    implementation_status: ImplementationStatus


class StrategyHorizon(_Contract):
    """A horizon specification; UNAPPROVED until its definition is decided (OD-03)."""

    id: str
    version: int
    approval: HorizonApproval
    decision_ref: str
    description: str


class StrategyScope(_Contract):
    """Lifecycle state of one Pod version and horizon version.

    Every status change advances `version` and `eligibility_generation`. `universe_ref` is
    null while no universe is approved (OD-02).
    """

    id: str
    pod: StrategyPodVersion
    horizon: StrategyHorizon
    universe_ref: str | None
    state: ScopeState
    previous_state: ScopeState | None
    version: int
    eligibility_generation: int
    qualification_id: str | None
    activation_id: str | None
    reason: str
    actor: str
    changed_at: datetime


class StrategyScopeCommand(_Contract):
    """Suspend or Resume. Replaying a command_id returns the recorded result; reusing it with
    a different body, scope or command is refused with 409."""

    command_id: UUID
    expected_version: int = Field(ge=1)
    reason: Reason


class StrategyScopeStatus(_Contract):
    """The state a Suspend or Resume command recorded."""

    scope_id: str
    state: ScopeState
    version: int
    eligibility_generation: int


class StrategyStatusChange(_Contract):
    """One STRATEGY_STATUS_CHANGED audit event of a scope. History lists oldest first."""

    sequence: int
    event_id: str
    command_id: str
    scope_id: str
    prior_state: ScopeState
    new_state: ScopeState
    prior_version: int
    version: int
    prior_generation: int
    generation: int
    actor: str
    reason: str
    recorded_at: datetime
