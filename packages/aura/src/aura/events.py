from datetime import datetime
from typing import Any
from uuid import uuid4

from sqlalchemy import BigInteger, DateTime, Integer, String, UniqueConstraint, select
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, Session, mapped_column

from aura.common import now
from aura.storage import Base


class Event(Base):
    __tablename__ = "outbox"
    sequence: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    event_id: Mapped[str] = mapped_column(String, unique=True, default=lambda: str(uuid4()))
    event_type: Mapped[str] = mapped_column(String)
    aggregate_id: Mapped[str] = mapped_column(String)
    aggregate_version: Mapped[int] = mapped_column(Integer)
    correlation_id: Mapped[str] = mapped_column(String)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB)
    recorded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    schema_version: Mapped[int] = mapped_column(Integer, default=1)


class Inbox(Base):
    __tablename__ = "inbox"
    __table_args__ = (UniqueConstraint("consumer", "event_id"),)
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    consumer: Mapped[str] = mapped_column(String)
    event_id: Mapped[str] = mapped_column(String)
    processed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


def emit(
    session: Session,
    kind: str,
    aggregate: str,
    version: int,
    correlation: str,
    payload: dict[str, Any],
) -> None:
    session.add(
        Event(
            event_type=kind,
            aggregate_id=aggregate,
            aggregate_version=version,
            correlation_id=correlation,
            payload=payload,
        )
    )


def recent(session: Session, after: int = 0) -> list[dict[str, Any]]:
    """Up to 100 events with sequence above `after`, newest (highest sequence) first.

    A display feed, not a delivery cursor: commit order can differ from sequence order,
    so consumers track processed events in the inbox instead of paging by sequence.
    """
    rows = session.scalars(
        select(Event).where(Event.sequence > after).order_by(Event.sequence.desc()).limit(100)
    ).all()
    return [
        {
            "sequence": e.sequence,
            "event_id": e.event_id,
            "event_type": e.event_type,
            "aggregate_id": e.aggregate_id,
            "aggregate_version": e.aggregate_version,
            "correlation_id": e.correlation_id,
            "payload": e.payload,
            "recorded_at": e.recorded_at.isoformat(),
            "schema_version": e.schema_version,
        }
        for e in rows
    ]
