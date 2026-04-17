from __future__ import annotations

import logging
from typing import Annotated

from fastapi import APIRouter, Header, HTTPException

from hft_platform.schemas import TelemetryIn
from hft_platform.security import verify_worker_token
from hft_platform.telemetry_hub import hub

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/internal", tags=["internal"])


@router.post("/telemetry")
async def ingest_telemetry(
    body: TelemetryIn,
    authorization: Annotated[str | None, Header()] = None,
) -> dict[str, str]:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Unauthorized")
    token = authorization.replace("Bearer ", "").strip()
    sid = body.session_id
    if not sid:
        raise HTTPException(status_code=400, detail="session_id required")
    try:
        verify_worker_token(token, sid)
    except ValueError as e:
        logger.warning("telemetry ingest auth failed for session %s: %s", sid, e)
        raise HTTPException(status_code=401, detail=str(e)) from e

    await hub.broadcast(
        sid,
        {
            "kind": body.kind,
            "ts": body.ts,
            "symbol": body.symbol,
            "data": body.data,
        },
    )
    return {"ok": "true"}
