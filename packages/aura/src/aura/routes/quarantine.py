"""Execution quarantine: list open entries and redrive one (owner command, audited).

Redrive re-validates the quarantined receipt and applies it at most once. It carries
command_id and expected_version, and each attempt is recorded as a FIXTURE_FILL_REDRIVEN
outbox event. SHADOW fixtures only; there is no order route.
"""

from dataclasses import asdict
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from aura.contracts import QuarantineRecord, RedriveResult
from aura.execution import open_quarantine, redrive_quarantine
from aura.routes.dependencies import DatabaseDep, owner

router = APIRouter()


class RedriveCommand(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_version: int = Field(ge=1)
    reason: str = Field(min_length=4, max_length=500)
    command_id: UUID


@router.get(
    "/api/execution/quarantine",
    dependencies=[Depends(owner)],
    response_model=list[QuarantineRecord],
)
def quarantine(db: DatabaseDep) -> list[dict[str, Any]]:
    with db.transaction() as session:
        return open_quarantine(session)


@router.post(
    "/api/execution/quarantine/{quarantine_id}/redrive",
    dependencies=[Depends(owner)],
    response_model=RedriveResult,
)
def redrive(quarantine_id: str, body: RedriveCommand, db: DatabaseDep) -> dict[str, Any]:
    try:
        with db.transaction() as session:
            outcome = redrive_quarantine(
                session,
                quarantine_id=quarantine_id,
                expected_version=body.expected_version,
                command_id=str(body.command_id),
                reason=body.reason,
            )
    except LookupError as missing:
        raise HTTPException(404, str(missing)) from missing
    return asdict(outcome)
