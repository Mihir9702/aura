"""Canonical journals and isolated M1 fixture accounting.

The Ledger books execution receipts through apply_fill, keyed by receipt: a receipt's journal
source is "receipt:<receipt id>", unique in PostgreSQL, so no receipt posts twice. Fixture
orders, reservations and receipts belong to aura.execution; the old names stay importable
from here. The fixture path rejects ACTIVE_PAPER portfolios. FIFO, fee-expensing and
immediate settlement are explicit test assumptions, not production defaults.
"""

from decimal import Decimal
from typing import Any
from uuid import uuid4

from sqlalchemy import CheckConstraint, ForeignKey, Integer, Numeric, String, select
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, Session, mapped_column

from aura.common import Conflict, amount
from aura.events import emit
from aura.operations import locked_gate
from aura.storage import Base

ZERO = Decimal("0")
EIGHT_PLACES = Decimal("0.00000001")
FIXTURE_POLICY = "FIFO_FEE_EXPENSE_IMMEDIATE_V1"
RECEIPT_SOURCE = "receipt:"


class LedgerRejected(Conflict):
    """Booking a fill would break a Ledger invariant. Nothing was written."""


class Portfolio(Base):
    __tablename__ = "portfolios"
    __table_args__ = (CheckConstraint("environment IN ('ACTIVE_PAPER', 'SHADOW')"),)
    id: Mapped[str] = mapped_column(String, primary_key=True)
    environment: Mapped[str] = mapped_column(String)
    version: Mapped[int] = mapped_column(Integer, default=1)
    capital: Mapped[Decimal] = mapped_column(Numeric(28, 8))


class Journal(Base):
    __tablename__ = "journals"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    portfolio_id: Mapped[str] = mapped_column(ForeignKey("portfolios.id"))
    source_key: Mapped[str] = mapped_column(String, unique=True)
    facts: Mapped[dict[str, Any]] = mapped_column(JSONB)


class Posting(Base):
    __tablename__ = "postings"
    __table_args__ = (CheckConstraint("amount <> 0"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    journal_id: Mapped[str] = mapped_column(ForeignKey("journals.id"))
    account: Mapped[str] = mapped_column(String)
    amount: Mapped[Decimal] = mapped_column(Numeric(28, 8))  # debit positive / credit negative


class Lot(Base):
    __tablename__ = "lots"
    __table_args__ = (CheckConstraint("quantity >= 0 AND basis >= 0"),)
    id: Mapped[str] = mapped_column(String, primary_key=True)
    ordinal: Mapped[int] = mapped_column(Integer)
    portfolio_id: Mapped[str] = mapped_column(ForeignKey("portfolios.id"))
    instrument: Mapped[str] = mapped_column(String)
    strategy_scope: Mapped[str] = mapped_column(String)
    quantity: Mapped[Decimal] = mapped_column(Numeric(28, 8))
    basis: Mapped[Decimal] = mapped_column(Numeric(28, 8))


def balances(session: Session, portfolio_id: str) -> dict[str, Decimal]:
    totals: dict[str, Decimal] = {}
    rows = session.execute(
        select(Posting.account, Posting.amount)
        .join(Journal)
        .where(Journal.portfolio_id == portfolio_id)
    ).all()
    for account, value in rows:
        totals[account] = totals.get(account, ZERO) + value
    return totals


def held_quantity(session: Session, portfolio_id: str, instrument: str, scope: str) -> Decimal:
    """Quantity held in a portfolio's lots for one instrument and strategy scope."""
    return sum(
        session.scalars(
            select(Lot.quantity).where(
                Lot.portfolio_id == portfolio_id,
                Lot.instrument == instrument,
                Lot.strategy_scope == scope,
            )
        ),
        ZERO,
    )


def post(
    session: Session,
    portfolio: Portfolio,
    source: str,
    facts: dict[str, Any],
    entries: dict[str, Decimal],
) -> str:
    entries = {key: amount(value) for key, value in entries.items() if value != ZERO}
    if sum(entries.values(), ZERO) != ZERO or not entries:
        raise ValueError("Unbalanced or empty journal")
    journal_id = str(uuid4())
    session.add(Journal(id=journal_id, portfolio_id=portfolio.id, source_key=source, facts=facts))
    session.flush()
    for account, value in entries.items():
        session.add(Posting(journal_id=journal_id, account=account, amount=value))
    portfolio.version += 1
    emit(
        session,
        "LEDGER_POSTED",
        portfolio.id,
        portfolio.version,
        source,
        {"journal_id": journal_id, "source": source},
    )
    session.flush()
    return journal_id


def fund(
    session: Session,
    portfolio_id: str,
    capital: Decimal = Decimal("500.00"),
    environment: str = "ACTIVE_PAPER",
) -> Portfolio:
    locked_gate(session)
    capital = amount(capital)
    if capital <= 0 or environment not in {"ACTIVE_PAPER", "SHADOW"}:
        raise ValueError("Invalid challenge funding")
    if environment == "ACTIVE_PAPER" and (capital != Decimal("500") or portfolio_id != "challenge"):
        raise ValueError("Initial active challenge funding is exactly 500.00 USD")
    existing = session.get(Portfolio, portfolio_id)
    if existing:
        if existing.capital != capital or existing.environment != environment:
            raise Conflict("Funding identity already used with different capital/environment")
        return existing
    portfolio = Portfolio(id=portfolio_id, capital=capital, environment=environment, version=1)
    session.add(portfolio)
    session.flush()
    post(
        session,
        portfolio,
        f"fund:{portfolio_id}",
        {"kind": "FUNDING", "capital": str(capital)},
        {"cash": capital, "capital": -capital},
    )
    return portfolio


def fixture_portfolio(session: Session, portfolio_id: str, policy: str) -> Portfolio:
    locked_gate(session)
    portfolio = session.get(Portfolio, portfolio_id)
    if not portfolio or portfolio.environment != "SHADOW" or policy != FIXTURE_POLICY:
        raise Conflict(
            "Fixture accounting requires an isolated SHADOW portfolio and explicit policy"
        )
    return portfolio


def apply_fill(
    session: Session,
    *,
    receipt_id: str,
    portfolio_id: str,
    policy: str,
    instrument: str,
    scope: str,
    side: str,
    quantity: Decimal,
    price: Decimal,
    fee: Decimal,
    facts: dict[str, Any],
) -> str:
    """Book one execution receipt and return its journal ID.

    Every check precedes every write, so LedgerRejected leaves nothing behind. A receipt that
    was already posted raises Conflict: callers apply a receipt only while it is unapplied, and
    the unique journal source makes a second posting impossible. This is a receipt, not a
    submission, so Entry Halt and Full Kill do not apply.
    """
    portfolio = fixture_portfolio(session, portfolio_id, policy)
    source = RECEIPT_SOURCE + receipt_id
    if session.scalars(select(Journal.id).where(Journal.source_key == source)).first():
        raise Conflict("Receipt already posted; reconciliation required")
    quantity, price, fee = map(amount, (quantity, price, fee))
    if side not in {"BUY", "SELL"} or min(quantity, price) <= 0 or fee < 0:
        raise LedgerRejected("A fill needs a side, positive quantity and price, nonnegative fee")
    # Stored decimals carry scale 8, so compare the exact product with its 8-place form.
    product = quantity * price
    try:
        value = amount(product.quantize(EIGHT_PLACES))
    except (ArithmeticError, ValueError) as unsupported:
        raise LedgerRejected("Fill value exceeds supported decimal precision") from unsupported
    if value != product:
        raise LedgerRejected("Fill value exceeds supported decimal precision")
    cash = balances(session, portfolio.id).get("cash", ZERO)
    if side == "BUY":
        if value + fee > cash:
            raise LedgerRejected("Fill would create negative cash")
        session.add(
            Lot(
                id=receipt_id,
                ordinal=portfolio.version,
                portfolio_id=portfolio.id,
                instrument=instrument,
                strategy_scope=scope,
                quantity=quantity,
                basis=value,
            )
        )
        entries = {"cash": -(value + fee), "security_cost": value, "fees": fee}
    else:
        lots = session.scalars(
            select(Lot)
            .where(
                Lot.portfolio_id == portfolio.id,
                Lot.instrument == instrument,
                Lot.strategy_scope == scope,
                Lot.quantity > 0,
            )
            .order_by(Lot.ordinal, Lot.id)
        ).all()
        if sum((lot.quantity for lot in lots), ZERO) < quantity:
            raise LedgerRejected("Fill would oversell holdings")
        if cash + value - fee < 0:
            raise LedgerRejected("Fill would create negative cash")
        remaining, basis = quantity, ZERO
        for lot in lots:
            take = min(remaining, lot.quantity)
            removed_basis = (
                lot.basis
                if take == lot.quantity
                else (lot.basis * take / lot.quantity).quantize(EIGHT_PLACES)
            )
            lot.quantity -= take
            lot.basis -= removed_basis
            basis += removed_basis
            remaining -= take
            if remaining == 0:
                break
        entries = {
            "cash": value - fee,
            "security_cost": -basis,
            "realized_gain": -(value - basis),
            "fees": fee,
        }
    booked = {
        "kind": "FILL",
        **facts,
        "receipt_id": receipt_id,
        "side": side,
        "quantity": format(quantity, "f"),
        "price": format(price, "f"),
        "fee": format(fee, "f"),
        "policy": policy,
    }
    return post(session, portfolio, source, booked, entries)


def snapshot(session: Session, portfolio_id: str) -> dict[str, Any]:
    # The same gate provides a coherent journal/lot/reservation read under READ COMMITTED.
    locked_gate(session)
    portfolio = session.get(Portfolio, portfolio_id)
    if not portfolio:
        raise Conflict("Portfolio not initialized; run migrations and initialization")
    accounts = balances(session, portfolio_id)
    lots = session.scalars(
        select(Lot).where(Lot.portfolio_id == portfolio_id, Lot.quantity > 0)
    ).all()
    return {
        "id": portfolio.id,
        "version": portfolio.version,
        "currency": "USD",
        "environment": portfolio.environment,
        "capital": str(portfolio.capital),
        "cash": str(accounts.get("cash", ZERO)),
        "realized_pnl": str(-accounts.get("realized_gain", ZERO)),
        "fees": str(accounts.get("fees", ZERO)),
        "valuation_status": "NO_MARKS" if lots else "CASH_ONLY",
        "equity": None if lots else str(accounts.get("cash", ZERO)),
        "positions": [
            {
                "instrument": lot.instrument,
                "scope": lot.strategy_scope,
                "quantity": str(lot.quantity),
                "cost_basis": str(lot.basis),
            }
            for lot in lots
        ],
    }


# Fixture orders, reservations and fills moved to aura.execution (S05). Resolving them lazily
# keeps `from aura.ledger import FixtureOrder` working without an import cycle.
_MOVED_TO_EXECUTION = frozenset({"FixtureOrder", "reserve_fixture", "apply_fixture_fill"})


def __getattr__(name: str) -> Any:
    if name in _MOVED_TO_EXECUTION:
        from aura import execution

        return getattr(execution, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
