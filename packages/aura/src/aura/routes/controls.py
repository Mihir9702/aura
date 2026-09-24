from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from aura.common import Conflict
from aura.events import Event
from aura.operations import change_control, locked_gate
from aura.routes.dependencies import DatabaseDep, owner

router = APIRouter()


class ControlCommand(BaseModel):
    model_config = ConfigDict(extra="forbid")
    active: bool
    expected_version: int = Field(ge=1)
    reason: str = Field(min_length=4, max_length=500)
    command_id: UUID


@router.post("/api/controls/{control}", dependencies=[Depends(owner)])
def controls(control: str, body: ControlCommand, db: DatabaseDep) -> dict[str, Any]:
    if control not in {"entry_halt", "full_kill"}:
        raise HTTPException(422, "Unknown control")
    with db.transaction() as session:
        locked_gate(session)
        previous = session.scalars(
            select(Event).where(Event.correlation_id == str(body.command_id))
        ).first()
        if previous:
            if previous.event_type != control.upper() + "_CHANGED" or previous.payload != {
                "active": body.active,
                "reason": body.reason,
                "actor": "owner",
                "cancellation_status": "NO_ADAPTER_CONFIGURED",
            }:
                raise Conflict("Command identity reused with different content")
            return {"version": previous.aggregate_version}
        gate = change_control(
            session,
            control,
            body.active,
            body.expected_version,
            body.reason,
            str(body.command_id),
        )
        return {"version": gate.version}
