from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from aura.common import now
from aura.contracts import JobCommandResult, JobRecord
from aura.orchestration import jobs
from aura.orchestration.jobs import JobState
from aura.routes.dependencies import DatabaseDep, owner

router = APIRouter()


@router.get("/api/jobs", dependencies=[Depends(owner)], response_model=list[JobRecord])
def list_jobs(db: DatabaseDep, state: JobState | None = None) -> list[dict[str, Any]]:
    with db.transaction() as session:
        return [jobs.record(job) for job in jobs.recent(session, state)]


@router.get("/api/jobs/dead-letters", dependencies=[Depends(owner)], response_model=list[JobRecord])
def dead_letters(db: DatabaseDep) -> list[dict[str, Any]]:
    with db.transaction() as session:
        return [jobs.record(job) for job in jobs.dead_letters(session)]


class RedriveCommand(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_version: int = Field(ge=1)
    reason: str = Field(min_length=4, max_length=500)
    command_id: UUID


@router.post(
    "/api/jobs/{job_id}/redrive",
    dependencies=[Depends(owner)],
    response_model=JobCommandResult,
)
def redrive(job_id: str, body: RedriveCommand, db: DatabaseDep) -> dict[str, Any]:
    """Requeue a dead letter with its identity; audited as JOB_REDRIVEN in the outbox."""
    try:
        with db.transaction() as session:
            version = jobs.redrive(
                session,
                job_id=job_id,
                expected_version=body.expected_version,
                reason=body.reason,
                command_id=str(body.command_id),
                actor="owner",
                at=now(),
            )
    except jobs.JobNotFound:
        raise HTTPException(404, "Unknown job") from None
    return {"job_id": job_id, "version": version}
