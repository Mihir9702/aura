"""Owner job routes on real PostgreSQL: reads, the audited redrive command and its conflicts."""

import os
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timedelta
from uuid import uuid4

import pytest
from aura.api import create_app
from aura.common import now
from aura.config import Settings
from aura.events import Event
from aura.orchestration.jobs import Capacity, Job, WorkClass
from aura.orchestration.registry import JobContext, JobSpec, PermanentJobError, Registry
from aura.orchestration.runner import Backoff, JobRunner, Outcome
from fastapi.testclient import TestClient
from sqlalchemy import select

pytestmark = pytest.mark.integration

COMMAND = {"X-Aura-Command": "1"}
JOB_FIELDS = [
    "id",
    "kind",
    "job_class",
    "scope",
    "occurrence_key",
    "correlation_id",
    "payload",
    "input_version",
    "priority",
    "state",
    "version",
    "attempts",
    "max_attempts",
    "redrives",
    "fencing_token",
    "lease_owner",
    "lease_expires_at",
    "scheduled_at",
    "available_at",
    "deadline",
    "terminal_reason",
    "last_error",
    "result",
    "created_at",
    "updated_at",
    "finished_at",
]


def reject(context: JobContext) -> None:
    raise PermanentJobError("fixture input rejected")


REJECTING = Registry([JobSpec("fixture.reject", WorkClass.MONITORING, reject, "Always rejects")])


@contextmanager
def owner_client() -> Iterator[TestClient]:
    settings = Settings(database_url=os.environ["AURA_TEST_DATABASE_URL"], owner_key="x" * 40)
    with TestClient(create_app(settings)) as client:
        response = client.post("/api/session", json={"key": "x" * 40}, headers=COMMAND)
        assert response.status_code == 200
        yield client


def create(db, key: str, at: datetime, deadline: datetime | None = None) -> Job:
    with db.transaction() as s:
        return REJECTING.enqueue(
            s,
            kind="fixture.reject",
            scope="fixture",
            occurrence_key=key,
            scheduled_at=at,
            deadline=deadline,
            priority=0,
            max_attempts=2,
            payload={"key": key},
            correlation_id="fixture-" + key,
            at=at,
        )


def dead_letter(db, key: str, at: datetime, deadline: datetime | None = None) -> Job:
    """A job dead-lettered by a permanent failure at the fixture instant `at`."""
    job = create(db, key, at, deadline)
    worker = JobRunner(
        db,
        REJECTING,
        worker="fixture-worker",
        capacity=Capacity(slots=2, safety_reserved=1),
        lease=timedelta(seconds=30),
        backoff=Backoff(base=timedelta(seconds=1), cap=timedelta(seconds=1)),
        clock=lambda: at,
    )
    lease = worker.claim()
    assert lease is not None and lease.job_id == job.id
    assert worker.run(lease) == Outcome.DEAD_LETTERED
    with db.transaction() as s:
        dead = s.get(Job, job.id)
        assert dead is not None
        return dead


def redrive_body(version: int, reason: str = "fixture corrected", command_id: str = "") -> dict:
    return {
        "command_id": command_id or str(uuid4()),
        "expected_version": version,
        "reason": reason,
    }


def test_job_routes_require_the_owner_session(db):
    settings = Settings(database_url=os.environ["AURA_TEST_DATABASE_URL"], owner_key="x" * 40)
    with TestClient(create_app(settings)) as client:
        assert client.get("/api/jobs").status_code == 401
        assert client.get("/api/jobs/dead-letters").status_code == 401
        body = redrive_body(1)
        path = f"/api/jobs/{uuid4()}/redrive"
        assert client.post(path, json=body, headers=COMMAND).status_code == 401
        assert client.post(path, json=body).status_code == 403  # command header required


def test_job_reads_list_records_newest_first_and_filter_by_state(db):
    at = now()
    dead = dead_letter(db, "listed-dead", at)
    queued = create(db, "listed-queued", at + timedelta(seconds=1))
    with owner_client() as client:
        listed = client.get("/api/jobs").json()
        assert [record["id"] for record in listed] == [queued.id, dead.id]
        assert all(list(record) == JOB_FIELDS for record in listed)
        assert listed[1]["job_class"] == "MONITORING"
        assert (listed[1]["state"], listed[1]["terminal_reason"]) == ("DEAD", "PERMANENT_FAILURE")
        assert datetime.fromisoformat(listed[1]["finished_at"]).tzinfo is not None
        filtered = client.get("/api/jobs", params={"state": "QUEUED"}).json()
        assert [record["id"] for record in filtered] == [queued.id]
        assert client.get("/api/jobs", params={"state": "PAUSED"}).status_code == 422
        assert [r["id"] for r in client.get("/api/jobs/dead-letters").json()] == [dead.id]


def test_redrive_rejects_stale_invalid_and_reused_commands(db):
    at = now()
    dead = dead_letter(db, "conflicts", at)
    queued = create(db, "not-dead", at)
    path = f"/api/jobs/{dead.id}/redrive"
    with owner_client() as client:
        assert (
            client.post(path, json=redrive_body(dead.version - 1), headers=COMMAND).status_code
            == 409
        )
        assert (
            client.post(
                f"/api/jobs/{uuid4()}/redrive", json=redrive_body(1), headers=COMMAND
            ).status_code
            == 404
        )
        assert (
            client.post(
                f"/api/jobs/{queued.id}/redrive", json=redrive_body(queued.version), headers=COMMAND
            ).status_code
            == 409
        )
        extra = {**redrive_body(dead.version), "attempts": 0}
        assert client.post(path, json=extra, headers=COMMAND).status_code == 422
        assert (
            client.post(path, json=redrive_body(dead.version, "no"), headers=COMMAND).status_code
            == 422
        )

        command = redrive_body(dead.version)
        assert client.post(path, json=command, headers=COMMAND).status_code == 200
        changed = {**command, "reason": "a different reason"}
        assert client.post(path, json=changed, headers=COMMAND).status_code == 409
        elsewhere = {**command, "expected_version": queued.version}
        other = f"/api/jobs/{queued.id}/redrive"
        assert client.post(other, json=elsewhere, headers=COMMAND).status_code == 409
    with db.transaction() as s:
        audited = s.scalars(select(Event).where(Event.event_type == "JOB_REDRIVEN")).all()
        assert [(e.aggregate_id, e.correlation_id) for e in audited] == [
            (dead.id, command["command_id"])
        ]


def test_redrive_refuses_a_job_whose_deadline_passed(db):
    at = now() - timedelta(minutes=10)
    dead = dead_letter(db, "too-late", at, deadline=at + timedelta(minutes=5))
    with owner_client() as client:
        response = client.post(
            f"/api/jobs/{dead.id}/redrive", json=redrive_body(dead.version), headers=COMMAND
        )
        assert response.status_code == 409
        assert "deadline" in response.json()["detail"]
