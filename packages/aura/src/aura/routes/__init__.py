"""HTTP routers of the local owner API, included by aura.api.create_app in this order.

Add a module here for a new resource and append its router to ROUTERS. Every route except
health and sign-in declares the owner dependency (tests/test_route_auth.py enforces it);
commands carry command_id and expected_version and are audited through the outbox.
"""

from fastapi import APIRouter

from aura.routes import (
    controls,
    events,
    health,
    journals,
    overview,
    quarantine,
    session,
    stream,
)

ROUTERS: tuple[APIRouter, ...] = (
    health.router,
    session.router,
    overview.router,
    journals.router,
    events.router,
    controls.router,
    stream.router,
    quarantine.router,
)
