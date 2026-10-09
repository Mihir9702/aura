from aura.api import create_app
from aura.config import Settings
from aura.routes.dependencies import owner
from fastapi.dependencies.models import Dependant
from fastapi.routing import APIRoute, iter_route_contexts

# The only operations reachable without an owner session.
PUBLIC = {("GET", "/api/health"), ("POST", "/api/session")}


def requires_owner(dependant: Dependant) -> bool:
    return any(d.call is owner or requires_owner(d) for d in dependant.dependencies)


def test_every_route_except_health_and_sign_in_requires_owner():
    app = create_app(
        Settings(
            database_url="postgresql+psycopg://schema@127.0.0.1/schema",
            owner_key="schema-generation-only-not-a-real-key",
        )
    )
    try:
        # Effective routes as served, including dependencies added when routers are included.
        served = [
            (method, context.path, requires_owner(context.dependant))
            for context in iter_route_contexts(app.routes)
            if isinstance(context.original_route, APIRoute)
            for method in context.methods
        ]
        assert {(method, path) for method, path, _ in served} >= PUBLIC
        for method, path, protected in served:
            assert protected is not ((method, path) in PUBLIC), (method, path)
    finally:
        app.state.database.engine.dispose()
