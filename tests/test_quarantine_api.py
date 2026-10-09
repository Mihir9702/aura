"""Owner quarantine routes: list open entries, redrive with command_id and expected_version."""

import os
from collections.abc import Iterator
from contextlib import contextmanager
from decimal import Decimal as D
from uuid import uuid4

import pytest
from aura.api import create_app
from aura.config import Settings
from aura.events import Event
from aura.execution import (
    FIXTURE_ACCOUNT_ID,
    FIXTURE_ADAPTER_ID,
    FixtureFill,
    FixtureOrder,
    receive_fixture_fill,
)
from aura.ledger import FIXTURE_POLICY
from fastapi.testclient import TestClient
from sqlalchemy import select

pytestmark = pytest.mark.integration

COMMAND = {"X-Aura-Command": "1"}
QUARANTINE = "/api/execution/quarantine"
RECORD_FIELDS = [
    "id",
    "receipt_id",
    "portfolio_id",
    "reason",
    "detail",
    "version",
    "created_at",
    "adapter_id",
    "adapter_account_id",
    "execution_id",
    "revision",
    "order_id",
]


def redrive_path(entry: str) -> str:
    return f"{QUARANTINE}/{entry}/redrive"


@contextmanager
def api_client(signed_in: bool = True) -> Iterator[TestClient]:
    settings = Settings(database_url=os.environ["AURA_TEST_DATABASE_URL"], owner_key="x" * 40)
    with TestClient(create_app(settings)) as client:
        if signed_in:
            response = client.post("/api/session", json={"key": "x" * 40}, headers=COMMAND)
            assert response.status_code == 200
        yield client


def quarantined_orphan(db):
    fill = FixtureFill(
        adapter_id=FIXTURE_ADAPTER_ID,
        adapter_account_id=FIXTURE_ACCOUNT_ID,
        execution_id="e1",
        revision=1,
        order_id="late",
        quantity=D("1"),
        price=D("100"),
        fee=D("0.25"),
        policy=FIXTURE_POLICY,
    )
    outcome = receive_fixture_fill(db, fill)
    assert (outcome.status, outcome.reason) == ("QUARANTINED", "ORPHAN")
    return outcome


def restore_order(db):
    """The order record the orphan named becomes known (restore or reconciliation)."""
    with db.transaction() as s:
        s.add(
            FixtureOrder(
                id="late",
                portfolio_id="fixture",
                instrument="TEST",
                strategy_scope="momentum:daily",
                side="BUY",
                quantity=D("2.5"),
                price_bound=D("100"),
                fee_budget=D("0.50"),
                filled=D(0),
                fees_paid=D(0),
                state="ACKNOWLEDGED",
                gate_version=1,
            )
        )


def test_quarantine_routes_require_the_owner_and_command_boundary(db):
    entry = quarantined_orphan(db).quarantine_id
    body = {"expected_version": 1, "reason": "order restored", "command_id": str(uuid4())}
    with api_client(signed_in=False) as client:
        assert client.get(QUARANTINE).status_code == 401
        assert client.post(redrive_path(entry), json=body, headers=COMMAND).status_code == 401
    with api_client() as client:
        assert client.post(redrive_path(entry), json=body).status_code == 403
        foreign = {**COMMAND, "Origin": "https://evil.invalid"}
        assert client.post(redrive_path(entry), json=body, headers=foreign).status_code == 403
        extra = {**body, "force": True}
        assert client.post(redrive_path(entry), json=extra, headers=COMMAND).status_code == 422
        assert client.post("/api/orders", headers=COMMAND).status_code == 404
        assert client.get(QUARANTINE).json()[0]["version"] == 1  # nothing was attempted


def test_owner_redrive_is_versioned_idempotent_and_audited(db):
    orphan = quarantined_orphan(db)
    entry = orphan.quarantine_id
    first = {"expected_version": 1, "reason": "order not restored yet", "command_id": str(uuid4())}
    with api_client() as client:
        (listed,) = client.get(QUARANTINE).json()
        assert list(listed) == RECORD_FIELDS
        assert (listed["id"], listed["version"], listed["reason"]) == (entry, 1, "ORPHAN")
        assert (listed["portfolio_id"], listed["order_id"], listed["revision"]) == (None, "late", 1)
        unknown = {**first, "command_id": str(uuid4())}
        assert (
            client.post(redrive_path("missing"), json=unknown, headers=COMMAND).status_code == 404
        )
        still = client.post(redrive_path(entry), json=first, headers=COMMAND)
        assert still.status_code == 200
        assert still.json() == {
            "quarantine_id": entry,
            "receipt_id": orphan.receipt_id,
            "outcome": "STILL_QUARANTINED",
            "status": "OPEN",
            "version": 2,
            "reason": "ORPHAN",
            "journal_id": None,
        }
        # Repeating the command returns its recorded outcome without another attempt.
        assert client.post(redrive_path(entry), json=first, headers=COMMAND).json() == still.json()
        # Reusing the identity with other content, or a stale version, conflicts.
        changed = {**first, "reason": "something else"}
        assert client.post(redrive_path(entry), json=changed, headers=COMMAND).status_code == 409
        stale = {**first, "command_id": str(uuid4())}
        assert client.post(redrive_path(entry), json=stale, headers=COMMAND).status_code == 409
        restore_order(db)
        second = {
            "expected_version": 2,
            "reason": "order record restored",
            "command_id": str(uuid4()),
        }
        applied = client.post(redrive_path(entry), json=second, headers=COMMAND)
        assert applied.status_code == 200
        result = applied.json()
        assert (result["outcome"], result["status"], result["version"]) == (
            "APPLIED",
            "RESOLVED",
            3,
        )
        assert result["reason"] is None and result["journal_id"]
        assert client.post(redrive_path(entry), json=second, headers=COMMAND).json() == result
        assert client.get(QUARANTINE).json() == []
    with db.transaction() as s:
        audit = s.scalars(
            select(Event)
            .where(Event.event_type == "FIXTURE_FILL_REDRIVEN")
            .order_by(Event.sequence)
        ).all()
        assert [e.correlation_id for e in audit] == [first["command_id"], second["command_id"]]
        assert [e.payload["outcome"] for e in audit] == ["STILL_QUARANTINED", "APPLIED"]
        assert {e.payload["actor"] for e in audit} == {"owner"}
        assert s.get(FixtureOrder, "late").filled == 1
