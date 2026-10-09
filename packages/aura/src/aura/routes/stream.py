import asyncio
import json
from collections.abc import AsyncIterator

from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse

from aura import auth
from aura.routes.dependencies import DatabaseDep, owner

router = APIRouter()


@router.get("/api/stream", dependencies=[Depends(owner)])
async def stream(request: Request, db: DatabaseDep) -> StreamingResponse:
    async def generate() -> AsyncIterator[str]:
        # Invalidation stream, not an event-delivery cursor: every tick requires
        # authoritative refetch. This avoids out-of-order commit/sequence gaps.
        while not await request.is_disconnected():
            with db.transaction() as session:
                if not auth.authorized(session, request.cookies.get("aura_session")):
                    yield "event: expired\ndata: {}\n\n"
                    return
            yield "event: refresh\ndata: " + json.dumps({"refresh": True}) + "\n\n"
            await asyncio.sleep(5)

    return StreamingResponse(generate(), media_type="text/event-stream")
