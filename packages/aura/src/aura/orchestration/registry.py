"""Job kinds the worker can run, their work classes and handlers. No schedules.

OD-03 leaves every cadence, session policy and deadline open, so nothing here creates
occurrences: the worker runs only jobs that code enqueued explicitly through a registry.
The production REGISTRY is empty until a later slice registers a kind.

A handler runs inside the fenced transaction that also marks its job SUCCEEDED, so its
local writes commit exactly once or not at all. Handlers must not call external services;
an external effect needs its own persisted intent, idempotency key and reconciliation
(chapter 08), which no registered kind implements yet.
"""

import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy.orm import Session

from aura.orchestration.jobs import Clock, Job, Lease, WorkClass, create


class PermanentJobError(Exception):
    """Input that can never succeed. The job dead-letters at once instead of retrying."""


class UnknownJobKind(LookupError):
    """The kind is not registered, so no handler or work class exists for it."""


@dataclass(frozen=True)
class JobContext:
    """What a handler sees: its fenced transaction, the job's input and its lease."""

    session: Session
    job_id: str
    kind: str
    scope: str
    payload: dict[str, Any]
    input_version: int | None
    lease: Lease
    clock: Clock


Handler = Callable[[JobContext], dict[str, Any] | None]
KIND = re.compile(r"[a-z][a-z0-9]*(?:[._-][a-z0-9]+)*")


@dataclass(frozen=True)
class JobSpec:
    kind: str
    work_class: WorkClass
    handler: Handler
    description: str

    def __post_init__(self) -> None:
        if len(self.kind) > 100 or not KIND.fullmatch(self.kind):
            raise ValueError("Job kinds are short lowercase dotted names")
        if not self.description.strip():
            raise ValueError("Describe what the job does")


class Registry:
    """Maps each kind to one spec. Enqueuing derives the class from the kind."""

    def __init__(self, specs: Iterable[JobSpec] = ()) -> None:
        self._specs: dict[str, JobSpec] = {}
        for spec in specs:
            if spec.kind in self._specs:
                raise ValueError(f"Job kind {spec.kind} is registered twice")
            self._specs[spec.kind] = spec

    def __contains__(self, kind: object) -> bool:
        return kind in self._specs

    def kinds(self) -> tuple[str, ...]:
        return tuple(sorted(self._specs))

    def spec(self, kind: str) -> JobSpec:
        try:
            return self._specs[kind]
        except KeyError:
            raise UnknownJobKind(kind) from None

    def enqueue(
        self,
        session: Session,
        *,
        kind: str,
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
        """Create the job once per occurrence key. Deadline and retry bound are explicit."""
        return create(
            session,
            kind=kind,
            work_class=self.spec(kind).work_class,
            scope=scope,
            occurrence_key=occurrence_key,
            scheduled_at=scheduled_at,
            deadline=deadline,
            priority=priority,
            max_attempts=max_attempts,
            payload=payload,
            correlation_id=correlation_id,
            at=at,
            input_version=input_version,
        )


REGISTRY = Registry()
