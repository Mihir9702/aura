"""Fixture Order Manager primitives for isolated SHADOW portfolios.

Reservation, dispatch admission and submission results; no adapter call, vendor or active
order route. Capacity comes from Ledger cash and lots. Open execution quarantine refuses new
reservations until it is redriven or reconciled.
"""

from decimal import Decimal

from sqlalchemy import CheckConstraint, ForeignKey, Integer, Numeric, String, select
from sqlalchemy.orm import Mapped, Session, mapped_column

from aura.common import Conflict, amount
from aura.events import emit
from aura.execution.receipts import blocking_quarantine
from aura.ledger import FIXTURE_POLICY, ZERO, balances, fixture_portfolio, held_quantity
from aura.operations import locked_gate
from aura.storage import Base


class FixtureOrder(Base):
    __tablename__ = "fixture_orders"
    __table_args__ = (CheckConstraint("quantity > 0 AND filled >= 0 AND filled <= quantity"),)
    id: Mapped[str] = mapped_column(String, primary_key=True)  # the client order ID
    portfolio_id: Mapped[str] = mapped_column(ForeignKey("portfolios.id"))
    instrument: Mapped[str] = mapped_column(String)
    strategy_scope: Mapped[str] = mapped_column(String)
    side: Mapped[str] = mapped_column(String)
    quantity: Mapped[Decimal] = mapped_column(Numeric(28, 8))
    filled: Mapped[Decimal] = mapped_column(Numeric(28, 8), default=ZERO)
    price_bound: Mapped[Decimal] = mapped_column(Numeric(28, 8))
    fee_budget: Mapped[Decimal] = mapped_column(Numeric(28, 8))
    fees_paid: Mapped[Decimal] = mapped_column(Numeric(28, 8), default=ZERO)
    state: Mapped[str] = mapped_column(String, default="AUTHORIZED")
    gate_version: Mapped[int] = mapped_column(Integer)


def reserve_fixture(
    session: Session,
    *,
    portfolio_id: str,
    order_id: str,
    instrument: str,
    scope: str,
    side: str,
    quantity: Decimal,
    price_bound: Decimal,
    fee_budget: Decimal,
    increment: Decimal,
    fractional_supported: bool,
    policy: str,
) -> FixtureOrder:
    portfolio = fixture_portfolio(session, portfolio_id, policy)
    gate = locked_gate(session)
    quantity, price_bound, fee_budget, increment = map(
        amount, (quantity, price_bound, fee_budget, increment)
    )
    if side not in {"BUY", "SELL"} or min(quantity, price_bound, increment) <= 0 or fee_budget < 0:
        raise ValueError("Invalid long-only fixture order")
    if quantity % increment or (not fractional_supported and quantity % 1):
        raise Conflict("Unsupported fractional quantity or increment")
    existing = session.get(FixtureOrder, order_id)
    if existing:
        if (
            existing.portfolio_id,
            existing.instrument,
            existing.strategy_scope,
            existing.side,
            existing.quantity,
            existing.price_bound,
            existing.fee_budget,
        ) != (portfolio_id, instrument, scope, side, quantity, price_bound, fee_budget):
            raise Conflict("Order identity reused with different intent")
        return existing
    if gate.full_kill or (gate.entry_halt and side == "BUY"):
        raise Conflict("Capability control blocks order")
    if blocking_quarantine(session, portfolio_id):
        raise Conflict("Open execution quarantine refuses new reservations; redrive or reconcile")
    pending = session.scalars(
        select(FixtureOrder).where(
            FixtureOrder.portfolio_id == portfolio_id,
            FixtureOrder.state.not_in(["CANCELED", "FILLED"]),
        )
    ).all()
    if side == "BUY":
        reserved = sum(
            (
                (o.quantity - o.filled) * o.price_bound + o.fee_budget - o.fees_paid
                for o in pending
                if o.side == "BUY"
            ),
            ZERO,
        )
        if (
            quantity * price_bound + fee_budget
            > balances(session, portfolio_id).get("cash", ZERO) - reserved
        ):
            raise Conflict("Insufficient shared cash including pending reservations")
    else:
        held = held_quantity(session, portfolio_id, instrument, scope)
        reserved = sum(
            (
                o.quantity - o.filled
                for o in pending
                if o.side == "SELL" and o.instrument == instrument and o.strategy_scope == scope
            ),
            ZERO,
        )
        if quantity > held - reserved:
            raise Conflict("Exit would oversell owned unreserved strategy lots")
        if quantity * price_bound < fee_budget:
            raise Conflict("Fee budget could require additional cash for exit")
    order = FixtureOrder(
        id=order_id,
        portfolio_id=portfolio_id,
        instrument=instrument,
        strategy_scope=scope,
        side=side,
        quantity=quantity,
        price_bound=price_bound,
        fee_budget=fee_budget,
        filled=ZERO,
        fees_paid=ZERO,
        state="AUTHORIZED",
        gate_version=gate.version,
    )
    session.add(order)
    portfolio.version += 1
    emit(
        session,
        "FIXTURE_ORDER_RESERVED",
        portfolio_id,
        portfolio.version,
        order_id,
        {"order_id": order_id, "environment": "SHADOW"},
    )
    session.flush()
    return order


def admit_fixture(session: Session, order_id: str) -> None:
    gate = locked_gate(session)
    order = session.get(FixtureOrder, order_id)
    if not order:
        raise Conflict("Unknown fixture order")
    fixture_portfolio(session, order.portfolio_id, FIXTURE_POLICY)
    if order.state != "AUTHORIZED":
        raise Conflict("Already attempted; reconcile rather than resubmit")
    if (
        gate.version != order.gate_version
        or gate.full_kill
        or (gate.entry_halt and order.side == "BUY")
    ):
        raise Conflict("Control state changed; new authorization required")
    order.state = "SUBMITTING"


def record_submission(session: Session, order_id: str, state: str) -> None:
    locked_gate(session)
    order = session.get(FixtureOrder, order_id)
    if order and state == "ACKNOWLEDGED" and order.state in {"PARTIALLY_FILLED", "FILLED"}:
        # A fill that arrived first already proved acceptance; never move state backward.
        return
    if not order or order.state not in {"SUBMITTING", "SUBMISSION_UNKNOWN"}:
        raise Conflict("Unexpected submission result")
    if state not in {"ACKNOWLEDGED", "SUBMISSION_UNKNOWN", "CANCELED"}:
        raise ValueError("Unsupported adapter result")
    order.state = state


def cancel_confirmed_fixture(session: Session, order_id: str) -> None:
    locked_gate(session)
    order = session.get(FixtureOrder, order_id)
    if not order or order.state == "FILLED":
        raise Conflict("No cancellable remainder")
    fixture_portfolio(session, order.portfolio_id, FIXTURE_POLICY)
    order.state = "CANCELED"
