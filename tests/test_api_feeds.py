import os
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from uuid import UUID

import pytest
from aura.api import create_app
from aura.config import Settings
from aura.events import Event, emit, recent
from fastapi.testclient import TestClient
from sqlalchemy import select

pytestmark = pytest.mark.integration

COMMAND = {"X-Aura-Command": "1"}
EVENT_FIELDS = [
    "sequence",
    "event_id",
    "event_type",
    "aggregate_id",
    "aggregate_version",
    "correlation_id",
    "payload",
    "recorded_at",
    "schema_version",
]


@contextmanager
def owner_client() -> Iterator[TestClient]:
    settings = Settings(database_url=os.environ["AURA_TEST_DATABASE_URL"], owner_key="x" * 40)
    with TestClient(create_app(settings)) as client:
        response = client.post("/api/session", json={"key": "x" * 40}, headers=COMMAND)
        assert response.status_code == 200
        yield client


def test_event_feed_returns_newest_events_newest_first_after_150(db):
    with db.transaction() as s:
        for n in range(150):
            emit(s, "FEED_CHECK", "feed", n + 1, f"feed-{n}", {"n": n})
            s.flush()  # one INSERT per event, so sequence follows emission order
    with db.transaction() as s:
        all_newest_first = s.scalars(select(Event.sequence).order_by(Event.sequence.desc())).all()
        feed = recent(s)
    # The fixture's two funding events are the oldest; 152 events exceed the page of 100.
    assert len(all_newest_first) == 152
    newest_hundred = all_newest_first[:100]
    assert [e["sequence"] for e in feed] == newest_hundred
    assert (feed[0]["payload"], feed[-1]["payload"]) == ({"n": 149}, {"n": 50})

    with owner_client() as client:
        response = client.get("/api/events")
    assert response.status_code == 200
    events = response.json()
    assert [e["sequence"] for e in events] == newest_hundred
    assert events[0]["correlation_id"] == "feed-149"
    assert all(list(e) == EVENT_FIELDS for e in events)
    assert datetime.fromisoformat(events[0]["recorded_at"]).tzinfo is not None


def test_journal_feed_keeps_its_record_shape(db):
    with owner_client() as client:
        response = client.get("/api/journals")
    assert response.status_code == 200
    (journal,) = response.json()
    assert list(journal) == ["id", "source", "facts"]
    UUID(journal["id"])
    assert journal["source"] == "fund:challenge"
    assert journal["facts"] == {"kind": "FUNDING", "capital": "500.00"}
