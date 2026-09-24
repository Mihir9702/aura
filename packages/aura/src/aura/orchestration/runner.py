"""Claims and runs durable jobs: at-least-once attempts, exactly-once local effects.

Each attempt uses two transactions. The claim commits first, so a crash still counts the
attempt and a job that keeps killing its worker dead-letters after its bound. The handler
then runs in a second transaction that locks the job under the lease's fencing token and
commits its writes together with SUCCEEDED. If the worker dies, or its lease expires or
is superseded first, nothing from that attempt commits and a later claim retries it.

The runner does not consult Entry Halt or Full Kill. Capability controls gate order
submission at admission; reconciliation, Ledger and monitoring jobs keep running under
both controls (ADR-0020), and no registered kind submits orders.
"""

import hashlib
import logging
from copy import deepcopy
from dataclasses import dataclass
from datetime import timedelta
from enum import StrEnum

from aura.common import now
from aura.orchestration import jobs
from aura.orchestration.jobs import Capacity, Clock, JobState, Lease
from aura.orchestration.registry import JobContext, PermanentJobError, Registry
from aura.storage import Database

LOG = logging.getLogger(__name__)


class Outcome(StrEnum):
    SUCCEEDED = "SUCCEEDED"
    RETRY_SCHEDULED = "RETRY_SCHEDULED"
    DEAD_LETTERED = "DEAD_LETTERED"
    EXPIRED = "EXPIRED"
    LEASE_LOST = "LEASE_LOST"


@dataclass(frozen=True)
class Backoff:
    """Exponential retry delay capped at `cap`, with deterministic jitter.

    The delay falls between half and all of min(cap, base * 2^(attempt-1)), spread by a
    hash of the job and attempt, so retries de-synchronize and tests stay reproducible.
    """

    base: timedelta
    cap: timedelta

    def __post_init__(self) -> None:
        if self.base <= timedelta(0) or self.cap < self.base:
            raise ValueError("Backoff needs a positive base and a cap at least that long")

    def delay(self, job_id: str, attempt: int) -> timedelta:
        if attempt < 1:
            raise ValueError("Attempts start at one")
        ceiling = min(self.cap, self.base * (1 << min(attempt - 1, 30)))
        digest = hashlib.sha256(f"{job_id}:{attempt}".encode()).digest()
        spread = int.from_bytes(digest[:8], "big") / 2**64
        return ceiling / 2 + ceiling / 2 * spread


# Development values only, not approved policy. Lease length, retry backoff, capacity and
# the per-tick batch are open operational parameters (OD-03 timing, OD-12 recovery targets,
# OD-14 budgets). The worker uses these labeled values until the owner approves real ones.
LEASE_DEV = timedelta(seconds=60)
BACKOFF_DEV = Backoff(base=timedelta(seconds=5), cap=timedelta(minutes=5))
CAPACITY_DEV = Capacity(slots=4, safety_reserved=1)
JOBS_PER_TICK_DEV = 10


def describe(error: BaseException) -> str:
    return f"{type(error).__name__}: {error}"[: jobs.ERROR_LIMIT]


class JobRunner:
    """One worker identity claiming registered kinds under explicit operating parameters."""

    def __init__(
        self,
        db: Database,
        registry: Registry,
        *,
        worker: str,
        capacity: Capacity,
        lease: timedelta,
        backoff: Backoff,
        clock: Clock = now,
    ) -> None:
        self.db = db
        self.registry = registry
        self.worker = worker
        self.capacity = capacity
        self.lease = lease
        self.backoff = backoff
        self.clock = clock

    def claim(self) -> Lease | None:
        with self.db.transaction() as session:
            return jobs.claim(
                session,
                kinds=self.registry.kinds(),
                worker=self.worker,
                capacity=self.capacity,
                lease=self.lease,
                at=self.clock(),
            )

    def run(self, lease: Lease) -> Outcome:
        """Run one claimed attempt and settle it. Only a current lease can commit."""
        spec = self.registry.spec(lease.kind)
        try:
            with self.db.transaction() as session:
                job = jobs.fence(session, lease, self.clock())
                if job.deadline is not None and job.deadline <= self.clock():
                    raise jobs.DeadlinePassed(job.id)
                result = spec.handler(
                    JobContext(
                        session=session,
                        job_id=job.id,
                        kind=job.kind,
                        scope=job.scope,
                        payload=deepcopy(job.payload),
                        input_version=job.input_version,
                        lease=lease,
                        clock=self.clock,
                    )
                )
                session.flush()
                jobs.succeed(job, lease, result, self.clock())
            return Outcome.SUCCEEDED
        except jobs.LeaseLost:
            LOG.warning(
                "Job %s lost lease %s; nothing committed", lease.job_id, lease.fencing_token
            )
            return Outcome.LEASE_LOST
        except jobs.DeadlinePassed:
            try:
                with self.db.transaction() as session:
                    jobs.expire(session, lease, self.clock())
            except jobs.LeaseLost:
                return Outcome.LEASE_LOST
            return Outcome.EXPIRED
        except PermanentJobError as error:
            return self._failed(lease, error, permanent=True)
        except Exception as error:  # retried within the job's explicit attempt bound
            return self._failed(lease, error, permanent=False)

    def _failed(self, lease: Lease, error: Exception, *, permanent: bool) -> Outcome:
        at = self.clock()
        try:
            with self.db.transaction() as session:
                state = jobs.fail(
                    session,
                    lease,
                    error=describe(error),
                    permanent=permanent,
                    retry_at=at + self.backoff.delay(lease.job_id, lease.attempt),
                    at=at,
                )
        except jobs.LeaseLost:
            return Outcome.LEASE_LOST
        outcome = Outcome.DEAD_LETTERED if state == JobState.DEAD else Outcome.RETRY_SCHEDULED
        LOG.warning(
            "Job %s (%s) attempt %s failed with %s: %s",
            lease.job_id,
            lease.kind,
            lease.attempt,
            type(error).__name__,
            outcome.value,
        )
        return outcome

    def run_available(self, limit: int) -> list[Outcome]:
        """Claim and run jobs one at a time until none is claimable or `limit` ran."""
        outcomes: list[Outcome] = []
        while len(outcomes) < limit:
            lease = self.claim()
            if lease is None:
                break
            outcomes.append(self.run(lease))
        return outcomes
