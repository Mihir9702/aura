import secrets
import time

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, SecretStr

from aura import auth
from aura.routes.dependencies import DatabaseDep, LoginAttemptsDep, SettingsDep, owner

router = APIRouter()


class Login(BaseModel):
    model_config = ConfigDict(extra="forbid")
    key: SecretStr


@router.post("/api/session")
def login(
    body: Login,
    response: Response,
    db: DatabaseDep,
    settings: SettingsDep,
    attempts: LoginAttemptsDep,
) -> dict[str, bool]:
    current = time.monotonic()
    while attempts and attempts[0] < current - 60:
        attempts.popleft()
    if len(attempts) >= 5:
        raise HTTPException(429, "Too many attempts; wait one minute")
    attempts.append(current)
    if not secrets.compare_digest(
        body.key.get_secret_value(), settings.owner_key.get_secret_value()
    ):
        raise HTTPException(401, "Invalid owner key")
    with db.transaction() as session:
        token = auth.issue(session)
    response.set_cookie(
        "aura_session",
        token,
        httponly=True,
        samesite="strict",
        max_age=28800,
        secure=False,
        path="/api",
    )  # loopback HTTP only; hosted auth not enabled
    return {"authenticated": True}


@router.delete("/api/session", dependencies=[Depends(owner)])
def logout(request: Request, response: Response, db: DatabaseDep) -> dict[str, bool]:
    with db.transaction() as session:
        record = session.get(auth.OwnerSession, auth.digest(request.cookies["aura_session"]))
        if record:
            session.delete(record)
    response.delete_cookie("aura_session", path="/api")
    return {"authenticated": False}
