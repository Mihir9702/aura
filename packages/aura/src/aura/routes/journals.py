from typing import Any

from fastapi import APIRouter, Depends
from sqlalchemy import select

from aura.contracts import JournalRecord
from aura.ledger import Journal
from aura.routes.dependencies import DatabaseDep, owner

router = APIRouter()


@router.get(
    "/api/journals", dependencies=[Depends(owner)], response_model=list[JournalRecord]
)
def journals(db: DatabaseDep) -> list[dict[str, Any]]:
    with db.transaction() as session:
        rows = session.scalars(
            select(Journal)
            .where(Journal.portfolio_id == "challenge")
            .order_by(Journal.source_key)
            .limit(100)
        ).all()
        return [{"id": row.id, "source": row.source_key, "facts": row.facts} for row in rows]
