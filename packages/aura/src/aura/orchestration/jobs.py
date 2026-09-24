"""Durable job records and their leased, fenced state transitions.

A job is a unit of work committed in PostgreSQL. Its identity (id, occurrence key, kind,
class, scope, payload, scheduled time and deadline) never changes; migration
0003_durable_jobs enforces that with a trigger. Workers claim with FOR UPDATE SKIP LOCKED
and receive a lease plus a fencing token. Every worker write re-checks both, so a worker
whose lease expired, or whose token a later claim superseded, cannot commit.

Attempts are at least once: a crash after the claim commits leaves the job RUNNING until
its lease expires, and a later claim retries it. Local effects commit in the same
transaction as completion, so a retry never duplicates them. Exhausted or permanently
failing jobs become visible DEAD letters, and an audited owner command redrives them with
the same identity. Nothing here schedules work: code enqueues jobs explicitly (OD-03).
"""

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from json import dumps
from typing import Any
from uuid import uuid4

from sqlalchemy import BigInteger, DateTime, Integer, String, and_, func, or_, select, text
from sqlalchemy.dialects.postgresql import JSONB, insert
from sqlalchemy.orm import Mapped, Session, mapped_column

from aura.common import Conflict
from aura.events import Event, emit
from aura.storage import Base

Clock = Callable[[], datetime]


class WorkClass(StrEnum):
    """Chapter 08 work classes. Reconciliation and monitoring are safety work."""

    RECONCILIATION = "RECONCILIATION"  # reconciliation, Ledger and health
    MONITORING = "MONITORING"  # deterministic position monitoring
    INGEST = "INGEST"  # market ingest and features
    ANALYSIS = "ANALYSIS"  # discovery and AI analysis
    RESEARCH = "RESEARCH"  # reporting and research


SAFETY_CLASSES = frozenset({WorkClass.RECONCILIATION, WorkClass.MONITORING})


class JobState(StrEnum):
    QUEUED = "QUEUED"  # waiting for available_at, including a retry after backoff
    RUNNING = "RUNNING"  # leased to one worker until lease_expires_at
    SUCCEEDED = "SUCCEEDED"  # terminal; result and local effects committed together
    DEAD = "DEAD"  # terminal dead letter until an owner redrives it
    EXPIRED = "EXPIRED"  # terminal; the deadline passed first


class TerminalReason(StrEnum):
    PERMANENT_FAILURE = "PERMANENT_FAILURE"
    MAX_ATTEMPTS_EXHAUSTED = "MAX_ATTEMPTS_EXHAUSTED"
    DEADLINE_PASSED = "DEADLINE_PASSED"


# Serializes capacity accounting between concurrent claimers ("jobs" in ASCII). Advisory
# locks are scoped to one database, so parallel test databases never contend.
CLAIM_LOCK = 0x6A6F6273
SWEEP_BATCH = 100
ERROR_LIMIT = 500


class LeaseLost(Exception):
    """The worker's lease expired, or a later claim superseded its fencing token."""


class DeadlinePassed(Exception):
    """The job's deadline passed before it could complete."""


class JobNotFound(Exception):
    """No job has the requested id."""


class Job(Base):
    __tablename__ = "jobs"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    kind: Mapped[str] = mapped_column(String)
    job_class: Mapped[str] = mapped_column(String)
    scope: Mapped[str] = mapped_column(String)
    occurrence_key: Mapped[str] = mapped_column(String, unique=True)
    correlation_id: Mapped[str] = mapped_column(String)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB)
    input_version: Mapped[int | None] = mapped_column(Integer)
    priority: Mapped[int] = mapped_column(Integer)
    scheduled_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    available_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    deadline: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    state: Mapped[str] = mapped_column(String)
    version: Mapped[int] = mapped_column(Integer)
    attempts: Mapped[int] = mapped_column(Integer)
    max_attempts: Mapped[int] = mapped_column(Integer)
    redrives: Mapped[int] = mapped_column(Integer)
    fencing_token: Mapped[int] = mapped_column(BigInteger)
    lease_owner: Mapped[str | None] = mapped_column(String)
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    result: Mapped[dict[str, Any] | None] = mapped_column(JSONB(none_as_null=True))
    terminal_reason: Mapped[str | None] = mapped_column(String)
    last_error: Mapped[str | None] = mapped_column(String)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


@dataclass(frozen=True)
class Lease:
    """A worker's claim on one attempt. The fencing token increases on every claim."""

    job_id: str
    kind: str
    owner: str
    fencing_token: int
    attempt: int
    expires_at: datetime


@dataclass(frozen=True)
class Capacity:
    """Concurrent leases across all workers, with slots that only safety work may use.

    The numbers are unapproved operational parameters (OD-03, OD-14). Pass them
    explicitly, or use the labeled CAPACITY_DEV in aura.orchestration.runner.
    """

    slots: int
    safety_reserved: int

    def __post_init__(self) -> None:
        if self.slots < 1 or not 1 <= self.safety_reserved <= self.slots:
            raise ValueError("Capacity needs slots >= 1 and 1..slots reserved for safety work")

    def claimable(self, running: int, running_non_safety: int) -> frozenset[WorkClass]:
        """Classes that may take a new lease. Other work never holds the reserved slots."""
        if running >= self.slots:
            return frozenset()
        if running_non_safety >= self.slots - self.safety_reserved:
            return SAFETY_CLASSES
        return frozenset(WorkClass)


def occurrence_key(schedule_version: str, scope: str, scheduled_at: datetime) -> str:
    """Chapter 08 scheduler occurrence identity: (schedule_version, scope, scheduled_at)."""
    if not schedule_version or not scope:
        raise ValueError("Schedule version and scope are required")
    if scheduled_at.tzinfo is None:
        raise ValueError("Timezone-aware scheduled time required")
    instant = scheduled_at.astimezone(UTC).isoformat()
    return "schedule:" + dumps([schedule_version, scope, instant], separators=(",", ":"))


def _aware(*values: datetime | None) -> None:
    if any(value is not None and value.tzinfo is None for value in values):
        raise ValueError("Timezone-aware timestamps required")


def create(
    session: Session,
    *,
    kind: str,
    work_class: WorkClass,
    scope: str,
    occurrence_key: str,
    scheduled_at: datetime,
    deadline: datetime | None,
    priority: int,
    max_attempts: int,
    payload: dict[str, Any],
    correlation_id: str,
    at: datetime,
    input_version: int | None = None,
) -> Job:
    """Insert a job once per occurrence key. Use Registry.enqueue, which fixes the class."""
    _aware(scheduled_at, deadline, at)
    if not scope or not occurrence_key or not correlation_id:
        raise ValueError("Scope, occurrence key and correlation ID are required")
    if max_attempts < 1:
        raise ValueError("A job needs at least one attempt")
    if deadline is not None and deadline <= scheduled_at:
        raise ValueError("Deadline must follow the scheduled time")
    content = {
        "kind": kind,
        "job_class": work_class.value,
        "scope": scope,
        "payload": payload,
        "input_version": input_version,
        "priority": priority,
        "scheduled_at": scheduled_at,
        "deadline": deadline,
        "max_attempts": max_attempts,
    }
    session.execute(
        insert(Job)
        .values(
            id=str(uuid4()),
            occurrence_key=occurrence_key,
            correlation_id=correlation_id,
            available_at=scheduled_at,
            state=JobState.QUEUED.value,
            version=1,
            attempts=0,
            redrives=0,
            fencing_token=0,
            created_at=at,
            updated_at=at,
            **content,
        )
        .on_conflict_do_nothing(index_elements=[Job.occurrence_key])
    )
    job = session.scalars(select(Job).where(Job.occurrence_key == occurrence_key)).one()
    if {name: getattr(job, name) for name in content} != content:
        raise Conflict("Occurrence key reused with different job content")
    return job


def _terminate(
    session: Session, job: Job, state: JobState, reason: TerminalReason, at: datetime
) -> None:
    job.state = state.value
    job.terminal_reason = reason.value
    job.finished_at = at
    job.lease_owner = None
    job.lease_expires_at = None
    job.version += 1
    job.updated_at = at
    emit(
        session,
        "JOB_DEAD_LETTERED" if state == JobState.DEAD else "JOB_EXPIRED",
        job.id,
        job.version,
        job.correlation_id,
        {
            "job_id": job.id,
            "kind": job.kind,
            "job_class": job.job_class,
            "occurrence_key": job.occurrence_key,
            "attempts": job.attempts,
            "fencing_token": job.fencing_token,
            "reason": reason.value,
        },
    )


def _sweep(session: Session, at: datetime) -> None:
    """Expire overdue jobs and dead-letter crashed final attempts, for every kind."""
    lapsed = and_(Job.state == JobState.RUNNING.value, Job.lease_expires_at <= at)
    stale = session.scalars(
        select(Job)
        .where(
            or_(
                and_(Job.state == JobState.QUEUED.value, Job.deadline <= at),
                and_(lapsed, or_(Job.deadline <= at, Job.attempts >= Job.max_attempts)),
            )
        )
        .order_by(Job.available_at, Job.id)
        .limit(SWEEP_BATCH)
        .with_for_update(skip_locked=True)
    ).all()
    for job in stale:
        if job.deadline is not None and job.deadline <= at:
            _terminate(session, job, JobState.EXPIRED, TerminalReason.DEADLINE_PASSED, at)
        else:
            job.last_error = "Lease expired before the final attempt completed"
            _terminate(session, job, JobState.DEAD, TerminalReason.MAX_ATTEMPTS_EXHAUSTED, at)


def claim(
    session: Session,
    *,
    kinds: Sequence[str],
    worker: str,
    capacity: Capacity,
    lease: timedelta,
    at: datetime,
) -> Lease | None:
    """Lease the next runnable job of the given kinds, safety classes first, or None.

    A QUEUED job, or a RUNNING job whose lease expired (its worker crashed or stalled), is
    runnable once available_at has passed. Rows locked by a running attempt are skipped.
    """
    _aware(at)
    if not worker or lease <= timedelta(0):
        raise ValueError("A worker identity and a positive lease are required")
    session.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": CLAIM_LOCK})
    _sweep(session, at)
    if not kinds:
        return None
    safety = [work_class.value for work_class in SAFETY_CLASSES]
    running, running_non_safety = session.execute(
        select(func.count(), func.count().filter(Job.job_class.not_in(safety))).where(
            Job.state == JobState.RUNNING.value, Job.lease_expires_at > at
        )
    ).one()
    classes = capacity.claimable(running, running_non_safety)
    if not classes:
        return None
    job = session.scalars(
        select(Job)
        .where(
            Job.kind.in_(kinds),
            Job.job_class.in_([work_class.value for work_class in classes]),
            Job.available_at <= at,
            Job.attempts < Job.max_attempts,
            or_(Job.deadline.is_(None), Job.deadline > at),
            or_(
                Job.state == JobState.QUEUED.value,
                and_(Job.state == JobState.RUNNING.value, Job.lease_expires_at <= at),
            ),
        )
        .order_by(Job.job_class.in_(safety).desc(), Job.priority.desc(), Job.available_at, Job.id)
        .limit(1)
        .with_for_update(skip_locked=True)
    ).first()
    if job is None:
        return None
    job.state = JobState.RUNNING.value
    job.attempts += 1
    job.fencing_token += 1
    job.lease_owner = worker
    job.lease_expires_at = at + lease
    job.version += 1
    job.updated_at = at
    return Lease(job.id, job.kind, worker, job.fencing_token, job.attempts, at + lease)


def _locked(session: Session, job_id: str) -> Job | None:
    """The job row, locked for this transaction and refreshed from the database."""
    return session.scalars(
        select(Job)
        .where(Job.id == job_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    ).first()


def _holds(job: Job, lease: Lease, at: datetime) -> bool:
    return (
        job.state == JobState.RUNNING
        and job.fencing_token == lease.fencing_token
        and job.lease_owner == lease.owner
        and job.lease_expires_at is not None
        and job.lease_expires_at > at
    )


def fence(session: Session, lease: Lease, at: datetime) -> Job:
    """Lock the job for this transaction if the lease is still current, else LeaseLost."""
    job = _locked(session, lease.job_id)
    if job is None or not _holds(job, lease, at):
        raise LeaseLost(f"Lease {lease.fencing_token} on job {lease.job_id} is not current")
    return job


def succeed(job: Job, lease: Lease, result: dict[str, Any] | None, at: datetime) -> None:
    """Complete a fenced job in the transaction that holds its effects."""
    if not _holds(job, lease, at):
        raise LeaseLost(f"Lease {lease.fencing_token} on job {lease.job_id} expired")
    if job.deadline is not None and job.deadline <= at:
        raise DeadlinePassed(job.id)
    job.state = JobState.SUCCEEDED.value
    job.result = result
    job.finished_at = at
    job.lease_owner = None
    job.lease_expires_at = None
    job.version += 1
    job.updated_at = at


def fail(
    session: Session,
    lease: Lease,
    *,
    error: str,
    permanent: bool,
    retry_at: datetime,
    at: datetime,
) -> JobState:
    """Record a failed attempt: queue a retry within the bound, otherwise dead-letter."""
    job = fence(session, lease, at)
    job.last_error = error[:ERROR_LIMIT]
    if permanent or job.attempts >= job.max_attempts:
        reason = (
            TerminalReason.PERMANENT_FAILURE if permanent else TerminalReason.MAX_ATTEMPTS_EXHAUSTED
        )
        _terminate(session, job, JobState.DEAD, reason, at)
        return JobState.DEAD
    job.state = JobState.QUEUED.value
    job.available_at = retry_at
    job.lease_owner = None
    job.lease_expires_at = None
    job.version += 1
    job.updated_at = at
    return JobState.QUEUED


def expire(session: Session, lease: Lease, at: datetime) -> None:
    """Terminate a fenced job whose deadline passed, visibly."""
    job = fence(session, lease, at)
    _terminate(session, job, JobState.EXPIRED, TerminalReason.DEADLINE_PASSED, at)


def redrive(
    session: Session,
    *,
    job_id: str,
    expected_version: int,
    reason: str,
    command_id: str,
    actor: str,
    at: datetime,
) -> int:
    """Return a dead letter to the queue with its identity; audited and idempotent.

    Attempts restart from zero, while the fencing token keeps increasing, so no lease from
    before the dead letter can commit. The job runs through the same claim and handler
    validation as any other attempt. Replaying a command returns its recorded version.
    """
    _aware(at)
    session.execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended(:command, 0))"),
        {"command": "job-command:" + command_id},
    )
    job = _locked(session, job_id)
    if job is None:
        raise JobNotFound(job_id)
    previous = session.scalars(select(Event).where(Event.correlation_id == command_id)).first()
    if previous is not None:
        recorded = (
            previous.event_type,
            previous.aggregate_id,
            previous.payload.get("reason"),
            previous.payload.get("actor"),
        )
        if recorded != ("JOB_REDRIVEN", job_id, reason, actor):
            raise Conflict("Command identity reused with different content")
        return previous.aggregate_version
    if job.version != expected_version:
        raise Conflict("Job changed; refresh before trying again")
    if job.state != JobState.DEAD:
        raise Conflict("Only dead-lettered jobs can be redriven")
    if job.deadline is not None and job.deadline <= at:
        raise Conflict("The job's deadline has passed; it cannot be redriven")
    attempts = job.attempts
    job.state = JobState.QUEUED.value
    job.attempts = 0
    job.redrives += 1
    job.available_at = at
    job.terminal_reason = None
    job.finished_at = None
    job.version += 1
    job.updated_at = at
    emit(
        session,
        "JOB_REDRIVEN",
        job.id,
        job.version,
        command_id,
        {
            "job_id": job.id,
            "kind": job.kind,
            "occurrence_key": job.occurrence_key,
            "reason": reason,
            "actor": actor,
            "attempts_before": attempts,
            "redrives": job.redrives,
        },
    )
    return job.version


def recent(session: Session, state: JobState | None = None) -> list[Job]:
    """Up to 100 jobs, newest first, optionally in one state."""
    query = select(Job).order_by(Job.created_at.desc(), Job.id.desc()).limit(100)
    if state is not None:
        query = query.where(Job.state == state.value)
    return list(session.scalars(query).all())


def dead_letters(session: Session) -> list[Job]:
    """Up to 100 dead letters, most recently dead-lettered first."""
    return list(
        session.scalars(
            select(Job)
            .where(Job.state == JobState.DEAD.value)
            .order_by(Job.finished_at.desc(), Job.id.desc())
            .limit(100)
        ).all()
    )


def record(job: Job) -> dict[str, Any]:
    """The JobRecord read model."""

    def instant(value: datetime | None) -> str | None:
        return value.isoformat() if value is not None else None

    return {
        "id": job.id,
        "kind": job.kind,
        "job_class": job.job_class,
        "scope": job.scope,
        "occurrence_key": job.occurrence_key,
        "correlation_id": job.correlation_id,
        "payload": job.payload,
        "input_version": job.input_version,
        "priority": job.priority,
        "state": job.state,
        "version": job.version,
        "attempts": job.attempts,
        "max_attempts": job.max_attempts,
        "redrives": job.redrives,
        "fencing_token": job.fencing_token,
        "lease_owner": job.lease_owner,
        "lease_expires_at": instant(job.lease_expires_at),
        "scheduled_at": instant(job.scheduled_at),
        "available_at": instant(job.available_at),
        "deadline": instant(job.deadline),
        "terminal_reason": job.terminal_reason,
        "last_error": job.last_error,
        "result": job.result,
        "created_at": instant(job.created_at),
        "updated_at": instant(job.updated_at),
        "finished_at": instant(job.finished_at),
    }
