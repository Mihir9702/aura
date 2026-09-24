"""Durable outbox acknowledgement worker. No broker/model/scheduler execution.

No high-watermark cursor is used: commit order may differ from sequence order.
Inbox writes and consumption commit together, and locked events prevent duplicate
local acknowledgement across competing workers. The worker also applies committed
SHADOW fixture fill receipts from their events, each exactly once; it never submits.
"""

import time

from sqlalchemy import exists, select
from sqlalchemy.orm import Session

from aura.config import Settings
from aura.events import Event, Inbox
from aura.execution import apply_received_fills
from aura.storage import Database

CONSUMER = "foundation-audit-v1"


def acknowledge_batch(session: Session) -> int:
    processed = exists(
        select(Inbox.id).where(Inbox.event_id == Event.event_id, Inbox.consumer == CONSUMER)
    )
    events = session.scalars(
        select(Event)
        .where(~processed)
        .order_by(Event.sequence)
        .limit(100)
        .with_for_update(skip_locked=True)
    ).all()
    for event in events:
        session.add(Inbox(consumer=CONSUMER, event_id=event.event_id))
    return len(events)


def main() -> None:
    db = Database(Settings().database_url.get_secret_value())  # type: ignore[call-arg]
    try:
        while True:
            with db.transaction() as session:
                acknowledge_batch(session)
            with db.transaction() as session:
                apply_received_fills(session)
            time.sleep(2)
    finally:
        db.engine.dispose()


if __name__ == "__main__":
    main()
