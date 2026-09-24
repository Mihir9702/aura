"""Implemented API contracts. Trading proposal contracts remain design specifications."""

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Position(Contract):
    instrument: str
    scope: str
    quantity: str
    cost_basis: str


class PortfolioSnapshot(Contract):
    id: str
    version: int
    currency: Literal["USD"]
    environment: Literal["ACTIVE_PAPER", "SHADOW"]
    capital: str
    cash: str
    realized_pnl: str
    fees: str
    valuation_status: Literal["NO_MARKS", "CASH_ONLY"]
    equity: str | None
    positions: list[Position]


class Controls(Contract):
    version: int
    entry_halt: bool
    full_kill: bool


class Strategy(Contract):
    id: str
    name: str
    description: str
    status: str
    implementation: str


class Integration(Contract):
    name: str
    status: Literal["NOT_CONFIGURED"]


class RegimeReadiness(Contract):
    status: Literal["NOT_READY"]
    reason: str


class Overview(Contract):
    portfolio: PortfolioSnapshot
    mode: Literal["OBSERVE"]
    risk_profile: Literal["BALANCED"]
    controls: Controls
    strategies: list[Strategy]
    integrations: list[Integration]
    regime: RegimeReadiness
    execution_enabled: Literal[False]


class JournalRecord(Contract):
    """A committed Ledger journal of the challenge portfolio."""

    id: str
    source: str
    facts: dict[str, Any]


class EventRecord(Contract):
    """An outbox event. Feeds list the newest (highest sequence) first; not a cursor."""

    sequence: int
    event_id: str
    event_type: str
    aggregate_id: str
    aggregate_version: int
    correlation_id: str
    payload: dict[str, Any]
    recorded_at: str
    schema_version: int
