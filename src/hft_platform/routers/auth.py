from __future__ import annotations

from datetime import datetime, timezone
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from hft_platform.database import get_db
from hft_platform.deps import get_current_user
from hft_platform.models import AuthChallenge, User
from hft_platform.schemas import ChallengeOut, Token, UserOut, WalletChallengeIn, WalletVerifyIn
from hft_platform.security import create_access_token
from hft_platform.wallet_auth import (
    build_login_message,
    challenge_ttl,
    new_nonce_hex,
    normalize_wallet_address,
    verify_wallet_signature,
)

router = APIRouter(prefix="/auth", tags=["auth"])


@router.post("/challenge", response_model=ChallengeOut)
async def wallet_challenge(
    body: WalletChallengeIn,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> ChallengeOut:
    try:
        w = normalize_wallet_address(body.address)
    except ValueError:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid address")
    nonce = new_nonce_hex()
    message = build_login_message(w, nonce)
    await db.execute(delete(AuthChallenge).where(AuthChallenge.wallet_address == w))
    db.add(AuthChallenge(wallet_address=w, message=message, expires_at=challenge_ttl()))
    await db.commit()
    return ChallengeOut(message=message)


@router.post("/verify", response_model=Token)
async def wallet_verify(
    body: WalletVerifyIn,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> Token:
    try:
        w = normalize_wallet_address(body.address)
    except ValueError:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid address")
    r = await db.execute(
        select(AuthChallenge).where(
            AuthChallenge.message == body.message,
            AuthChallenge.wallet_address == w,
        )
    )
    ch = r.scalar_one_or_none()
    if not ch:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Unknown challenge; request a new one",
        )
    now = datetime.now(timezone.utc)
    exp = ch.expires_at
    if exp.tzinfo is None:
        exp = exp.replace(tzinfo=timezone.utc)
    else:
        exp = exp.astimezone(timezone.utc)
    if exp < now:
        await db.delete(ch)
        await db.commit()
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Challenge expired",
        )
    if not verify_wallet_signature(w, body.message, body.signature):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid signature",
        )
    await db.delete(ch)
    r2 = await db.execute(select(User).where(User.wallet_address == w))
    user = r2.scalar_one_or_none()
    if not user:
        user = User(wallet_address=w)
        db.add(user)
    await db.commit()
    await db.refresh(user)
    token = create_access_token(subject=user.id)
    return Token(access_token=token)


@router.get("/me", response_model=UserOut)
async def me(user: Annotated[User, Depends(get_current_user)]) -> User:
    return user
