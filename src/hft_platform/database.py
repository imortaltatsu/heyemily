from __future__ import annotations

import logging
from collections.abc import AsyncGenerator

from sqlalchemy import text, update
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from hft_platform.config import get_settings

logger = logging.getLogger(__name__)


class Base(DeclarativeBase):
    pass


_settings = get_settings()
engine = create_async_engine(_settings.database_url, echo=False)
async_session_maker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    async with async_session_maker() as session:
        yield session


async def _sqlite_drop_legacy_email_user_tables() -> None:
    """SQLite DBs from the email/password era lack users.wallet_address; drop and recreate on next create_all."""
    if "sqlite" not in _settings.database_url.lower():
        return
    async with engine.begin() as conn:
        r = await conn.execute(text("SELECT name FROM sqlite_master WHERE type='table' AND name='users'"))
        if r.scalar_one_or_none() is None:
            return
        r = await conn.execute(text("PRAGMA table_info(users)"))
        cols = {row[1] for row in r.fetchall()}
        if "wallet_address" in cols:
            return
        logger.warning(
            "SQLite users table is legacy (email/password). Dropping users, bot_sessions, auth_challenges "
            "so wallet-auth schema can be created (old platform rows cleared)."
        )
        await conn.execute(text("PRAGMA foreign_keys=OFF"))
        await conn.execute(text("DROP TABLE IF EXISTS bot_sessions"))
        await conn.execute(text("DROP TABLE IF EXISTS auth_challenges"))
        await conn.execute(text("DROP TABLE IF EXISTS users"))
        await conn.execute(text("PRAGMA foreign_keys=ON"))


async def _sqlite_add_bot_sessions_custodial_column() -> None:
    url = _settings.database_url.lower()
    if "sqlite" not in url:
        return
    async with engine.begin() as conn:
        r = await conn.execute(
            text("SELECT name FROM sqlite_master WHERE type='table' AND name='bot_sessions'")
        )
        if r.scalar_one_or_none() is None:
            return
        r = await conn.execute(text("PRAGMA table_info(bot_sessions)"))
        cols = [row[1] for row in r.fetchall()]
        if "custodial_address" not in cols:
            await conn.execute(
                text("ALTER TABLE bot_sessions ADD COLUMN custodial_address VARCHAR(42)")
            )


async def _sqlite_rebuild_users_if_wallet_column_missing() -> None:
    """Last resort if legacy users survived create_all (e.g. cwd/db path mismatch before config fix)."""
    if "sqlite" not in _settings.database_url.lower():
        return
    try:
        async with engine.begin() as conn:
            await conn.execute(text("SELECT wallet_address FROM users LIMIT 0"))
    except OperationalError as e:
        err = str(e.orig) if getattr(e, "orig", None) else str(e)
        if "wallet_address" not in err:
            raise
        logger.warning(
            "Detected users table without wallet_address after migrations; forcing drop + create_all (%s)",
            err,
        )
        await _sqlite_drop_legacy_email_user_tables()
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        await _sqlite_add_bot_sessions_custodial_column()


async def _sqlite_add_users_trading_wallet_columns() -> None:
    if "sqlite" not in _settings.database_url.lower():
        return
    async with engine.begin() as conn:
        r = await conn.execute(text("SELECT name FROM sqlite_master WHERE type='table' AND name='users'"))
        if r.scalar_one_or_none() is None:
            return
        r = await conn.execute(text("PRAGMA table_info(users)"))
        cols = {row[1] for row in r.fetchall()}
        if "trading_encrypted_private_key" not in cols:
            await conn.execute(text("ALTER TABLE users ADD COLUMN trading_encrypted_private_key TEXT"))
        if "trading_custodial_address" not in cols:
            await conn.execute(text("ALTER TABLE users ADD COLUMN trading_custodial_address VARCHAR(42)"))


async def mirror_user_trading_wallet_to_sessions(
    db: AsyncSession,
    user_id: str,
    custodial_address: str | None,
    encrypted_private_key: str | None,
) -> None:
    """Keep bot_sessions rows in sync so existing code paths see one wallet per account."""
    from hft_platform.models import BotSession

    await db.execute(
        update(BotSession)
        .where(BotSession.user_id == user_id)
        .values(
            custodial_address=custodial_address,
            encrypted_private_key=encrypted_private_key,
        )
    )


async def backfill_user_trading_wallet_from_sessions() -> None:
    """Promote the first session wallet on each user to user-level (one HL key per account)."""
    from sqlalchemy import select

    from hft_platform.models import BotSession, User

    async with async_session_maker() as db:
        r = await db.execute(select(User))
        for u in r.scalars().all():
            if u.trading_encrypted_private_key:
                continue
            r2 = await db.execute(
                select(BotSession)
                .where(BotSession.user_id == u.id, BotSession.encrypted_private_key.is_not(None))
                .order_by(BotSession.created_at.asc())
                .limit(1)
            )
            s = r2.scalar_one_or_none()
            if not s or not s.encrypted_private_key:
                continue
            u.trading_encrypted_private_key = s.encrypted_private_key
            u.trading_custodial_address = (s.custodial_address or "").lower() or None
            await mirror_user_trading_wallet_to_sessions(
                db,
                u.id,
                u.trading_custodial_address,
                u.trading_encrypted_private_key,
            )
        await db.commit()


async def backfill_custodial_addresses_from_encrypted_keys() -> None:
    """Ensure bot_sessions.custodial_address is set whenever an encrypted key exists (legacy rows, DB-only truth)."""
    from eth_account import Account
    from sqlalchemy import or_, select

    from hft_platform.models import BotSession
    from hft_platform.vault import get_vault

    v = get_vault(_settings.master_encryption_key)
    updated = 0
    async with async_session_maker() as db:
        r = await db.execute(
            select(BotSession).where(
                BotSession.encrypted_private_key.is_not(None),
                BotSession.encrypted_private_key != "",
                or_(BotSession.custodial_address.is_(None), BotSession.custodial_address == ""),
            )
        )
        for s in r.scalars().all():
            raw = (s.encrypted_private_key or "").strip()
            if not raw:
                continue
            try:
                pk = v.decrypt(raw).strip()
            except Exception as e:
                logger.warning("Custodial backfill skipped session %s: decrypt failed (%s)", s.id, e)
                continue
            try:
                s.custodial_address = Account.from_key(pk).address.lower()
            except Exception as e:
                logger.warning("Custodial backfill skipped session %s: invalid key material (%s)", s.id, e)
                continue
            updated += 1
        if updated:
            await db.commit()
            logger.info("Backfilled custodial_address on %d bot session(s) from encrypted keys", updated)


async def init_db() -> None:
    import hft_platform.models  # noqa: F401 — register ORM tables on Base.metadata before create_all

    await _sqlite_drop_legacy_email_user_tables()
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    await _sqlite_add_bot_sessions_custodial_column()
    await _sqlite_add_users_trading_wallet_columns()
    await _sqlite_rebuild_users_if_wallet_column_missing()
    await backfill_custodial_addresses_from_encrypted_keys()
    await backfill_user_trading_wallet_from_sessions()
