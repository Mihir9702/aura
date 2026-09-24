"""Durable worker: outbox acknowledgement plus the durable job runner. No broker/model work.

No high-watermark cursor is used: commit order may differ from sequence order.
Inbox writes and consumption commit together, and locked events prevent duplicate
local acknowledgement across competing workers.

Each tick then claims and runs jobs that code enqueued explicitly (aura.orchestration).
The worker creates no jobs itself: no recurring schedule is enabled (OD-03). Entry Halt
and Full Kill gate order submission, not jobs, so safety work keeps running under both.
"""

import os
import socket
import time
from uuid import uuid4

from sqlalchemy import exists, select
from sqlalchemy.orm import Session

from aura.config import Settings
from aura.events import Event, Inbox
from aura.orchestration.registry import REGISTRY
from aura.orchestration.runner import (
    BACKOFF_DEV,
    CAPACITY_DEV,
    JOBS_PER_TICK_DEV,
    LEASE_DEV,
    JobRunner,
    Outcome,
)
from aura.storage import Database

CONSUMER = "foundation-audit-v1"


def acknowledge_batch(session: Session) -> int:
    processed = exists(
        select(Inbox.id).where(Inbox.event_id == Event.event_id, Inbox.consumer == CONSUMER)
    )
    events = session.scalars(
        select(Event)
        .where(~processed)
        .order_by(Event.sequence)
        .limit(100)
        .with_for_update(skip_locked=True)
    ).all()
    for event in events:
        session.add(Inbox(consumer=CONSUMER, event_id=event.event_id))
    return len(events)


def identity() -> str:
    """A lease owner unique to this process, readable in job records."""
    return f"worker:{socket.gethostname()}:{os.getpid()}:{uuid4().hex[:8]}"


def tick(db: Database, runner: JobRunner, *, jobs_per_tick: int) -> tuple[int, list[Outcome]]:
    """Acknowledge one inbox batch, then run up to `jobs_per_tick` claimable jobs."""
    with db.transaction() as session:
        acknowledged = acknowledge_batch(session)
    return acknowledged, runner.run_available(jobs_per_tick)


def main() -> None:
    db = Database(Settings().database_url.get_secret_value())  # type: ignore[call-arg]
    runner = JobRunner(
        db,
        REGISTRY,
        worker=identity(),
        capacity=CAPACITY_DEV,
        lease=LEASE_DEV,
        backoff=BACKOFF_DEV,
    )
    try:
        while True:
            tick(db, runner, jobs_per_tick=JOBS_PER_TICK_DEV)
            time.sleep(2)
    finally:
        db.engine.dispose()


if __name__ == "__main__":
    main()
