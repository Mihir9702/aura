"""Owner API for strategy scopes: reads, audit history, and audited Suspend/Resume commands."""

import os
from collections.abc import Iterator
from contextlib import contextmanager
from uuid import uuid4

import pytest
from aura.api import create_app
from aura.config import Settings
from aura.events import Event
from aura.strategies import SEEDED_SCOPE_IDS, STATUS_CHANGED
from fastapi.testclient import TestClient
from sqlalchemy import func, select

pytestmark = pytest.mark.integration

COMMAND = {"X-Aura-Command": "1"}
BASE = "/api/strategies/scopes"
MOMENTUM = SEEDED_SCOPE_IDS[0]


@contextmanager
def api(*, signed_in: bool = True) -> Iterator[TestClient]:
    settings = Settings(database_url=os.environ["AURA_TEST_DATABASE_URL"], owner_key="x" * 40)
    with TestClient(create_app(settings)) as client:
        if signed_in:
            login = client.post("/api/session", json={"key": "x" * 40}, headers=COMMAND)
            assert login.status_code == 200
        yield client


def body(version: int, *, command_id: str | None = None, reason: str = "owner review") -> dict:
    return {
        "command_id": command_id or str(uuid4()),
        "expected_version": version,
        "reason": reason,
    }


def status_event_count(db) -> int:
    with db.transaction() as s:
        return s.scalar(
            select(func.count()).select_from(Event).where(Event.event_type == STATUS_CHANGED)
        )


def test_scope_routes_require_the_owner(db):
    with api(signed_in=False) as anonymous:
        for method, path in [
            ("GET", BASE),
            ("GET", f"{BASE}/{MOMENTUM}"),
            ("GET", f"{BASE}/{MOMENTUM}/history"),
            ("POST", f"{BASE}/{MOMENTUM}/suspend"),
            ("POST", f"{BASE}/{MOMENTUM}/resume"),
        ]:
            payload = body(1) if method == "POST" else None
            response = anonymous.request(method, path, headers=COMMAND, json=payload)
            assert response.status_code == 401, (method, path)
    assert status_event_count(db) == 0


def test_scope_reads(db):
    with api() as client:
        listed = client.get(BASE)
        assert listed.status_code == 200
        scopes = listed.json()
        assert [s["id"] for s in scopes] == sorted(SEEDED_SCOPE_IDS)
        momentum = next(s for s in scopes if s["id"] == MOMENTUM)
        assert momentum["pod"] == {
            "id": "momentum",
            "version": 0,
            "name": "MOMENTUM",
            "display_name": "Momentum",
            "implementation_status": "UNIMPLEMENTED",
        }
        assert {
            k: momentum["horizon"][k] for k in ("id", "version", "approval", "decision_ref")
        } == {
            "id": "DAILY_MULTI_SESSION_DEV",
            "version": 1,
            "approval": "UNAPPROVED",
            "decision_ref": "OD-03",
        }
        assert (momentum["state"], momentum["version"], momentum["eligibility_generation"]) == (
            "DEVELOPMENT",
            1,
            1,
        )
        assert client.get(f"{BASE}/{MOMENTUM}").json() == momentum
        assert client.get(f"{BASE}/{MOMENTUM}/history").json() == []
        assert client.get(f"{BASE}/no-such-scope").status_code == 404
        assert client.get(f"{BASE}/no-such-scope/history").status_code == 404


def test_suspend_and_resume_are_audited_and_idempotent(db):
    suspend_body = body(1, reason="data incident")
    with api() as client:
        suspended = client.post(f"{BASE}/{MOMENTUM}/suspend", json=suspend_body, headers=COMMAND)
        assert suspended.status_code == 200
        assert suspended.json() == {
            "scope_id": MOMENTUM,
            "state": "SUSPENDED",
            "version": 2,
            "eligibility_generation": 2,
        }
        # Replay returns the recorded result and records nothing new.
        replay = client.post(f"{BASE}/{MOMENTUM}/suspend", json=suspend_body, headers=COMMAND)
        assert (replay.status_code, replay.json()) == (200, suspended.json())
        assert status_event_count(db) == 1
        # The same command_id with a different body, command or scope is a conflict.
        for path, reused in [
            (f"{BASE}/{MOMENTUM}/suspend", {**suspend_body, "reason": "another reason"}),
            (f"{BASE}/{MOMENTUM}/suspend", {**suspend_body, "expected_version": 2}),
            (f"{BASE}/{MOMENTUM}/resume", suspend_body),
            (f"{BASE}/{SEEDED_SCOPE_IDS[1]}/suspend", suspend_body),
        ]:
            response = client.post(path, json=reused, headers=COMMAND)
            assert response.status_code == 409, path
            assert response.json()["detail"].startswith("COMMAND_REUSED: ")
        stale = client.post(f"{BASE}/{MOMENTUM}/resume", json=body(1), headers=COMMAND)
        assert stale.status_code == 409
        assert stale.json()["detail"].startswith("VERSION_CONFLICT: ")
        resumed = client.post(f"{BASE}/{MOMENTUM}/resume", json=body(2), headers=COMMAND)
        assert resumed.json() == {
            "scope_id": MOMENTUM,
            "state": "DEVELOPMENT",
            "version": 3,
            "eligibility_generation": 3,
        }
        again = client.post(f"{BASE}/{MOMENTUM}/resume", json=body(3), headers=COMMAND)
        assert again.status_code == 409
        assert again.json()["detail"].startswith("RESUME_REQUIRES_SUSPENDED: ")
        scope = client.get(f"{BASE}/{MOMENTUM}").json()
        assert (scope["state"], scope["previous_state"], scope["actor"]) == (
            "DEVELOPMENT",
            "SUSPENDED",
            "owner",
        )
        history = client.get(f"{BASE}/{MOMENTUM}/history").json()
    assert [
        (h["prior_state"], h["new_state"], h["prior_generation"], h["generation"], h["actor"])
        for h in history
    ] == [
        ("DEVELOPMENT", "SUSPENDED", 1, 2, "owner"),
        ("SUSPENDED", "DEVELOPMENT", 2, 3, "owner"),
    ]
    assert history[0]["command_id"] == suspend_body["command_id"]
    assert history[0]["reason"] == "data incident"
    assert status_event_count(db) == 2


def test_command_validation_and_boundaries(db):
    with api() as client:
        path = f"{BASE}/{MOMENTUM}/suspend"
        for invalid in [
            {**body(1), "target": "ACTIVE_PAPER"},
            body(1, reason="no"),
            body(1, reason="      "),
            body(0),
            {**body(1), "command_id": "not-a-uuid"},
        ]:
            assert client.post(path, json=invalid, headers=COMMAND).status_code == 422
        assert client.post(path, json=body(1)).status_code == 403
        evil = {**COMMAND, "Origin": "https://evil.invalid"}
        assert client.post(path, json=body(1), headers=evil).status_code == 403
        unknown = client.post(f"{BASE}/no-such-scope/suspend", json=body(1), headers=COMMAND)
        assert unknown.status_code == 404
        # There is no route that qualifies or activates a scope.
        for verb in ("qualify", "activate"):
            response = client.post(f"{BASE}/{MOMENTUM}/{verb}", json=body(1), headers=COMMAND)
            assert response.status_code in (404, 405)
    assert status_event_count(db) == 0
