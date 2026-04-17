from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import JSON, DateTime, ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from hft_platform.database import Base


def _uuid() -> str:
    return str(uuid.uuid4())


class User(Base):
    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    wallet_address: Mapped[str] = mapped_column(String(42), unique=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    # One Hyperliquid trading key per login account, shared by every bot session (custodial).
    trading_encrypted_private_key: Mapped[str | None] = mapped_column(Text, nullable=True)
    trading_custodial_address: Mapped[str | None] = mapped_column(String(42), nullable=True, index=True)

    sessions: Mapped[list["BotSession"]] = relationship(back_populates="user")


class AuthChallenge(Base):
    __tablename__ = "auth_challenges"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    wallet_address: Mapped[str] = mapped_column(String(42), index=True)
    message: Mapped[str] = mapped_column(Text, unique=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class BotSession(Base):
    __tablename__ = "bot_sessions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    user_id: Mapped[str] = mapped_column(String(36), ForeignKey("users.id"), index=True)
    name: Mapped[str] = mapped_column(String(128))
    config: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="stopped")  # stopped | running
    encrypted_private_key: Mapped[str | None] = mapped_column(Text, nullable=True)
    custodial_address: Mapped[str | None] = mapped_column(String(42), nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow
    )

    user: Mapped["User"] = relationship(back_populates="sessions")
