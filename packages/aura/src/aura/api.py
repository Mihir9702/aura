from collections import deque
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from sqlalchemy.exc import SQLAlchemyError
from starlette.middleware.trustedhost import TrustedHostMiddleware

from aura.common import Conflict
from aura.config import Settings
from aura.routes import ROUTERS
from aura.storage import Database


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings()  # type: ignore[call-arg]
    db = Database(settings.database_url.get_secret_value())

    @asynccontextmanager
    async def lifespan(app: FastAPI):  # type: ignore[no-untyped-def]
        yield
        db.engine.dispose()

    app = FastAPI(
        title="Aura local foundation",
        version="0.0.1",
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    # Routers read these through aura.routes.dependencies.
    app.state.database = db
    app.state.settings = settings
    attempts: deque[float] = deque()
    app.state.login_attempts = attempts
    app.add_middleware(
        TrustedHostMiddleware, allowed_hosts=["localhost", "127.0.0.1", "testserver"]
    )

    @app.middleware("http")
    async def browser_boundary(request: Request, call_next):  # type: ignore[no-untyped-def]
        if request.method not in {"GET", "HEAD", "OPTIONS"}:
            # Same-origin browser commands, no wildcard CORS or credentials in URLs.
            origin = request.headers.get("origin")
            if origin and origin not in {
                "http://127.0.0.1:5173",
                "http://localhost:5173",
                "http://127.0.0.1:8000",
                "http://localhost:8000",
            }:
                return JSONResponse({"detail": "Origin denied"}, status_code=403)
            if request.headers.get("x-aura-command") != "1":
                return JSONResponse({"detail": "Command header required"}, status_code=403)
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        return response

    @app.exception_handler(Conflict)
    async def conflict(request: Request, exc: Conflict) -> JSONResponse:
        return JSONResponse({"detail": str(exc)}, status_code=409)

    @app.exception_handler(SQLAlchemyError)
    async def database_failure(request: Request, exc: SQLAlchemyError) -> JSONResponse:
        return JSONResponse(
            {"detail": "Database unavailable; execution remains disabled"}, status_code=503
        )

    for router in ROUTERS:
        app.include_router(router)
    return app
