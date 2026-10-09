from fastapi import APIRouter
from sqlalchemy import text

from aura.routes.dependencies import DatabaseDep

router = APIRouter()


@router.get("/api/health")
def health(db: DatabaseDep) -> dict[str, str]:
    with db.transaction() as session:
        session.execute(text("SELECT 1"))
    return {"database": "CONNECTED", "execution": "DISABLED", "environment": "LOCAL"}
