"""Request dependencies shared by the API routers.

create_app() stores one Database, Settings and login-attempt window on app.state;
routers reach them only through these functions, so tests can override them.
"""

from collections import deque
from typing import Annotated

from fastapi import Depends, HTTPException, Request

from aura import auth
from aura.config import Settings
from aura.storage import Database


def database(request: Request) -> Database:
    db: Database = request.app.state.database
    return db


def settings(request: Request) -> Settings:
    configured: Settings = request.app.state.settings
    return configured


def login_attempts(request: Request) -> deque[float]:
    attempts: deque[float] = request.app.state.login_attempts
    return attempts


DatabaseDep = Annotated[Database, Depends(database)]
SettingsDep = Annotated[Settings, Depends(settings)]
LoginAttemptsDep = Annotated[deque[float], Depends(login_attempts)]


def owner(request: Request, db: DatabaseDep) -> None:
    """Require a valid owner session. Every route except health and sign-in declares it."""
    with db.transaction() as session:
        if not auth.authorized(session, request.cookies.get("aura_session")):
            raise HTTPException(401, "Owner sign-in required")
