from dataclasses import asdict
from typing import Any

from fastapi import APIRouter, Depends

from aura.contracts import Overview
from aura.ledger import snapshot
from aura.operations import locked_gate
from aura.routes.dependencies import DatabaseDep, owner
from aura.strategies import PODS

router = APIRouter()


@router.get("/api/overview", dependencies=[Depends(owner)], response_model=Overview)
def overview(db: DatabaseDep) -> dict[str, Any]:
    with db.transaction() as session:
        portfolio = snapshot(session, "challenge")
        gate = locked_gate(session)
        return {
            "portfolio": portfolio,
            "mode": "OBSERVE",
            "risk_profile": "BALANCED",
            "controls": {
                "version": gate.version,
                "entry_halt": gate.entry_halt,
                "full_kill": gate.full_kill,
            },
            "strategies": [asdict(pod) for pod in PODS],
            "integrations": [
                {"name": name, "status": "NOT_CONFIGURED"}
                for name in ("Market data", "Paper adapter", "Investment Committee")
            ],
            "regime": {"status": "NOT_READY", "reason": "Definition and data not qualified"},
            "execution_enabled": False,
        }
