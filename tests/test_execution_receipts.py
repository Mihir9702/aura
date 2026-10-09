"""Durable execution receipts, quarantine and redrive (S05) on real PostgreSQL.

Receipts come from the fixture adapter account under FIXTURE_POLICY. The only portfolio used
is the SHADOW fixture that tests/conftest.py seeds with 500.00 USD.
"""

import threading
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal as D
from uuid import uuid4

import pytest
from aura.common import Conflict
from aura.events import Event
from aura.execution import (
    FIXTURE_ACCOUNT_ID,
    FIXTURE_ADAPTER_ID,
    ExecutionReceipt,
    FixtureFill,
    FixtureOrder,
    QuarantineEntry,
    admit_fixture,
    apply_receipt,
    apply_received_fills,
    handle_fill_received,
    receive_fixture_fill,
    record_fill,
    record_submission,
    redrive_quarantine,
    reserve_fixture,
)
from aura.ledger import FIXTURE_POLICY, Journal, Lot, apply_fill, balances, snapshot
from aura.operations import Gate, change_control
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError

pytestmark = pytest.mark.integration

SCOPE = "momentum:daily"


def reserve(db, order="buy", side="BUY", qty="2.5", bound="100", fee="0.50"):
    with db.transaction() as s:
        return reserve_fixture(
            s,
            portfolio_id="fixture",
            order_id=order,
            instrument="TEST",
            scope=SCOPE,
            side=side,
            quantity=D(qty),
            price_bound=D(bound),
            fee_budget=D(fee),
            increment=D("0.01"),
            fractional_supported=True,
            policy=FIXTURE_POLICY,
        )


def fill(
    order="buy",
    execution="e1",
    qty="1",
    price="100",
    fee="0.25",
    revision=1,
    account=FIXTURE_ACCOUNT_ID,
):
    return FixtureFill(
        adapter_id=FIXTURE_ADAPTER_ID,
        adapter_account_id=account,
        execution_id=execution,
        revision=revision,
        order_id=order,
        quantity=D(qty),
        price=D(price),
        fee=D(fee),
        policy=FIXTURE_POLICY,
    )


def redrive(db, entry, version, command=None, reason="order record restored"):
    with db.transaction() as s:
        return redrive_quarantine(
            s,
            quarantine_id=entry,
            expected_version=version,
            command_id=command or str(uuid4()),
            reason=reason,
        )


def restore_order(db, order="late", side="BUY", qty="2.5", bound="100", fee="0.50"):
    """Commit an order record that was missing when its fill arrived.

    Stands in for a restore or an adapter-history reconciliation: the order existed before its
    fill, so this is not a new reservation (which open quarantine refuses).
    """
    with db.transaction() as s:
        s.add(
            FixtureOrder(
                id=order,
                portfolio_id="fixture",
                instrument="TEST",
                strategy_scope=SCOPE,
                side=side,
                quantity=D(qty),
                price_bound=D(bound),
                fee_budget=D(fee),
                filled=D(0),
                fees_paid=D(0),
                state="ACKNOWLEDGED",
                gate_version=1,
            )
        )


def fill_journals(s):
    return s.scalars(select(Journal).where(Journal.source_key.like("receipt:%"))).all()


def events(s, kind):
    return s.scalars(select(Event).where(Event.event_type == kind).order_by(Event.sequence)).all()


def test_receipt_is_committed_before_it_is_applied(db):
    reserve(db)
    with db.transaction() as s:
        receipt_id = record_fill(s, fill()).receipt_id
    # The applying transaction books the fill, then fails before commit.
    with pytest.raises(RuntimeError), db.transaction() as s:
        assert apply_receipt(s, receipt_id).status == "APPLIED"
        raise RuntimeError("crash before commit")
    with db.transaction() as s:
        assert s.get(ExecutionReceipt, receipt_id).status == "RECEIVED"
        assert not fill_journals(s)
        assert s.get(FixtureOrder, "buy").filled == 0
    # The committed receipt's event lets the worker's consumer finish the work once.
    with db.transaction() as s:
        assert apply_received_fills(s) == 1
    with db.transaction() as s:
        receipt = s.get(ExecutionReceipt, receipt_id)
        (journal,) = fill_journals(s)
        assert (receipt.status, receipt.journal_id) == ("APPLIED", journal.id)
        assert journal.source_key == "receipt:" + receipt_id
        assert journal.facts["execution_id"] == "e1"
        assert s.get(FixtureOrder, "buy").filled == 1
        assert apply_received_fills(s) == 0


def test_repeated_receipt_redelivery_and_redrive_post_once(db):
    """AC-02: one economic posting and one quantity change."""
    first = receive_fixture_fill(db, fill(order="late"))
    assert (first.status, first.reason) == ("QUARANTINED", "ORPHAN")
    # A repeat before the order is known neither posts nor opens another entry.
    assert receive_fixture_fill(db, fill(order="late")).quarantine_id == first.quarantine_id
    restore_order(db)
    with db.transaction() as s:
        (received,) = events(s, "FIXTURE_FILL_RECEIVED")
        # Delivering the event does not apply a quarantined receipt; that takes a redrive.
        assert handle_fill_received(s, received).status == "QUARANTINED"
        # Redelivering the same event is absorbed by the consumer inbox.
        assert handle_fill_received(s, received) is None
    command = str(uuid4())
    applied = redrive(db, first.quarantine_id, 1, command)
    assert (applied.outcome, applied.status, applied.version) == ("APPLIED", "RESOLVED", 2)
    # The same command returns its recorded outcome; a new command finds the entry resolved.
    assert redrive(db, first.quarantine_id, 1, command) == applied
    with pytest.raises(Conflict, match="resolved"):
        redrive(db, first.quarantine_id, 2)
    # Later repeats of the receipt and of its event change nothing.
    again = receive_fixture_fill(db, fill(order="late"))
    assert (again.status, again.journal_id) == ("ALREADY_APPLIED", applied.journal_id)
    with db.transaction() as s:
        (received,) = events(s, "FIXTURE_FILL_RECEIVED")
        assert handle_fill_received(s, received) is None
        assert apply_received_fills(s) == 0
        assert apply_receipt(s, first.receipt_id).status == "ALREADY_APPLIED"
    with db.transaction() as s:
        (journal,) = fill_journals(s)
        assert journal.id == applied.journal_id
        posted = [e.payload["source"] for e in events(s, "LEDGER_POSTED")]
        assert posted.count(journal.source_key) == 1
        assert s.get(FixtureOrder, "late").filled == 1
        assert [lot.quantity for lot in s.scalars(select(Lot))] == [1]
        assert balances(s, "fixture")["cash"] == D("399.75")
        assert len(s.scalars(select(ExecutionReceipt)).all()) == 1


def test_fill_before_acknowledgement_is_valid(db):
    reserve(db)
    with db.transaction() as s:
        admit_fixture(s, "buy")
    assert receive_fixture_fill(db, fill()).status == "APPLIED"
    with db.transaction() as s:
        assert s.get(FixtureOrder, "buy").state == "PARTIALLY_FILLED"
        # The acknowledgement arrives after the fill and cannot move the order backward.
        record_submission(s, "buy", "ACKNOWLEDGED")
    with db.transaction() as s:
        assert s.get(FixtureOrder, "buy").state == "PARTIALLY_FILLED"
    assert receive_fixture_fill(db, fill(execution="e2", qty="1.5")).status == "APPLIED"
    # An unknown submission outcome is reconciled through the client order ID a fill carries.
    reserve(db, "unknown", qty="1", fee="0.25")
    with db.transaction() as s:
        admit_fixture(s, "unknown")
    with db.transaction() as s:
        record_submission(s, "unknown", "SUBMISSION_UNKNOWN")
    outcome = receive_fixture_fill(db, fill(order="unknown", execution="e3"))
    assert outcome.status == "APPLIED"
    with db.transaction() as s:
        assert [s.get(FixtureOrder, o).state for o in ("buy", "unknown")] == ["FILLED", "FILLED"]
        assert len(fill_journals(s)) == 3


def test_orphan_fill_is_applied_once_after_its_order_appears(db):
    orphan = receive_fixture_fill(db, fill(order="late"))
    assert (orphan.status, orphan.reason) == ("QUARANTINED", "ORPHAN")
    with db.transaction() as s:  # committed with its event, not rolled back
        entry = s.get(QuarantineEntry, orphan.quarantine_id)
        assert (entry.status, entry.portfolio_id) == ("OPEN", None)
        (event,) = events(s, "FIXTURE_FILL_QUARANTINED")
        assert (event.payload["quarantine_id"], event.payload["reason"]) == (entry.id, "ORPHAN")
    # An orphan cannot be attributed, so it refuses new reservations for every portfolio.
    with pytest.raises(Conflict, match="quarantine"):
        reserve(db, "other")
    still = redrive(db, orphan.quarantine_id, 1)
    assert (still.outcome, still.reason, still.version) == ("STILL_QUARANTINED", "ORPHAN", 2)
    restore_order(db)
    applied = redrive(db, orphan.quarantine_id, 2)
    assert (applied.outcome, applied.status, applied.version) == ("APPLIED", "RESOLVED", 3)
    with pytest.raises(Conflict):
        redrive(db, orphan.quarantine_id, 3)
    with db.transaction() as s:
        assert apply_receipt(s, orphan.receipt_id).status == "ALREADY_APPLIED"
    with db.transaction() as s:
        assert len(fill_journals(s)) == 1
        assert s.get(FixtureOrder, "late").filled == 1
    reserve(db, "other", qty="1")  # the resolved entry no longer blocks


def test_concurrent_appliers_post_once(db):
    reserve(db)
    receivers = threading.Barrier(2)

    def receive(_):
        receivers.wait(timeout=10)
        return receive_fixture_fill(db, fill()).status

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(receive, range(2))) == ["ALREADY_APPLIED", "APPLIED"]

    with db.transaction() as s:
        receipt_id = record_fill(s, fill(execution="e2")).receipt_id
    appliers = threading.Barrier(3)

    def apply(_):
        with db.transaction() as s:
            appliers.wait(timeout=10)
            return apply_receipt(s, receipt_id).status

    with ThreadPoolExecutor(max_workers=3) as pool:
        statuses = sorted(pool.map(apply, range(3)))
    assert statuses == ["ALREADY_APPLIED", "ALREADY_APPLIED", "APPLIED"]
    with db.transaction() as s:  # the consumer then finds both receipts applied
        assert apply_received_fills(s) == 2
    with db.transaction() as s:
        assert len(fill_journals(s)) == 2
        assert s.get(FixtureOrder, "buy").filled == 2
        assert sum(lot.quantity for lot in s.scalars(select(Lot))) == 2


def test_receipts_are_accepted_under_full_kill(db):
    reserve(db)
    with db.transaction() as s:
        admit_fixture(s, "buy")
    with db.transaction() as s:
        change_control(s, "full_kill", True, 1, "test incident", str(uuid4()))
    assert receive_fixture_fill(db, fill()).status == "APPLIED"
    orphan = receive_fixture_fill(db, fill(order="late", execution="e2"))
    assert (orphan.status, orphan.reason) == ("QUARANTINED", "ORPHAN")
    restore_order(db)
    assert redrive(db, orphan.quarantine_id, 1).outcome == "APPLIED"  # reconciliation continues
    with pytest.raises(Conflict, match="Capability control"):
        reserve(db, "other", qty="1")
    with db.transaction() as s:
        assert s.get(Gate, "global").full_kill
        assert len(fill_journals(s)) == 2


def test_worked_example_reconciles_through_receipts(db):
    reserve(db)
    assert receive_fixture_fill(db, fill(qty="2.5", fee="0.50")).status == "APPLIED"
    assert receive_fixture_fill(db, fill(qty="2.5", fee="0.50")).status == "ALREADY_APPLIED"
    reserve(db, "sell", "SELL", "0.75", "110", "0.25")
    sold = fill(order="sell", execution="e2", qty="0.75", price="110", fee="0.25")
    assert receive_fixture_fill(db, sold).status == "APPLIED"
    with db.transaction() as s:
        state = snapshot(s, "fixture")
        accounts = balances(s, "fixture")
        (position,) = state["positions"]
        quantity, basis = D(position["quantity"]), D(position["cost_basis"])
        assert (D(state["cash"]), quantity, basis) == (D("331.75"), D("1.75"), D("175"))
        assert sum(accounts.values()) == 0
        realized, fees = -accounts["realized_gain"], accounts["fees"]
        assert (realized, fees) == (D("7.5"), D("0.75"))
        mark = D("110")
        unrealized = quantity * mark - basis
        assert unrealized == D("17.5")
        equity = D(state["cash"]) + quantity * mark
        assert equity == D("524.25") == D(state["capital"]) + realized + unrealized - fees
        assert len(fill_journals(s)) == 2


def test_conflicting_duplicate_is_committed_to_quarantine(db):
    reserve(db)
    applied = receive_fixture_fill(db, fill())
    conflict = receive_fixture_fill(db, fill(qty="2"))
    assert (conflict.status, conflict.reason) == ("QUARANTINED", "CONFLICTING_DUPLICATE")
    assert conflict.receipt_id == applied.receipt_id
    # The same conflicting delivery again does not open a second entry.
    assert receive_fixture_fill(db, fill(qty="2")).quarantine_id == conflict.quarantine_id
    with db.transaction() as s:
        entry = s.get(QuarantineEntry, conflict.quarantine_id)
        assert (entry.portfolio_id, entry.delivered["quantity"]) == ("fixture", "2")
        assert s.get(ExecutionReceipt, applied.receipt_id).status == "APPLIED"
        assert s.get(FixtureOrder, "buy").filled == 1
        assert len(fill_journals(s)) == 1
        reasons = [e.payload["reason"] for e in events(s, "FIXTURE_FILL_QUARANTINED")]
        assert reasons == ["CONFLICTING_DUPLICATE"]
    with pytest.raises(Conflict, match="quarantine"):
        reserve(db, "other", qty="1")
    # Both deliveries are immutable, so a redrive re-validates and keeps the entry open.
    still = redrive(db, conflict.quarantine_id, 1)
    assert (still.outcome, still.status, still.version) == ("STILL_QUARANTINED", "OPEN", 2)
    # A conflict that arrives before application holds the receipt of record too.
    with db.transaction() as s:
        pending = record_fill(s, fill(execution="e2")).receipt_id
    held = receive_fixture_fill(db, fill(execution="e2", qty="1.5"))
    assert (held.receipt_id, held.reason) == (pending, "CONFLICTING_DUPLICATE")
    with db.transaction() as s:
        assert apply_receipt(s, pending).status == "QUARANTINED"
        assert s.get(ExecutionReceipt, pending).status == "QUARANTINED"
        assert len(fill_journals(s)) == 1


@pytest.mark.parametrize(
    ("changes", "reason"),
    [
        ({"price": "100.01"}, "OUT_OF_BOUNDS"),  # a BUY above its price bound
        ({"qty": "2.51"}, "OUT_OF_BOUNDS"),  # more than the unfilled quantity
        ({"fee": "0.51"}, "OUT_OF_BOUNDS"),  # beyond the fixture fee budget
        ({"qty": "0"}, "OUT_OF_BOUNDS"),
        ({"price": "-1"}, "OUT_OF_BOUNDS"),
        ({"revision": 2}, "UNSUPPORTED_REVISION"),  # a correction without an OD-11 policy
        ({"account": "another-account"}, "UNKNOWN_ACCOUNT"),
        # 0.33333333 x 1.00000001 needs 16 places; rounding it would be unapproved policy.
        ({"qty": "0.33333333", "price": "1.00000001"}, "LEDGER_REJECTED"),
    ],
)
def test_invalid_fills_are_quarantined_not_raised(db, changes, reason):
    reserve(db)
    outcome = receive_fixture_fill(db, fill(**changes))
    assert (outcome.status, outcome.reason) == ("QUARANTINED", reason)
    with db.transaction() as s:
        entry = s.get(QuarantineEntry, outcome.quarantine_id)
        assert (entry.status, entry.reason) == ("OPEN", reason)
        assert entry.portfolio_id == (None if reason == "UNKNOWN_ACCOUNT" else "fixture")
        assert s.get(ExecutionReceipt, outcome.receipt_id).status == "QUARANTINED"
        assert s.get(FixtureOrder, "buy").filled == 0
        assert not fill_journals(s)
        assert len(events(s, "FIXTURE_FILL_QUARANTINED")) == 1
    with pytest.raises(Conflict, match="quarantine"):
        reserve(db, "other", qty="1")
    # Immutable facts that fail validation still fail on redrive.
    assert redrive(db, outcome.quarantine_id, 1).outcome == "STILL_QUARANTINED"


def test_ledger_refuses_to_post_a_receipt_twice(db):
    reserve(db)
    applied = receive_fixture_fill(db, fill())
    with pytest.raises(Conflict, match="already posted"), db.transaction() as s:
        apply_fill(
            s,
            receipt_id=applied.receipt_id,
            portfolio_id="fixture",
            policy=FIXTURE_POLICY,
            instrument="TEST",
            scope=SCOPE,
            side="BUY",
            quantity=D("1"),
            price=D("100"),
            fee=D("0.25"),
            facts={},
        )
    with db.transaction() as s:
        assert len(fill_journals(s)) == 1


def test_later_revision_of_an_applied_execution_is_quarantined(db):
    reserve(db)
    assert receive_fixture_fill(db, fill()).status == "APPLIED"
    correction = receive_fixture_fill(db, fill(revision=2, qty="0.5"))
    assert (correction.status, correction.reason) == ("QUARANTINED", "UNSUPPORTED_REVISION")
    with db.transaction() as s:
        assert s.get(FixtureOrder, "buy").filled == 1
        assert len(fill_journals(s)) == 1


def test_sell_booked_before_its_buy_is_redriven_once_holdings_exist(db):
    reserve(db)
    restore_order(db, "exit", side="SELL", qty="1", bound="100", fee="0")
    early = receive_fixture_fill(db, fill(order="exit", execution="s1", fee="0"))
    assert (early.status, early.reason) == ("QUARANTINED", "LEDGER_REJECTED")
    # Receipts for existing orders still apply while the portfolio is blocked.
    assert receive_fixture_fill(db, fill(qty="2.5", fee="0.50")).status == "APPLIED"
    assert redrive(db, early.quarantine_id, 1).outcome == "APPLIED"
    with db.transaction() as s:
        (position,) = snapshot(s, "fixture")["positions"]
        assert D(position["quantity"]) == D("1.5")
        assert balances(s, "fixture")["cash"] == D("500") - D("250.50") + D("100")
        assert len(fill_journals(s)) == 2


def test_receipts_and_quarantine_are_append_only(db):
    reserve(db)
    applied = receive_fixture_fill(db, fill())
    orphan = receive_fixture_fill(db, fill(order="late", execution="e2"))
    copy = (
        "INSERT INTO execution_receipts (id, adapter_id, adapter_account_id, execution_id,"
        " revision, order_id, quantity, price, fee, policy) SELECT 'copy', adapter_id,"
        " adapter_account_id, execution_id, revision, order_id, quantity, price, fee, policy"
        " FROM execution_receipts WHERE id = :id"
    )
    refused = [
        ("UPDATE execution_receipts SET quantity = 2 WHERE id = :id", applied, "immutable"),
        (
            "UPDATE execution_receipts SET status = 'RECEIVED', journal_id = NULL WHERE id = :id",
            applied,
            "final",
        ),
        ("DELETE FROM execution_receipts WHERE id = :id", orphan, "append-only"),
        (copy, applied, "execution_receipts_identity"),
    ]
    for statement, outcome, message in refused:
        with pytest.raises(DBAPIError, match=message), db.transaction() as s:
            s.execute(text(statement), {"id": outcome.receipt_id})
    entry = {"id": orphan.quarantine_id}
    with pytest.raises(DBAPIError, match="immutable"), db.transaction() as s:
        s.execute(text("UPDATE execution_quarantine SET delivered = '{}' WHERE id = :id"), entry)
    with pytest.raises(DBAPIError, match="append-only"), db.transaction() as s:
        s.execute(text("DELETE FROM execution_quarantine WHERE id = :id"), entry)
    restore_order(db)
    assert redrive(db, orphan.quarantine_id, 1).status == "RESOLVED"
    reopen = "UPDATE execution_quarantine SET status = 'OPEN', resolved_at = NULL WHERE id = :id"
    with pytest.raises(DBAPIError, match="final"), db.transaction() as s:
        s.execute(text(reopen), entry)


def test_worker_tick_applies_committed_fill_under_full_kill(db):
    """S05/S06 wiring: blocked submission must not block committed receipt accounting."""
    from aura.worker import tick

    class NoSubmissionJobs:
        def run_available(self, limit: int) -> list[object]:
            assert limit == 0
            return []

    reserve(db)
    with db.transaction() as session:
        receipt_id = record_fill(session, fill()).receipt_id
        change_control(session, "full_kill", True, 1, "receipt-consumer drill", str(uuid4()))

    assert tick(db, NoSubmissionJobs(), jobs_per_tick=0)[1] == []
    with db.transaction() as session:
        receipt = session.get(ExecutionReceipt, receipt_id)
        assert receipt is not None and receipt.status == "APPLIED"
        assert len(fill_journals(session)) == 1

    assert tick(db, NoSubmissionJobs(), jobs_per_tick=0)[1] == []
    with db.transaction() as session:
        assert len(fill_journals(session)) == 1
