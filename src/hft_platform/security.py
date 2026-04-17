from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from jose import JWTError, jwt

from hft_platform.config import get_settings

_settings = get_settings()


def create_access_token(
    subject: str,
    extra: dict[str, Any] | None = None,
    expires_delta: timedelta | None = None,
) -> str:
    expire = datetime.now(timezone.utc) + (
        expires_delta
        if expires_delta
        else timedelta(minutes=_settings.access_token_expire_minutes)
    )
    to_encode: dict[str, Any] = {"sub": subject, "exp": expire}
    if extra:
        to_encode.update(extra)
    return jwt.encode(to_encode, _settings.jwt_secret, algorithm=_settings.jwt_algorithm)


def decode_token(token: str) -> dict[str, Any]:
    return jwt.decode(token, _settings.jwt_secret, algorithms=[_settings.jwt_algorithm])


def create_worker_token(user_id: str, session_id: str) -> str:
    return create_access_token(
        subject=user_id,
        extra={"scope": "worker", "sid": session_id},
        expires_delta=timedelta(hours=24),
    )


def verify_worker_token(token: str, session_id: str) -> str:
    try:
        payload = decode_token(token)
        if payload.get("scope") != "worker":
            raise JWTError("not worker")
        if payload.get("sid") != session_id:
            raise JWTError("session mismatch")
        sub = payload.get("sub")
        if not sub or not isinstance(sub, str):
            raise JWTError("no sub")
        return sub
    except JWTError as e:
        raise ValueError(str(e)) from e
