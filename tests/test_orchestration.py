"""Database-free rules of the durable job runner: capacity, backoff, registry, keys."""

from datetime import UTC, datetime, timedelta, timezone

import pytest
from aura.orchestration.jobs import SAFETY_CLASSES, Capacity, WorkClass, occurrence_key
from aura.orchestration.registry import JobSpec, Registry, UnknownJobKind
from aura.orchestration.runner import Backoff


def nothing(context: object) -> None:
    return None


def test_safety_classes_are_reconciliation_and_monitoring():
    assert SAFETY_CLASSES == {WorkClass.RECONCILIATION, WorkClass.MONITORING}


@pytest.mark.parametrize(
    "running,running_non_safety,claimable",
    [
        (0, 0, set(WorkClass)),
        (1, 1, set(WorkClass)),
        (2, 2, SAFETY_CLASSES),  # other work already holds every unreserved slot
        (2, 0, set(WorkClass)),  # safety work may also use unreserved slots
        (2, 1, set(WorkClass)),
        (3, 2, set()),  # all slots leased
        (3, 0, set()),
    ],
)
def test_capacity_keeps_reserved_slots_for_safety_work(running, running_non_safety, claimable):
    capacity = Capacity(slots=3, safety_reserved=1)  # FIXTURE capacity, not a policy value
    assert capacity.claimable(running, running_non_safety) == claimable


@pytest.mark.parametrize("slots,reserved", [(0, 0), (2, 0), (2, 3), (1, -1)])
def test_capacity_requires_a_safety_reservation(slots, reserved):
    with pytest.raises(ValueError):
        Capacity(slots=slots, safety_reserved=reserved)


def test_backoff_is_bounded_deterministic_and_jittered():
    backoff = Backoff(base=timedelta(seconds=2), cap=timedelta(seconds=10))
    for attempt, ceiling in [(1, 2), (2, 4), (3, 8), (4, 10), (60, 10)]:
        delay = backoff.delay("job-a", attempt)
        assert timedelta(seconds=ceiling) / 2 <= delay <= timedelta(seconds=ceiling)
        assert delay == backoff.delay("job-a", attempt)
    assert len({backoff.delay(f"job-{n}", 3) for n in range(10)}) > 1
    with pytest.raises(ValueError):
        backoff.delay("job-a", 0)
    with pytest.raises(ValueError):
        Backoff(base=timedelta(0), cap=timedelta(seconds=1))
    with pytest.raises(ValueError):
        Backoff(base=timedelta(seconds=2), cap=timedelta(seconds=1))


def test_registry_rejects_duplicates_bad_names_and_unknown_kinds():
    spec = JobSpec("fixture.check", WorkClass.RECONCILIATION, nothing, "Fixture check")
    registry = Registry([spec])
    assert registry.kinds() == ("fixture.check",)
    assert "fixture.check" in registry
    assert registry.spec("fixture.check") is spec
    with pytest.raises(UnknownJobKind):
        registry.spec("fixture.missing")
    with pytest.raises(ValueError):
        Registry([spec, spec])
    for kind in ["", "Fixture", "fixture..check", "fixture check", "x" * 101]:
        with pytest.raises(ValueError):
            JobSpec(kind, WorkClass.RESEARCH, nothing, "Bad kind")
    with pytest.raises(ValueError):
        JobSpec("fixture.check", WorkClass.RESEARCH, nothing, " ")


def test_occurrence_key_is_canonical_utc():
    at = datetime(2026, 9, 23, 20, 30, tzinfo=UTC)
    eastern = at.astimezone(timezone(timedelta(hours=-4)))
    key = occurrence_key("fixture-schedule-v1", "portfolio:fixture", at)
    assert key == occurrence_key("fixture-schedule-v1", "portfolio:fixture", eastern)
    assert key != occurrence_key("fixture-schedule-v2", "portfolio:fixture", at)
    assert occurrence_key("a:b", "c", at) != occurrence_key("a", "b:c", at)
    with pytest.raises(ValueError):
        occurrence_key("fixture-schedule-v1", "portfolio:fixture", at.replace(tzinfo=None))
    with pytest.raises(ValueError):
        occurrence_key("", "portfolio:fixture", at)
