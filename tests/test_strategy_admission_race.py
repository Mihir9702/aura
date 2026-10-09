"""AC-24: entry admission racing strategy suspension on the eligibility generation.

An admission binds the generation observed when its entry was authorized and records its
effect in the transaction that holds the eligibility locks. Suspension advances the
generation. Both lock the global gate first and then scopes in sorted order. The admitted
effect here is a FIXTURE event; no order, reservation or adapter is involved.
"""

import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from uuid import uuid4

import pytest
import strategy_fixtures as fx
from aura.events import Event, emit
from aura.strategies import STATUS_CHANGED, check_entry_eligibility, suspend
from aura.strategies import eligibility as el
from sqlalchemy import event, select, text

pytestmark = pytest.mark.integration

ADMITTED = "FIXTURE_ENTRY_ADMITTED"


def activated(db, universe=None):
    """An ACTIVE_PAPER FIXTURE scope: (id, version, eligibility generation)."""
    with db.transaction() as s:
        scope = fx.fixture_scope(s, universe_ref=universe)
        fx.activate(s, scope)
        return scope.id, scope.version, scope.eligibility_generation


def admit(session, scope_id, generation):
    """Check eligibility and, only when permitted, record the admission in the same
    transaction, while the gate and scope locks are held."""
    decision = check_entry_eligibility(
        session,
        {scope_id: generation},
        portfolio_id=fx.PORTFOLIO,
        approved_policies=(fx.FIXTURE_POLICY,),
        at=datetime.now(UTC),
    )
    if decision.permitted:
        payload = {"scope_id": scope_id, "generation": generation}
        emit(session, ADMITTED, scope_id, generation, str(uuid4()), payload)
    session.flush()
    return decision


def admit_now(db, scope_id, generation, barrier=None):
    if barrier is not None:
        barrier.wait()
    with db.transaction() as s:
        return admit(s, scope_id, generation)


def suspend_now(db, scope_id, version, barrier=None):
    if barrier is not None:
        barrier.wait()
    with db.transaction() as s:
        return suspend(
            s,
            scope_id,
            expected_version=version,
            command_id=str(uuid4()),
            reason="suspended during admission",
            actor="owner",
        )


def events(db, kind, scope_id):
    with db.transaction() as s:
        return s.scalars(
            select(Event)
            .where(Event.event_type == kind, Event.aggregate_id == scope_id)
            .order_by(Event.sequence)
        ).all()


def wait_until_blocked(db, timeout=10.0):
    """Wait until a backend on this test database is waiting for a lock."""
    deadline = time.monotonic() + timeout
    with db.engine.connect() as connection:
        while time.monotonic() < deadline:
            waiting = connection.execute(
                text(
                    "SELECT count(*) FROM pg_stat_activity "
                    "WHERE datname = current_database() AND wait_event_type = 'Lock'"
                )
            ).scalar_one()
            connection.rollback()  # activity statistics are cached per transaction
            if waiting:
                return
            time.sleep(0.02)
    raise AssertionError("The competing transaction never waited for a lock")


def race(db, holder, contender):
    """Run `holder` in an open transaction, start `contender` in another thread, wait until
    it blocks on a lock, then commit the holder. Returns both results."""
    session = db.sessions()
    pool = ThreadPoolExecutor(max_workers=1)
    try:
        held = holder(session)
        pending = pool.submit(contender)
        try:
            wait_until_blocked(db)
        finally:
            session.commit()
        return held, pending.result(timeout=30)
    finally:
        session.close()
        pool.shutdown(wait=True)


def test_admission_after_a_committed_suspension_is_refused(db):
    scope_id, version, generation = activated(db)
    change, decision = race(
        db,
        lambda s: suspend(
            s,
            scope_id,
            expected_version=version,
            command_id=str(uuid4()),
            reason="incident review",
            actor="owner",
        ),
        lambda: admit_now(db, scope_id, generation),
    )
    assert change.eligibility_generation == generation + 1
    assert not decision.permitted
    (result,) = decision.scopes
    assert (result.state, result.generation) == ("SUSPENDED", generation + 1)
    assert el.STALE_ELIGIBILITY_GENERATION in result.blockers
    assert events(db, ADMITTED, scope_id) == []


def test_admission_committed_before_suspension_is_in_flight_and_not_repeatable(db):
    scope_id, version, generation = activated(db)
    decision, change = race(
        db,
        lambda s: admit(s, scope_id, generation),
        lambda: suspend_now(db, scope_id, version),
    )
    assert decision.permitted
    assert change.eligibility_generation == generation + 1
    (admission,) = events(db, ADMITTED, scope_id)
    suspension = events(db, STATUS_CHANGED, scope_id)[-1]
    # The admission was serialized before the suspension, on the then-current generation.
    assert admission.payload["generation"] == generation == suspension.payload["prior_generation"]
    assert admission.sequence < suspension.sequence
    # The admitted entry stays in flight for reconciliation; nothing can be admitted again.
    for bound in (generation, generation + 1):
        assert not admit_now(db, scope_id, bound).permitted
    assert len(events(db, ADMITTED, scope_id)) == 1


def test_concurrent_admission_and_suspension_never_admit_on_a_stale_generation(db):
    scopes = [activated(db, universe=f"FIXTURE_RACE_{n}") for n in range(20)]
    outcomes = []
    with ThreadPoolExecutor(max_workers=2) as pool:
        for scope_id, version, generation in scopes:
            barrier = threading.Barrier(2, timeout=30)
            admission = pool.submit(admit_now, db, scope_id, generation, barrier)
            suspension = pool.submit(suspend_now, db, scope_id, version, barrier)
            outcomes.append(
                (scope_id, generation, admission.result(timeout=60), suspension.result(timeout=60))
            )
    for scope_id, generation, decision, change in outcomes:
        assert change.eligibility_generation == generation + 1
        suspended = events(db, STATUS_CHANGED, scope_id)[-1]
        admitted = events(db, ADMITTED, scope_id)
        if decision.permitted:
            (admission,) = admitted
            assert admission.payload["generation"] == suspended.payload["prior_generation"]
            assert admission.sequence < suspended.sequence
        else:
            assert admitted == []
            assert decision.scopes[0].generation == generation + 1
            assert el.STALE_ELIGIBILITY_GENERATION in decision.scopes[0].blockers


def test_gate_is_locked_before_scopes_and_scopes_in_sorted_order(db):
    with db.transaction() as s:
        ids = [fx.fixture_scope(s, universe_ref=f"FIXTURE_ORDER_{n}").id for n in "CAB"]
    locks = []

    def record(connection, cursor, statement, parameters, context, executemany):
        if "FOR UPDATE" in statement:
            table = "execution_gates" if "FROM execution_gates" in statement else "strategy_scopes"
            locks.append((table, next(iter(parameters.values()))))

    event.listen(db.engine, "before_cursor_execute", record)
    try:
        with db.transaction() as s:
            check_entry_eligibility(
                s,
                {scope_id: 1 for scope_id in reversed(ids)},
                portfolio_id=fx.PORTFOLIO,
                approved_policies=(),
                at=datetime.now(UTC),
            )
        admission_locks = list(locks)
        locks.clear()
        suspend_now(db, ids[0], 1)
        suspension_locks = list(locks)
    finally:
        event.remove(db.engine, "before_cursor_execute", record)
    assert admission_locks == [("execution_gates", "global")] + [
        ("strategy_scopes", scope_id) for scope_id in sorted(ids)
    ]
    assert suspension_locks == [("execution_gates", "global"), ("strategy_scopes", ids[0])]
