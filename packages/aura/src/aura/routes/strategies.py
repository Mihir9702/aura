"""Strategy scope reads, audit history and the owner's Suspend and Resume commands.

There is no qualify or activate command: no approved qualification policy exists (OD-15).
Refused commands return 409 with a detail that starts with the refusal code.
"""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Path

from aura import strategies
from aura.routes.dependencies import DatabaseDep, owner
from aura.storage import Database
from aura.strategies.contracts import (
    StrategyScope,
    StrategyScopeCommand,
    StrategyScopeStatus,
    StrategyStatusChange,
)

router = APIRouter()

ScopeId = Annotated[str, Path(min_length=1, max_length=200)]


def _unknown(scope_id: str) -> HTTPException:
    return HTTPException(404, f"Unknown strategy scope {scope_id}")


@router.get(
    "/api/strategies/scopes", dependencies=[Depends(owner)], response_model=list[StrategyScope]
)
def list_strategy_scopes(db: DatabaseDep) -> list[StrategyScope]:
    with db.transaction() as session:
        return strategies.list_scopes(session)


@router.get(
    "/api/strategies/scopes/{scope_id}",
    dependencies=[Depends(owner)],
    response_model=StrategyScope,
)
def get_strategy_scope(scope_id: ScopeId, db: DatabaseDep) -> StrategyScope:
    try:
        with db.transaction() as session:
            return strategies.get_scope(session, scope_id)
    except strategies.UnknownScope:
        raise _unknown(scope_id) from None


@router.get(
    "/api/strategies/scopes/{scope_id}/history",
    dependencies=[Depends(owner)],
    response_model=list[StrategyStatusChange],
)
def strategy_scope_history(scope_id: ScopeId, db: DatabaseDep) -> list[StrategyStatusChange]:
    try:
        with db.transaction() as session:
            return strategies.scope_history(session, scope_id)
    except strategies.UnknownScope:
        raise _unknown(scope_id) from None


def _command(
    db: Database, scope_id: str, target: strategies.Status, body: StrategyScopeCommand
) -> StrategyScopeStatus:
    try:
        with db.transaction() as session:
            return strategies.transition(
                session,
                scope_id,
                target,
                expected_version=body.expected_version,
                command_id=str(body.command_id),
                reason=body.reason,
                actor="owner",
            )
    except strategies.UnknownScope:
        raise _unknown(scope_id) from None


@router.post(
    "/api/strategies/scopes/{scope_id}/suspend",
    dependencies=[Depends(owner)],
    response_model=StrategyScopeStatus,
)
def suspend_strategy_scope(
    scope_id: ScopeId, body: StrategyScopeCommand, db: DatabaseDep
) -> StrategyScopeStatus:
    """Suspend from any state: blocks new or increasing exposure, advances the generation."""
    return _command(db, scope_id, strategies.Status.SUSPENDED, body)


@router.post(
    "/api/strategies/scopes/{scope_id}/resume",
    dependencies=[Depends(owner)],
    response_model=StrategyScopeStatus,
)
def resume_strategy_scope(
    scope_id: ScopeId, body: StrategyScopeCommand, db: DatabaseDep
) -> StrategyScopeStatus:
    """Resume a suspended scope to DEVELOPMENT only."""
    return _command(db, scope_id, strategies.Status.DEVELOPMENT, body)
