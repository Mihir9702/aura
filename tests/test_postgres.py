import os
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal as D
from uuid import uuid4

import pytest
from aura.api import create_app
from aura.common import Conflict
from aura.config import Settings
from aura.execution import admit_fixture, record_submission
from aura.ledger import (
    FIXTURE_POLICY,
    FixtureOrder,
    Journal,
    apply_fixture_fill,
    balances,
    fund,
    reserve_fixture,
    snapshot,
)
from aura.operations import change_control
from fastapi.testclient import TestClient
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError

# The shared `db` fixture in tests/conftest.py truncates every public table except
# alembic_version, then re-seeds the global gate and the challenge/fixture funding.
pytestmark = pytest.mark.integration


def reserve(
    db, order="buy", side="BUY", qty="2.5", bound="100", fee="0.50", scope="momentum:daily"
):
    with db.transaction() as s:
        return reserve_fixture(
            s,
            portfolio_id="fixture",
            order_id=order,
            instrument="TEST",
            scope=scope,
            side=side,
            quantity=D(qty),
            price_bound=D(bound),
            fee_budget=D(fee),
            increment=D("0.01"),
            fractional_supported=True,
            policy=FIXTURE_POLICY,
        )


def fill(db, order, execution, qty, price, fee):
    with db.transaction() as s:
        apply_fixture_fill(
            s,
            order_id=order,
            execution_id=execution,
            quantity=D(qty),
            price=D(price),
            fee=D(fee),
            policy=FIXTURE_POLICY,
        )


def test_funding_once_and_fractional_example(db):
    with db.transaction() as s:
        fund(s, "challenge")
        assert (
            len(s.scalars(select(Journal).where(Journal.source_key == "fund:challenge")).all()) == 1
        )
    reserve(db)
    fill(db, "buy", "e1", "2.5", "100", ".5")
    fill(db, "buy", "e1", "2.5", "100", ".5")
    reserve(db, "sell", "SELL", ".75", "110", ".25")
    fill(db, "sell", "e2", ".75", "110", ".25")
    with db.transaction() as s:
        state = snapshot(s, "fixture")
        assert D(state["cash"]) == D("331.75")
        assert D(state["positions"][0]["quantity"]) == D("1.75")
        assert D(state["positions"][0]["cost_basis"]) == D("175")
        b = balances(s, "fixture")
        assert sum(b.values()) == 0
        assert -b["realized_gain"] == D("7.5")
        assert D(state["cash"]) + D("1.75") * 110 == D("524.25")


def test_conflicting_duplicate_is_quarantined_not_applied(db):
    reserve(db)
    fill(db, "buy", "e1", "1", "100", ".25")
    # Since S05 the conflicting delivery commits to quarantine instead of raising.
    fill(db, "buy", "e1", "2", "100", ".25")
    with db.transaction() as s:
        assert s.get(FixtureOrder, "buy").filled == 1


def test_concurrent_pods_cannot_double_spend(db):
    def compete(n):
        try:
            reserve(db, str(n), qty="4", scope=f"pod-{n}:daily")
            return True
        except Conflict:
            return False

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sum(pool.map(compete, [1, 2])) == 1


def test_unknown_submission_retains_reservation_and_cannot_retry(db):
    reserve(db, qty="4")
    with db.transaction() as s:
        admit_fixture(s, "buy")
    with db.transaction() as s:
        record_submission(s, "buy", "SUBMISSION_UNKNOWN")
    with pytest.raises(Conflict):
        reserve(db, "another", qty="2")
    with pytest.raises(Conflict), db.transaction() as s:
        admit_fixture(s, "buy")


def test_halt_blocks_dispatch_but_not_receipts(db):
    reserve(db)
    with db.transaction() as s:
        admit_fixture(s, "buy")
    with db.transaction() as s:
        change_control(s, "full_kill", True, 1, "test incident", str(uuid4()))
    fill(db, "buy", "late", "1", "100", ".25")
    with pytest.raises(Conflict):
        reserve(db, "other")
    with db.transaction() as s:
        assert snapshot(s, "fixture")["positions"]


def test_database_rejects_unbalanced_and_mutated_journals(db):
    with pytest.raises(DBAPIError, match="Unbalanced"), db.transaction() as s:
        s.execute(
            text(
                "INSERT INTO journals (id,portfolio_id,source_key,facts) "
                "VALUES ('bad', 'fixture', 'bad', '{}')"
            )
        )
        s.execute(
            text("INSERT INTO postings (journal_id, account, amount) VALUES ('bad','cash',1)")
        )
    with pytest.raises(DBAPIError), db.transaction() as s:
        s.execute(text("UPDATE postings SET amount=1"))
    with pytest.raises(DBAPIError, match="committed journal"), db.transaction() as s:
        s.execute(
            text(
                "INSERT INTO postings(journal_id,account,amount) "
                "SELECT id,'cash',1 FROM journals LIMIT 1"
            )
        )


def test_exit_reservations_prevent_fractional_oversell(db):
    reserve(db)
    fill(db, "buy", "e1", "2.5", "100", ".5")
    reserve(db, "exit-1", "SELL", "2", "100", "0")
    with pytest.raises(Conflict):
        reserve(db, "exit-2", "SELL", ".75", "100", "0")


def test_gate_change_invalidates_unsubmitted_authorization(db):
    reserve(db)
    with db.transaction() as s:
        change_control(s, "entry_halt", True, 1, "pause entries", str(uuid4()))
    with pytest.raises(Conflict), db.transaction() as s:
        admit_fixture(s, "buy")


def test_outbox_consumer_handles_commit_order_and_retries(db):
    from aura.events import Inbox, emit
    from aura.worker import acknowledge_batch

    slow = db.sessions()
    try:
        emit(slow, "SLOW", "fixture", 1, "slow-commit", {})
        slow.flush()
        with db.transaction() as s:
            emit(s, "FAST", "fixture", 1, "fast-commit", {})
        with db.transaction() as s:
            assert acknowledge_batch(s) >= 1
        slow.commit()
        with db.transaction() as s:
            assert acknowledge_batch(s) == 1
        with db.transaction() as s:
            assert acknowledge_batch(s) == 0
            assert len(s.scalars(select(Inbox)).all()) == 4
    finally:
        slow.close()


def test_fixture_cannot_mutate_active_portfolio(db):
    with pytest.raises(Conflict), db.transaction() as s:
        reserve_fixture(
            s,
            portfolio_id="challenge",
            order_id="bad",
            instrument="TEST",
            scope="pod",
            side="BUY",
            quantity=D(1),
            price_bound=D(1),
            fee_budget=D(0),
            increment=D(1),
            fractional_supported=False,
            policy=FIXTURE_POLICY,
        )


def test_authenticated_api_observe_and_audited_control(db):
    settings = Settings(database_url=os.environ["AURA_TEST_DATABASE_URL"], owner_key="x" * 40)
    with TestClient(create_app(settings)) as client:
        assert client.get("/api/overview").status_code == 401
        headers = {"X-Aura-Command": "1"}
        assert (
            client.post("/api/session", json={"key": "x" * 40}, headers=headers).status_code == 200
        )
        assert client.get("/api/overview").json()["mode"] == "OBSERVE"
        assert client.post("/api/orders", headers=headers).status_code == 404
        body = {
            "active": True,
            "expected_version": 1,
            "reason": "owner requested",
            "command_id": str(uuid4()),
        }
        assert (
            client.post(
                "/api/controls/entry_halt",
                json=body,
                headers={**headers, "Origin": "https://evil.invalid"},
            ).status_code
            == 403
        )
        assert (
            client.post("/api/controls/entry_halt", json=body, headers=headers).status_code == 200
        )
        assert (
            client.post("/api/controls/entry_halt", json=body, headers=headers).status_code == 200
        )
        assert client.get("/api/overview").json()["controls"]["entry_halt"] is True
        assert client.delete("/api/session", headers=headers).status_code == 200
        assert client.get("/api/overview").status_code == 401
