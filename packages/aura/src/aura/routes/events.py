from typing import Any

from fastapi import APIRouter, Depends

from aura.contracts import EventRecord
from aura.events import recent
from aura.routes.dependencies import DatabaseDep, owner

router = APIRouter()


@router.get("/api/events", dependencies=[Depends(owner)], response_model=list[EventRecord])
def events(db: DatabaseDep) -> list[dict[str, Any]]:
    with db.transaction() as session:
        return recent(session)
