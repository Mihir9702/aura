"""Durable jobs on real PostgreSQL: crash recovery, fencing, dead letters, capacity (AC-19).

Handlers write their local effect as an outbox row (FIXTURE_JOB_EFFECT) in the fenced
transaction, so counting those rows shows whether an effect committed once, twice or never.
Operating parameters are FIXTURE values passed explicitly, not approved policy.
"""

import os
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import datetime, timedelta
from typing import Any
from uuid import uuid4

import pytest
from aura.api import create_app
from aura.common import Conflict, now
from aura.config import Settings
from aura.events import Event, Inbox, emit
from aura.operations import change_control, locked_gate
from aura.orchestration import jobs
from aura.orchestration.jobs import Capacity, Job, JobState, LeaseLost, WorkClass, occurrence_key
from aura.orchestration.registry import (
    REGISTRY,
    JobContext,
    JobSpec,
    PermanentJobError,
    Registry,
    UnknownJobKind,
)
from aura.orchestration.runner import (
    BACKOFF_DEV,
    CAPACITY_DEV,
    JOBS_PER_TICK_DEV,
    LEASE_DEV,
    Backoff,
    JobRunner,
    Outcome,
)
from aura.worker import identity, tick
from fastapi.testclient import TestClient
from sqlalchemy import func, select

pytestmark = pytest.mark.integration

FIXTURE_CAPACITY = Capacity(slots=3, safety_reserved=1)
FIXTURE_LEASE = timedelta(seconds=30)
FIXTURE_BACKOFF = Backoff(base=timedelta(seconds=1), cap=timedelta(seconds=4))
EFFECT = "FIXTURE_JOB_EFFECT"
COMMAND = {"X-Aura-Command": "1"}


class ManualClock:
    """The injected clock: starts at the real time and moves only when a test says so."""

    def __init__(self, start: datetime | None = None) -> None:
        self.at = start or now()

    def __call__(self) -> datetime:
        return self.at

    def advance(self, seconds: float) -> None:
        self.at += timedelta(seconds=seconds)


class Crash(BaseException):
    """Stands in for process death: not an Exception, so the runner records nothing."""


def effect(context: JobContext) -> dict[str, Any]:
    emit(
        context.session,
        EFFECT,
        context.job_id,
        context.lease.fencing_token,
        context.job_id,
        {"attempt": context.lease.attempt},
    )
    return {"attempt": context.lease.attempt}


SAFE = JobSpec("fixture.reconcile", WorkClass.RECONCILIATION, effect, "Fixture safety effect")
RESEARCH = JobSpec("fixture.research", WorkClass.RESEARCH, effect, "Fixture research effect")


def variant(spec: JobSpec, handler: Callable[[JobContext], Any]) -> Registry:
    """The same kind and class with another handler, as a different deploy would run it."""
    return Registry([JobSpec(spec.kind, spec.work_class, handler, spec.description)])


def runner(db, registry, clock, worker="worker-a", capacity=FIXTURE_CAPACITY) -> JobRunner:
    return JobRunner(
        db,
        registry,
        worker=worker,
        capacity=capacity,
        lease=FIXTURE_LEASE,
        backoff=FIXTURE_BACKOFF,
        clock=clock,
    )


def enqueue(db, registry, clock, kind, key, *, max_attempts=3, deadline=None, priority=0):
    with db.transaction() as s:
        job = registry.enqueue(
            s,
            kind=kind,
            scope="fixture",
            occurrence_key=key,
            scheduled_at=clock(),
            deadline=deadline,
            priority=priority,
            max_attempts=max_attempts,
            payload={"key": key},
            correlation_id="fixture-" + key,
            at=clock(),
        )
        return job.id


def load(db, job_id) -> Job:
    with db.transaction() as s:
        job = s.get(Job, job_id)
        assert job is not None
        return job


def effects(db, job_id) -> int:
    with db.transaction() as s:
        return s.scalar(
            select(func.count())
            .select_from(Event)
            .where(Event.event_type == EFFECT, Event.aggregate_id == job_id)
        )


def events(db, event_type, job_id) -> list[Event]:
    with db.transaction() as s:
        return list(
            s.scalars(
                select(Event).where(Event.event_type == event_type, Event.aggregate_id == job_id)
            )
        )


@contextmanager
def owner_client() -> Iterator[TestClient]:
    settings = Settings(database_url=os.environ["AURA_TEST_DATABASE_URL"], owner_key="x" * 40)
    with TestClient(create_app(settings)) as client:
        response = client.post("/api/session", json={"key": "x" * 40}, headers=COMMAND)
        assert response.status_code == 200
        yield client


def test_occurrence_key_creates_each_job_once(db):
    clock = ManualClock()
    registry = Registry([SAFE])
    key = occurrence_key("fixture-schedule-v1", "fixture", clock())
    first = enqueue(db, registry, clock, SAFE.kind, key)
    assert enqueue(db, registry, clock, SAFE.kind, key) == first
    with ThreadPoolExecutor(max_workers=4) as pool:
        racing = set(pool.map(lambda _: enqueue(db, registry, clock, SAFE.kind, "race"), range(4)))
    assert len(racing) == 1
    with pytest.raises(Conflict), db.transaction() as s:
        registry.enqueue(
            s,
            kind=SAFE.kind,
            scope="fixture",
            occurrence_key=key,
            scheduled_at=clock(),
            deadline=None,
            priority=0,
            max_attempts=3,
            payload={"key": "different content"},
            correlation_id="fixture-reuse",
            at=clock(),
        )
    with pytest.raises(UnknownJobKind):
        enqueue(db, registry, clock, RESEARCH.kind, "unregistered")
    with db.transaction() as s:
        assert s.scalar(select(func.count()).select_from(Job)) == 2


def test_crash_between_claim_and_commit_is_retried_without_duplicate_effect(db):
    clock = ManualClock()

    def crash(context: JobContext) -> None:
        effect(context)
        context.session.flush()  # the effect reaches PostgreSQL inside the open transaction
        raise Crash

    doomed = runner(db, variant(SAFE, crash), clock, "doomed")
    healthy = runner(db, Registry([SAFE]), clock, "healthy")
    job_id = enqueue(db, healthy.registry, clock, SAFE.kind, "crash")
    lease = doomed.claim()
    assert lease is not None and (lease.fencing_token, lease.attempt) == (1, 1)
    with pytest.raises(Crash):
        doomed.run(lease)
    assert effects(db, job_id) == 0
    assert load(db, job_id).state == JobState.RUNNING  # the claim and its attempt are durable
    assert healthy.claim() is None  # still leased to the crashed worker

    clock.advance(FIXTURE_LEASE.total_seconds() + 1)
    retry = healthy.claim()
    assert retry is not None and (retry.job_id, retry.fencing_token, retry.attempt) == (
        job_id,
        2,
        2,
    )
    assert healthy.run(retry) == Outcome.SUCCEEDED
    assert doomed.run(lease) == Outcome.LEASE_LOST  # a late wake-up changes nothing
    assert effects(db, job_id) == 1
    done = load(db, job_id)
    assert (done.state, done.attempts, done.result) == (JobState.SUCCEEDED, 2, {"attempt": 2})


KILLED_WORKER = """
import os
from datetime import timedelta
from aura.events import emit
from aura.orchestration.jobs import Capacity, WorkClass
from aura.orchestration.registry import JobSpec, Registry
from aura.orchestration.runner import Backoff, JobRunner
from aura.storage import Database

def die(context):
    emit(context.session, "FIXTURE_JOB_EFFECT", context.job_id, 1, context.job_id, {})
    context.session.flush()
    os._exit(17)  # the process dies holding the job lock and an uncommitted effect

db = Database(os.environ["AURA_TEST_DATABASE_URL"])
spec = JobSpec("fixture.reconcile", WorkClass.RECONCILIATION, die, "Dies mid-attempt")
JobRunner(db, Registry([spec]), worker="killed", capacity=Capacity(slots=3, safety_reserved=1),
          lease=timedelta(seconds=30),
          backoff=Backoff(timedelta(seconds=1), timedelta(seconds=4))).run_available(1)
"""


def test_killed_worker_process_is_recovered_without_duplicate_effect(db):
    clock = ManualClock()
    registry = Registry([SAFE])
    job_id = enqueue(db, registry, clock, SAFE.kind, "killed")
    killed = subprocess.run(
        [sys.executable, "-c", KILLED_WORKER],
        env=os.environ.copy(),
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert killed.returncode == 17, killed.stderr
    stranded = load(db, job_id)
    assert (stranded.state, stranded.lease_owner, stranded.attempts) == (
        JobState.RUNNING,
        "killed",
        1,
    )
    assert effects(db, job_id) == 0

    assert stranded.lease_expires_at is not None
    clock.at = stranded.lease_expires_at + timedelta(seconds=1)
    healthy = runner(db, registry, clock, "healthy")
    patience = time.monotonic() + 15
    lease = healthy.claim()
    while lease is None and time.monotonic() < patience:  # the server may still hold the lock
        time.sleep(0.2)
        lease = healthy.claim()
    assert lease is not None and (lease.fencing_token, lease.attempt) == (2, 2)
    assert healthy.run(lease) == Outcome.SUCCEEDED
    assert effects(db, job_id) == 1


def test_stale_fencing_token_cannot_commit(db):
    clock = ManualClock()
    registry = Registry([SAFE])
    job_id = enqueue(db, registry, clock, SAFE.kind, "stale")
    worker = runner(db, registry, clock, "worker-a")
    stale = worker.claim()
    assert stale is not None
    clock.advance(FIXTURE_LEASE.total_seconds() + 1)
    current = worker.claim()  # the same identity, as a restarted process would reclaim it
    assert current is not None and current.fencing_token == stale.fencing_token + 1
    assert worker.run(stale) == Outcome.LEASE_LOST
    with pytest.raises(LeaseLost), db.transaction() as s:
        jobs.fence(s, stale, clock())
    assert effects(db, job_id) == 0
    assert worker.run(current) == Outcome.SUCCEEDED
    assert effects(db, job_id) == 1


def test_expired_lease_cannot_commit(db):
    clock = ManualClock()

    def overrun(context: JobContext) -> None:
        effect(context)
        clock.advance(FIXTURE_LEASE.total_seconds() + 1)  # the handler outlives its lease

    slow = runner(db, variant(SAFE, overrun), clock, "slow")
    job_id = enqueue(db, slow.registry, clock, SAFE.kind, "expired")
    lease = slow.claim()
    assert lease is not None
    assert slow.run(lease) == Outcome.LEASE_LOST
    assert effects(db, job_id) == 0
    stuck = load(db, job_id)
    assert (stuck.state, stuck.lease_owner, stuck.attempts) == (JobState.RUNNING, "slow", 1)

    # No rival claimed it, yet a lease that has run out still cannot commit.
    idle = runner(db, Registry([SAFE]), clock, "idle")
    late = idle.claim()
    assert late is not None and late.attempt == 2
    clock.advance(FIXTURE_LEASE.total_seconds() + 1)
    assert idle.run(late) == Outcome.LEASE_LOST
    assert effects(db, job_id) == 0
    final = idle.claim()
    assert final is not None and final.attempt == 3
    assert idle.run(final) == Outcome.SUCCEEDED
    assert effects(db, job_id) == 1


def test_claims_skip_a_stalled_attempt_that_still_holds_its_job(db):
    clock = ManualClock()
    inside, release = threading.Event(), threading.Event()

    def stall(context: JobContext) -> None:
        effect(context)
        inside.set()
        assert release.wait(10)

    stalled = runner(db, variant(SAFE, stall), clock, "stalled")
    rival = runner(db, Registry([SAFE]), clock, "rival")
    job_id = enqueue(db, stalled.registry, clock, SAFE.kind, "stalled")
    lease = stalled.claim()
    assert lease is not None
    with ThreadPoolExecutor(max_workers=1) as pool:
        attempt = pool.submit(stalled.run, lease)
        assert inside.wait(10)
        clock.advance(FIXTURE_LEASE.total_seconds() + 1)  # the lease lapses mid-attempt
        started = time.monotonic()
        assert rival.claim() is None  # SKIP LOCKED passes over the locked row instead of waiting
        assert time.monotonic() - started < 5
        release.set()
        assert attempt.result(timeout=10) == Outcome.LEASE_LOST
    assert effects(db, job_id) == 0
    retry = rival.claim()
    assert retry is not None and (retry.fencing_token, retry.attempt) == (2, 2)
    assert rival.run(retry) == Outcome.SUCCEEDED
    assert effects(db, job_id) == 1


def test_poison_job_becomes_a_visible_dead_letter_and_redrive_keeps_identity(db):
    clock = ManualClock()

    def poison(context: JobContext) -> None:
        raise RuntimeError("fixture input cannot be processed")

    worker = runner(db, variant(SAFE, poison), clock)
    job_id = enqueue(db, worker.registry, clock, SAFE.kind, "poison", max_attempts=3)
    outcomes = []
    while (lease := worker.claim()) is not None:
        outcomes.append(worker.run(lease))
        clock.advance(FIXTURE_BACKOFF.cap.total_seconds())  # past any retry delay
    assert outcomes == [Outcome.RETRY_SCHEDULED, Outcome.RETRY_SCHEDULED, Outcome.DEAD_LETTERED]
    dead = load(db, job_id)
    assert (dead.state, dead.attempts, dead.terminal_reason) == (
        JobState.DEAD,
        3,
        "MAX_ATTEMPTS_EXHAUSTED",
    )
    assert dead.last_error == "RuntimeError: fixture input cannot be processed"
    (dead_event,) = events(db, "JOB_DEAD_LETTERED", job_id)
    assert dead_event.payload["reason"] == "MAX_ATTEMPTS_EXHAUSTED"

    command = {"command_id": str(uuid4()), "expected_version": dead.version, "reason": "fixed"}
    with owner_client() as client:
        letters = client.get("/api/jobs/dead-letters").json()
        assert [(r["id"], r["state"], r["attempts"], r["last_error"]) for r in letters] == [
            (job_id, "DEAD", 3, dead.last_error)
        ]
        redriven = client.post(f"/api/jobs/{job_id}/redrive", json=command, headers=COMMAND)
        assert redriven.status_code == 200
        assert redriven.json() == {"job_id": job_id, "version": dead.version + 1}
        replay = client.post(f"/api/jobs/{job_id}/redrive", json=command, headers=COMMAND)
        assert (replay.status_code, replay.json()) == (200, redriven.json())
        assert client.get("/api/jobs/dead-letters").json() == []

    def identity_of(job: Job) -> tuple[Any, ...]:
        return (job.id, job.occurrence_key, job.kind, job.job_class, job.scope, job.payload,
                job.scheduled_at, job.correlation_id, job.max_attempts)  # fmt: skip

    queued = load(db, job_id)
    assert identity_of(queued) == identity_of(dead)
    assert (queued.state, queued.attempts, queued.redrives, queued.fencing_token) == (
        JobState.QUEUED,
        0,
        1,
        dead.fencing_token,
    )
    (audit,) = events(db, "JOB_REDRIVEN", job_id)  # the replay added no second transition
    assert audit.correlation_id == command["command_id"]
    assert (audit.payload["actor"], audit.payload["attempts_before"]) == ("owner", 3)

    clock.advance(60)
    fixed = runner(db, Registry([SAFE]), clock, "fixed")
    lease = fixed.claim()
    assert lease is not None
    assert (lease.job_id, lease.fencing_token, lease.attempt) == (job_id, dead.fencing_token + 1, 1)
    assert fixed.run(lease) == Outcome.SUCCEEDED
    assert effects(db, job_id) == 1


def test_permanent_failure_dead_letters_without_retrying(db):
    clock = ManualClock()

    def invalid(context: JobContext) -> None:
        raise PermanentJobError("fixture payload fails validation")

    worker = runner(db, variant(SAFE, invalid), clock)
    job_id = enqueue(db, worker.registry, clock, SAFE.kind, "invalid", max_attempts=3)
    lease = worker.claim()
    assert lease is not None
    assert worker.run(lease) == Outcome.DEAD_LETTERED
    dead = load(db, job_id)
    assert (dead.state, dead.attempts, dead.terminal_reason) == (
        JobState.DEAD,
        1,
        "PERMANENT_FAILURE",
    )
    assert worker.claim() is None


def test_job_that_kills_its_worker_dead_letters_after_its_bound(db):
    clock = ManualClock()

    def crash(context: JobContext) -> None:
        raise Crash

    worker = runner(db, variant(SAFE, crash), clock)
    job_id = enqueue(db, worker.registry, clock, SAFE.kind, "killer", max_attempts=2)
    for _ in range(2):
        lease = worker.claim()
        assert lease is not None
        with pytest.raises(Crash):
            worker.run(lease)
        clock.advance(FIXTURE_LEASE.total_seconds() + 1)
    assert worker.claim() is None  # the claim sweep dead-letters the crashed final attempt
    dead = load(db, job_id)
    assert (dead.state, dead.attempts, dead.terminal_reason) == (
        JobState.DEAD,
        2,
        "MAX_ATTEMPTS_EXHAUSTED",
    )
    assert dead.last_error == "Lease expired before the final attempt completed"
    assert len(events(db, "JOB_DEAD_LETTERED", job_id)) == 1


def test_research_backlog_cannot_starve_safety_jobs(db):
    clock = ManualClock()
    registry = Registry([SAFE, RESEARCH])
    for n in range(6):
        enqueue(db, registry, clock, RESEARCH.kind, f"research-{n}", priority=10)
    clock.advance(1)
    a, b, c, d = (runner(db, registry, clock, f"worker-{name}") for name in "abcd")
    research = [a.claim(), b.claim()]
    assert all(lease is not None and lease.kind == RESEARCH.kind for lease in research)
    assert c.claim() is None  # the backlog cannot take the slot reserved for safety work
    urgent_id = enqueue(db, registry, clock, SAFE.kind, "safety-1")
    urgent = c.claim()
    assert urgent is not None and urgent.job_id == urgent_id
    assert d.claim() is None  # every slot is leased
    assert c.run(urgent) == Outcome.SUCCEEDED
    for worker, lease in zip((a, b), research, strict=True):
        assert lease is not None and worker.run(lease) == Outcome.SUCCEEDED

    # With free slots, safety work still goes first: ahead of an older, higher-priority backlog.
    later_id = enqueue(db, registry, clock, SAFE.kind, "safety-2", priority=-10)
    later = d.claim()
    assert later is not None and later.job_id == later_id


def test_concurrent_claims_leave_the_reserved_slot_free(db):
    clock = ManualClock()
    registry = Registry([RESEARCH])
    for n in range(8):
        enqueue(db, registry, clock, RESEARCH.kind, f"research-{n}")
    capacity = Capacity(slots=4, safety_reserved=1)
    workers = [runner(db, registry, clock, f"worker-{n}", capacity) for n in range(6)]
    start = threading.Barrier(len(workers))

    def claim(n: int):
        start.wait(10)  # all claims contend at once
        return workers[n].claim()

    with ThreadPoolExecutor(max_workers=len(workers)) as pool:
        leases = [lease for lease in pool.map(claim, range(6)) if lease is not None]
    assert len(leases) == 3
    assert len({lease.job_id for lease in leases}) == 3
    with db.transaction() as s:
        running = select(func.count()).select_from(Job).where(Job.state == JobState.RUNNING)
        assert s.scalar(running) == 3


@pytest.mark.parametrize("control", ["full_kill", "entry_halt"])
def test_capability_controls_do_not_stop_safety_jobs(db, control):
    clock = ManualClock()

    def reconcile(context: JobContext) -> dict[str, Any]:
        gate = locked_gate(context.session)  # reads the persisted controls, as reconciliation would
        effect(context)
        return {"entry_halt": gate.entry_halt, "full_kill": gate.full_kill}

    worker = runner(db, variant(SAFE, reconcile), clock)
    with db.transaction() as s:
        change_control(s, control, True, 1, "incident drill", str(uuid4()))
    job_id = enqueue(db, worker.registry, clock, SAFE.kind, "during-" + control)
    acknowledged, outcomes = tick(db, worker, jobs_per_tick=5)
    assert outcomes == [Outcome.SUCCEEDED]
    assert acknowledged == 3  # two funding events and the control change
    assert load(db, job_id).result == {
        "entry_halt": control == "entry_halt",
        "full_kill": control == "full_kill",
    }
    assert effects(db, job_id) == 1


def test_deadlines_expire_jobs_visibly(db):
    clock = ManualClock()
    registry = Registry([RESEARCH])
    overdue = enqueue(
        db, registry, clock, RESEARCH.kind, "overdue", deadline=clock() + timedelta(seconds=10)
    )
    clock.advance(11)
    assert runner(db, registry, clock).claim() is None
    expired = load(db, overdue)
    assert (expired.state, expired.terminal_reason, expired.attempts) == (
        JobState.EXPIRED,
        "DEADLINE_PASSED",
        0,
    )
    assert len(events(db, "JOB_EXPIRED", overdue)) == 1

    def overrun(context: JobContext) -> None:
        effect(context)
        clock.advance(10)  # past the deadline while the lease is still valid

    slow = runner(db, variant(RESEARCH, overrun), clock)
    late = enqueue(
        db, slow.registry, clock, RESEARCH.kind, "late", deadline=clock() + timedelta(seconds=5)
    )
    lease = slow.claim()
    assert lease is not None
    assert slow.run(lease) == Outcome.EXPIRED
    assert effects(db, late) == 0
    assert load(db, late).state == JobState.EXPIRED
    assert len(events(db, "JOB_EXPIRED", late)) == 1


def test_worker_tick_acknowledges_events_runs_jobs_and_schedules_nothing(db):
    production = JobRunner(
        db,
        REGISTRY,
        worker=identity(),
        capacity=CAPACITY_DEV,
        lease=LEASE_DEV,
        backoff=BACKOFF_DEV,
    )
    assert tick(db, production, jobs_per_tick=JOBS_PER_TICK_DEV) == (2, [])  # funding events

    clock = ManualClock()
    worker = runner(db, Registry([SAFE]), clock)
    for _ in range(3):
        clock.advance(86400)  # days pass; no recurring schedule creates occurrences (OD-03)
        assert tick(db, worker, jobs_per_tick=5) == (0, [])
    with db.transaction() as s:
        assert s.scalar(select(func.count()).select_from(Job)) == 0

    other = enqueue(db, Registry([RESEARCH]), clock, RESEARCH.kind, "other-kind")
    job_id = enqueue(db, worker.registry, clock, SAFE.kind, "explicit")
    assert tick(db, worker, jobs_per_tick=5) == (0, [Outcome.SUCCEEDED])
    assert load(db, other).state == JobState.QUEUED  # kinds this worker cannot run are left alone
    assert tick(db, worker, jobs_per_tick=5) == (1, [])  # the committed effect is acknowledged
    with db.transaction() as s:
        effect_event = s.scalars(select(Event).where(Event.aggregate_id == job_id)).one()
        acknowledged = select(func.count()).select_from(Inbox)
        assert s.scalar(acknowledged.where(Inbox.event_id == effect_event.event_id)) == 1
