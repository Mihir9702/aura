"""Durable fixture fill receipts: record, apply each fill once, quarantine and redrive.

Receiving takes two transactions. The first commits the receipt, unique per adapter, account,
execution ID and revision, and emits FIXTURE_FILL_RECEIVED. The second locks the global gate,
re-reads the receipt and either books it through the Ledger (journal source
"receipt:<receipt id>") and advances the order, or commits a quarantine entry and a
FIXTURE_FILL_QUARANTINED event. A repeated receipt, a redelivered event, a concurrent applier or
a repeated redrive then finds the receipt APPLIED and changes nothing.

Receipts are facts, not submissions: Entry Halt and Full Kill never block recording, applying
or redriving them. Only SHADOW fixture portfolios under FIXTURE_POLICY are supported. Execution
revisions after the first are corrections, which stay quarantined until OD-11 policy exists.
"""

from dataclasses import asdict, dataclass
from decimal import Decimal
from typing import Any, Literal
from uuid import uuid4

from sqlalchemy import exists, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from aura.common import Conflict, amount, now
from aura.events import Event, Inbox, emit
from aura.execution.adapter import FIXTURE_ACCOUNT_ID, FIXTURE_ADAPTER_ID
from aura.execution.orders import FixtureOrder
from aura.execution.receipts import ExecutionReceipt, QuarantineEntry, Reason
from aura.ledger import FIXTURE_POLICY, LedgerRejected, apply_fill
from aura.operations import locked_gate
from aura.storage import Database

RECEIVED = "FIXTURE_FILL_RECEIVED"
QUARANTINED = "FIXTURE_FILL_QUARANTINED"
REDRIVEN = "FIXTURE_FILL_REDRIVEN"
RECEIPT_CONSUMER = "fixture-fill-applier-v1"


@dataclass(frozen=True)
class FixtureFill:
    """One normalized fill report from the fixture adapter, in exact decimals."""

    adapter_id: str
    adapter_account_id: str
    execution_id: str
    revision: int
    order_id: str  # the client order ID the adapter reports
    quantity: Decimal
    price: Decimal
    fee: Decimal
    policy: str  # accounting policy to book under; only FIXTURE_POLICY is accepted


@dataclass(frozen=True)
class Recorded:
    receipt_id: str
    duplicate: bool
    quarantine_id: str | None = None  # set when the delivery conflicts with the receipt


@dataclass(frozen=True)
class FillOutcome:
    receipt_id: str
    status: Literal["APPLIED", "ALREADY_APPLIED", "QUARANTINED"]
    journal_id: str | None = None
    quarantine_id: str | None = None
    reason: str | None = None


@dataclass(frozen=True)
class RedriveOutcome:
    quarantine_id: str
    receipt_id: str
    outcome: Literal["APPLIED", "STILL_QUARANTINED"]
    status: Literal["OPEN", "RESOLVED"]
    version: int
    reason: str | None  # the entry's reason when it stays open
    journal_id: str | None


@dataclass(frozen=True)
class _Problem:
    reason: Reason
    detail: str
    portfolio_id: str | None


def _text(value: Decimal) -> str:
    return format(value, "f")


def _facts(
    order_id: str, quantity: Decimal, price: Decimal, fee: Decimal, policy: str
) -> dict[str, Any]:
    return {
        "order_id": order_id,
        "quantity": _text(quantity),
        "price": _text(price),
        "fee": _text(fee),
        "policy": policy,
    }


def _receipt_facts(receipt: ExecutionReceipt) -> dict[str, Any]:
    return _facts(receipt.order_id, receipt.quantity, receipt.price, receipt.fee, receipt.policy)


def record_fill(session: Session, fill: FixtureFill) -> Recorded:
    """Durably record one fill report. Commit this before applying it.

    An identical repeat returns the existing receipt. A repeat with different facts keeps the
    receipt of record and commits a CONFLICTING_DUPLICATE quarantine entry; if that receipt is
    not applied yet, it is held too. Controls are not consulted.
    """
    quantity, price, fee = map(amount, (fill.quantity, fill.price, fill.fee))
    if fill.revision < 1 or not all(
        (fill.adapter_id, fill.adapter_account_id, fill.execution_id, fill.order_id)
    ):
        raise ValueError("A receipt needs adapter, account, execution and order IDs, revision >= 1")
    if fill.policy != FIXTURE_POLICY:
        raise Conflict("Fixture receipts require the explicit fixture accounting policy")
    receipt_id = str(uuid4())
    inserted = session.execute(
        pg_insert(ExecutionReceipt)
        .values(
            id=receipt_id,
            adapter_id=fill.adapter_id,
            adapter_account_id=fill.adapter_account_id,
            execution_id=fill.execution_id,
            revision=fill.revision,
            order_id=fill.order_id,
            quantity=quantity,
            price=price,
            fee=fee,
            policy=fill.policy,
            received_at=now(),
            status="RECEIVED",
        )
        .on_conflict_do_nothing(
            index_elements=["adapter_id", "adapter_account_id", "execution_id", "revision"]
        )
        .returning(ExecutionReceipt.id)
    ).scalar_one_or_none()
    if inserted is not None:
        emit(
            session,
            RECEIVED,
            receipt_id,
            1,
            fill.order_id,
            {
                "receipt_id": receipt_id,
                "order_id": fill.order_id,
                "adapter_id": fill.adapter_id,
                "adapter_account_id": fill.adapter_account_id,
                "execution_id": fill.execution_id,
                "revision": fill.revision,
                "environment": "SHADOW",
            },
        )
        return Recorded(receipt_id, duplicate=False)
    receipt = session.scalars(
        select(ExecutionReceipt)
        .where(
            ExecutionReceipt.adapter_id == fill.adapter_id,
            ExecutionReceipt.adapter_account_id == fill.adapter_account_id,
            ExecutionReceipt.execution_id == fill.execution_id,
            ExecutionReceipt.revision == fill.revision,
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    ).one()
    if (receipt.order_id, receipt.quantity, receipt.price, receipt.fee, receipt.policy) == (
        fill.order_id,
        quantity,
        price,
        fee,
        fill.policy,
    ):
        return Recorded(receipt.id, duplicate=True)
    delivered = _facts(fill.order_id, quantity, price, fee, fill.policy)
    for entry in _open_entries(session, receipt.id, Reason.CONFLICTING_DUPLICATE):
        if entry.delivered == delivered:  # the same conflicting delivery, repeated
            return Recorded(receipt.id, duplicate=True, quarantine_id=entry.id)
    order = session.get(FixtureOrder, receipt.order_id)
    if receipt.status == "RECEIVED":
        receipt.status = "QUARANTINED"  # neither version may post until reconciled
    entry = _quarantine(
        session,
        receipt,
        _Problem(
            Reason.CONFLICTING_DUPLICATE,
            "Execution identity reused with different facts",
            order.portfolio_id if order else None,
        ),
        delivered,
    )
    return Recorded(receipt.id, duplicate=True, quarantine_id=entry.id)


def apply_receipt(session: Session, receipt_id: str) -> FillOutcome:
    """Apply a committed receipt once, or quarantine it within this transaction.

    Serializes with reservations and other appliers on the global gate. An APPLIED receipt is a
    no-op and a QUARANTINED one waits for an explicit redrive.
    """
    locked_gate(session)
    receipt = session.get(
        ExecutionReceipt, receipt_id, with_for_update=True, populate_existing=True
    )
    if receipt is None:
        raise LookupError("Unknown execution receipt")
    if receipt.status == "APPLIED":
        return FillOutcome(receipt.id, "ALREADY_APPLIED", journal_id=receipt.journal_id)
    if receipt.status == "QUARANTINED":
        held = _open_entries(session, receipt.id)
        latest = held[-1] if held else None
        return FillOutcome(
            receipt.id,
            "QUARANTINED",
            quarantine_id=latest.id if latest else None,
            reason=latest.reason if latest else None,
        )
    problem, journal_id = _book(session, receipt)
    if problem:
        receipt.status = "QUARANTINED"
        entry = _quarantine(session, receipt, problem, _receipt_facts(receipt))
        return FillOutcome(
            receipt.id, "QUARANTINED", quarantine_id=entry.id, reason=problem.reason.value
        )
    return FillOutcome(receipt.id, "APPLIED", journal_id=journal_id)


def receive_fixture_fill(db: Database, fill: FixtureFill) -> FillOutcome:
    """Commit the receipt, then apply or quarantine it in a second transaction.

    If the second transaction fails, the committed receipt stays RECEIVED and its
    FIXTURE_FILL_RECEIVED event lets apply_received_fills finish the work.
    """
    with db.transaction() as session:
        recorded = record_fill(session, fill)
    if recorded.quarantine_id:
        return FillOutcome(
            recorded.receipt_id,
            "QUARANTINED",
            quarantine_id=recorded.quarantine_id,
            reason=Reason.CONFLICTING_DUPLICATE.value,
        )
    with db.transaction() as session:
        return apply_receipt(session, recorded.receipt_id)


def apply_received_fills(session: Session) -> int:
    """Consume up to 100 unprocessed FIXTURE_FILL_RECEIVED events; returns how many.

    Inbox rows commit with their effects, and competing workers skip locked events.
    """
    processed = exists(
        select(Inbox.id).where(Inbox.event_id == Event.event_id, Inbox.consumer == RECEIPT_CONSUMER)
    )
    events = session.scalars(
        select(Event)
        .where(Event.event_type == RECEIVED, ~processed)
        .order_by(Event.sequence)
        .limit(100)
        .with_for_update(skip_locked=True)
    ).all()
    for event in events:
        handle_fill_received(session, event)
    return len(events)


def handle_fill_received(session: Session, event: Event) -> FillOutcome | None:
    """Handle one delivery of a FIXTURE_FILL_RECEIVED event. A redelivery returns None."""
    if event.event_type != RECEIVED:
        raise ValueError("Not a fixture fill receipt event")
    first = session.execute(
        pg_insert(Inbox)
        .values(consumer=RECEIPT_CONSUMER, event_id=event.event_id, processed_at=now())
        .on_conflict_do_nothing(index_elements=["consumer", "event_id"])
        .returning(Inbox.id)
    ).scalar_one_or_none()
    if first is None:
        return None
    return apply_receipt(session, str(event.payload["receipt_id"]))


def redrive_quarantine(
    session: Session, *, quarantine_id: str, expected_version: int, command_id: str, reason: str
) -> RedriveOutcome:
    """Owner command: re-validate a quarantined receipt and apply it if it is now valid.

    Idempotent by command_id: a repeated command returns its recorded outcome. A redrive never
    bypasses validation and applies a receipt at most once; an entry that still fails keeps its
    portfolio blocked and records the attempt. Every attempt is audited as FIXTURE_FILL_REDRIVEN.
    """
    locked_gate(session)
    previous = session.scalars(select(Event).where(Event.correlation_id == command_id)).first()
    if previous:
        recorded = previous.payload
        if (
            previous.event_type != REDRIVEN
            or recorded.get("quarantine_id") != quarantine_id
            or recorded.get("expected_version") != expected_version
            or recorded.get("justification") != reason
        ):
            raise Conflict("Command identity reused with different content")
        return RedriveOutcome(
            quarantine_id=recorded["quarantine_id"],
            receipt_id=recorded["receipt_id"],
            outcome=recorded["outcome"],
            status=recorded["status"],
            version=recorded["version"],
            reason=recorded["reason"],
            journal_id=recorded["journal_id"],
        )
    entry = session.get(
        QuarantineEntry, quarantine_id, with_for_update=True, populate_existing=True
    )
    if entry is None:
        raise LookupError("Unknown quarantine entry")
    if entry.version != expected_version:
        raise Conflict("Quarantine entry changed; refresh before trying again")
    if entry.status != "OPEN":
        raise Conflict("Quarantine entry already resolved")
    receipt = session.get(
        ExecutionReceipt, entry.receipt_id, with_for_update=True, populate_existing=True
    )
    if receipt is None:
        raise LookupError("Unknown execution receipt")
    problem: _Problem | None
    journal_id: str | None = None
    if entry.reason == Reason.CONFLICTING_DUPLICATE:
        # Both deliveries are immutable facts, so the conflict itself cannot clear.
        order = session.get(FixtureOrder, receipt.order_id)
        problem = _Problem(
            Reason.CONFLICTING_DUPLICATE, entry.detail, order.portfolio_id if order else None
        )
    elif receipt.status == "APPLIED":
        raise Conflict("Receipt already applied; reconciliation required")
    else:
        problem, journal_id = _book(session, receipt)
    entry.version += 1
    if problem:
        entry.reason = problem.reason.value
        entry.detail = problem.detail
        entry.portfolio_id = problem.portfolio_id
    else:
        entry.status = "RESOLVED"
        entry.resolved_at = now()
    outcome = RedriveOutcome(
        quarantine_id=entry.id,
        receipt_id=receipt.id,
        outcome="STILL_QUARANTINED" if problem else "APPLIED",
        status="OPEN" if problem else "RESOLVED",
        version=entry.version,
        reason=problem.reason.value if problem else None,
        journal_id=journal_id,
    )
    emit(
        session,
        REDRIVEN,
        entry.id,
        entry.version,
        command_id,
        {
            **asdict(outcome),
            "expected_version": expected_version,
            "justification": reason,
            "actor": "owner",
            "environment": "SHADOW",
        },
    )
    session.flush()
    return outcome


def apply_fixture_fill(
    session: Session,
    *,
    order_id: str,
    execution_id: str,
    quantity: Decimal,
    price: Decimal,
    fee: Decimal,
    policy: str,
) -> FillOutcome:
    """Compatibility entry point for callers that hold one transaction.

    Records a fixture-account receipt (revision 1) and applies it in the caller's transaction,
    so the receipt and its effect commit together. receive_fixture_fill commits the receipt
    first and is the durable path. Invalid fills are quarantined and returned, not raised.
    """
    fill = FixtureFill(
        adapter_id=FIXTURE_ADAPTER_ID,
        adapter_account_id=FIXTURE_ACCOUNT_ID,
        execution_id=execution_id,
        revision=1,
        order_id=order_id,
        quantity=quantity,
        price=price,
        fee=fee,
        policy=policy,
    )
    recorded = record_fill(session, fill)
    if recorded.quarantine_id:
        return FillOutcome(
            recorded.receipt_id,
            "QUARANTINED",
            quarantine_id=recorded.quarantine_id,
            reason=Reason.CONFLICTING_DUPLICATE.value,
        )
    return apply_receipt(session, recorded.receipt_id)


def _open_entries(
    session: Session, receipt_id: str, reason: Reason | None = None
) -> list[QuarantineEntry]:
    query = select(QuarantineEntry).where(
        QuarantineEntry.receipt_id == receipt_id, QuarantineEntry.status == "OPEN"
    )
    if reason is not None:
        query = query.where(QuarantineEntry.reason == reason.value)
    return list(session.scalars(query.order_by(QuarantineEntry.created_at, QuarantineEntry.id)))


def _quarantine(
    session: Session, receipt: ExecutionReceipt, problem: _Problem, delivered: dict[str, Any]
) -> QuarantineEntry:
    entry = QuarantineEntry(
        id=str(uuid4()),
        receipt_id=receipt.id,
        portfolio_id=problem.portfolio_id,
        reason=problem.reason.value,
        detail=problem.detail,
        delivered=delivered,
        status="OPEN",
        version=1,
        created_at=now(),
    )
    session.add(entry)
    emit(
        session,
        QUARANTINED,
        entry.id,
        1,
        receipt.order_id,
        {
            "quarantine_id": entry.id,
            "receipt_id": receipt.id,
            "reason": problem.reason.value,
            "portfolio_id": problem.portfolio_id,
            "environment": "SHADOW",
        },
    )
    session.flush()
    return entry


def _diagnose(session: Session, receipt: ExecutionReceipt) -> _Problem | FixtureOrder:
    """The order a receipt can be applied to, or why it cannot be applied."""
    order = session.get(
        FixtureOrder, receipt.order_id, with_for_update=True, populate_existing=True
    )
    portfolio_id = order.portfolio_id if order else None
    if _open_entries(session, receipt.id, Reason.CONFLICTING_DUPLICATE):
        return _Problem(
            Reason.CONFLICTING_DUPLICATE,
            "Another delivery reused this execution identity with different facts",
            portfolio_id,
        )
    if (receipt.adapter_id, receipt.adapter_account_id) != (FIXTURE_ADAPTER_ID, FIXTURE_ACCOUNT_ID):
        return _Problem(Reason.UNKNOWN_ACCOUNT, "Receipt is not from the fixture account", None)
    other_revision = session.scalars(
        select(ExecutionReceipt.id).where(
            ExecutionReceipt.adapter_id == receipt.adapter_id,
            ExecutionReceipt.adapter_account_id == receipt.adapter_account_id,
            ExecutionReceipt.execution_id == receipt.execution_id,
            ExecutionReceipt.id != receipt.id,
        )
    ).first()
    if receipt.revision != 1 or other_revision:
        return _Problem(
            Reason.UNSUPPORTED_REVISION,
            "Execution revisions are corrections; no correction policy is approved (OD-11)",
            portfolio_id,
        )
    if order is None:
        return _Problem(Reason.ORPHAN, "No order has this client order ID", None)
    if min(receipt.quantity, receipt.price) <= 0 or receipt.fee < 0:
        return _Problem(
            Reason.OUT_OF_BOUNDS,
            "Quantity and price must be positive, fee nonnegative",
            portfolio_id,
        )
    if order.filled + receipt.quantity > order.quantity:
        return _Problem(
            Reason.OUT_OF_BOUNDS, "Fill exceeds the unfilled order quantity", portfolio_id
        )
    if order.fees_paid + receipt.fee > order.fee_budget:
        return _Problem(Reason.OUT_OF_BOUNDS, "Fee exceeds the order's fee budget", portfolio_id)
    if (order.side == "BUY" and receipt.price > order.price_bound) or (
        order.side == "SELL" and receipt.price < order.price_bound
    ):
        return _Problem(Reason.OUT_OF_BOUNDS, "Price is outside the authorized bound", portfolio_id)
    return order


def _book(session: Session, receipt: ExecutionReceipt) -> tuple[_Problem | None, str | None]:
    """Apply a valid unapplied receipt: Ledger journal, order fill facts and receipt status."""
    diagnosis = _diagnose(session, receipt)
    if isinstance(diagnosis, _Problem):
        return diagnosis, None
    order = diagnosis
    try:
        with session.begin_nested():
            journal_id = apply_fill(
                session,
                receipt_id=receipt.id,
                portfolio_id=order.portfolio_id,
                policy=receipt.policy,
                instrument=order.instrument,
                scope=order.strategy_scope,
                side=order.side,
                quantity=receipt.quantity,
                price=receipt.price,
                fee=receipt.fee,
                facts={
                    "order_id": order.id,
                    "adapter_id": receipt.adapter_id,
                    "adapter_account_id": receipt.adapter_account_id,
                    "execution_id": receipt.execution_id,
                    "revision": receipt.revision,
                },
            )
    except LedgerRejected as rejected:
        return _Problem(Reason.LEDGER_REJECTED, str(rejected), order.portfolio_id), None
    order.filled += receipt.quantity
    order.fees_paid += receipt.fee
    order.state = "FILLED" if order.filled == order.quantity else "PARTIALLY_FILLED"
    receipt.status = "APPLIED"
    receipt.journal_id = journal_id
    session.flush()
    return None, journal_id
