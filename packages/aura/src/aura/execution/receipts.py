"""Durable execution receipts and their quarantine, owned by Execution.

A receipt is committed before the Ledger applies it. Its identity is unique per adapter,
account, execution ID and revision. Its facts are immutable, which a database trigger
enforces; only its status advances: RECEIVED -> APPLIED, or RECEIVED -> QUARANTINED ->
APPLIED after a successful redrive. Quarantine entries keep the delivery that caused them.
"""

from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any

from sqlalchemy import (
    DateTime,
    ForeignKey,
    Integer,
    Numeric,
    String,
    UniqueConstraint,
    func,
    or_,
    select,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, Session, mapped_column

from aura.common import now
from aura.storage import Base


class Reason(StrEnum):
    """Why a receipt was quarantined instead of applied."""

    CONFLICTING_DUPLICATE = "CONFLICTING_DUPLICATE"  # same identity, different facts
    UNSUPPORTED_REVISION = "UNSUPPORTED_REVISION"  # a correction; needs OD-11 policy
    UNKNOWN_ACCOUNT = "UNKNOWN_ACCOUNT"  # not the fixture adapter account
    ORPHAN = "ORPHAN"  # no order with this client order ID, yet
    OUT_OF_BOUNDS = "OUT_OF_BOUNDS"  # outside the order's quantity, price or fee bounds
    LEDGER_REJECTED = "LEDGER_REJECTED"  # would create negative cash or oversell lots


class ExecutionReceipt(Base):
    __tablename__ = "execution_receipts"
    __table_args__ = (
        UniqueConstraint(
            "adapter_id",
            "adapter_account_id",
            "execution_id",
            "revision",
            name="execution_receipts_identity",
        ),
    )
    id: Mapped[str] = mapped_column(String, primary_key=True)
    adapter_id: Mapped[str] = mapped_column(String)
    adapter_account_id: Mapped[str] = mapped_column(String)
    execution_id: Mapped[str] = mapped_column(String)
    revision: Mapped[int] = mapped_column(Integer)
    order_id: Mapped[str] = mapped_column(String)  # client order ID as reported
    quantity: Mapped[Decimal] = mapped_column(Numeric(28, 8))
    price: Mapped[Decimal] = mapped_column(Numeric(28, 8))
    fee: Mapped[Decimal] = mapped_column(Numeric(28, 8))
    policy: Mapped[str] = mapped_column(String)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    status: Mapped[str] = mapped_column(String, default="RECEIVED")
    journal_id: Mapped[str | None] = mapped_column(ForeignKey("journals.id"))


class QuarantineEntry(Base):
    __tablename__ = "execution_quarantine"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    receipt_id: Mapped[str] = mapped_column(ForeignKey("execution_receipts.id"))
    portfolio_id: Mapped[str | None] = mapped_column(ForeignKey("portfolios.id"))
    reason: Mapped[str] = mapped_column(String)
    detail: Mapped[str] = mapped_column(String)
    delivered: Mapped[dict[str, Any]] = mapped_column(JSONB)
    status: Mapped[str] = mapped_column(String, default="OPEN")
    version: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


def blocking_quarantine(session: Session, portfolio_id: str) -> int:
    """Count open entries that refuse new reservations for a portfolio.

    An entry without a portfolio (an orphan or unknown-account receipt) cannot be attributed,
    so it blocks every portfolio until it is redriven or reconciled.
    """
    count = session.scalar(
        select(func.count())
        .select_from(QuarantineEntry)
        .where(
            QuarantineEntry.status == "OPEN",
            or_(
                QuarantineEntry.portfolio_id == portfolio_id,
                QuarantineEntry.portfolio_id.is_(None),
            ),
        )
    )
    return int(count or 0)


def open_quarantine(session: Session) -> list[dict[str, Any]]:
    """Up to 100 open entries with their receipt identity, newest first."""
    rows = session.execute(
        select(QuarantineEntry, ExecutionReceipt)
        .join(ExecutionReceipt, QuarantineEntry.receipt_id == ExecutionReceipt.id)
        .where(QuarantineEntry.status == "OPEN")
        .order_by(QuarantineEntry.created_at.desc(), QuarantineEntry.id)
        .limit(100)
    ).all()
    return [
        {
            "id": entry.id,
            "receipt_id": receipt.id,
            "portfolio_id": entry.portfolio_id,
            "reason": entry.reason,
            "detail": entry.detail,
            "version": entry.version,
            "created_at": entry.created_at.isoformat(),
            "adapter_id": receipt.adapter_id,
            "adapter_account_id": receipt.adapter_account_id,
            "execution_id": receipt.execution_id,
            "revision": receipt.revision,
            "order_id": receipt.order_id,
        }
        for entry, receipt in rows
    ]
