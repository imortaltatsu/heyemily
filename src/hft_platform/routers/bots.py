from __future__ import annotations

import asyncio
import logging
import secrets
from typing import Annotated, Any

import httpx
from eth_account import Account
from fastapi import APIRouter, Depends, Header, HTTPException, Request, WebSocket, status
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from hft_platform.config import get_settings
from hft_platform.database import get_db, mirror_user_trading_wallet_to_sessions
from hft_platform.deps import get_current_user
from hft_platform.hl_public import (
    fetch_clearinghouse_state,
    fetch_spot_clearinghouse_state,
    margin_summary_from_clearinghouse,
    spot_balances_rows,
    spot_usdc_available,
)
from hft_platform.models import BotSession, User
from hft_platform.schemas import (
    BotSessionCreate,
    BotSessionOut,
    CredentialIn,
    CustodialWalletOut,
    ExportPrivateKeyOut,
    HyperliquidAccountSnapshot,
    HyperliquidBalanceOut,
    HyperliquidMarginSummary,
    SpotBalanceRow,
    SessionStartOut,
    SessionCloseAllOut,
    SessionDeleteOut,
    SessionStopOut,
    UsdClassTransferIn,
    UsdClassTransferOut,
    WorkerBootstrap,
)
from hft_platform.security import create_worker_token, decode_token, verify_worker_token
from hft_platform.telemetry_hub import hub
from hft_platform.vault import get_vault
from hft_platform.worker_supervisor import spawn_lite_worker, stop_lite_worker

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/bots", tags=["bots"])


def _trading_encrypted_material(user: User, s: BotSession) -> str | None:
    """Prefer user-level custodial key (one HL wallet per login), then session row."""
    v = (user.trading_encrypted_private_key or "").strip() or (s.encrypted_private_key or "").strip()
    return v or None


def _trading_address_resolved(user: User, s: BotSession) -> str | None:
    v = (user.trading_custodial_address or "").strip() or (s.custodial_address or "").strip()
    return v.lower() if v else None


def _sync_usd_class_transfer(private_key: str, testnet: bool, amount: float, to_perp: bool) -> Any:
    from hyperliquid.api import API
    from hyperliquid.utils.constants import MAINNET_API_URL, TESTNET_API_URL
    from hyperliquid.utils.signing import get_timestamp_ms, sign_usd_class_transfer_action

    wallet = Account.from_key(private_key)
    base = TESTNET_API_URL if testnet else MAINNET_API_URL
    timestamp = get_timestamp_ms()
    action = {
        "type": "usdClassTransfer",
        "amount": str(amount),
        "toPerp": to_perp,
        "nonce": timestamp,
    }
    signature = sign_usd_class_transfer_action(wallet, action, not testnet)
    payload = {
        "action": action,
        "nonce": timestamp,
        "signature": signature,
        "vaultAddress": None,
        "expiresAfter": None,
    }
    api = API(base_url=base)
    return api.post("/exchange", payload)


def _as_float(v: Any) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def _sync_close_all_orders_and_positions(private_key: str, testnet: bool) -> dict[str, Any]:
    from hyperliquid.api import API
    from hyperliquid.exchange import Exchange
    from hyperliquid.info import Info
    from hyperliquid.utils.constants import MAINNET_API_URL, TESTNET_API_URL
    from hyperliquid.utils.signing import OrderType as HLOrderType

    wallet = Account.from_key(private_key)
    base = TESTNET_API_URL if testnet else MAINNET_API_URL
    # Match the adapter's safe spotMeta initialization path; some endpoints
    # can return malformed spot indices and crash Info/Exchange init.
    raw_spot_meta = API(base_url=base).post("/info", {"type": "spotMeta"})
    spot_tokens = raw_spot_meta.get("tokens") or []
    safe_universe: list[dict[str, Any]] = []
    for spot_info in raw_spot_meta.get("universe") or []:
        pair = spot_info.get("tokens") or []
        if len(pair) != 2:
            continue
        try:
            base_i = int(pair[0])
            quote_i = int(pair[1])
        except (TypeError, ValueError):
            continue
        if 0 <= base_i < len(spot_tokens) and 0 <= quote_i < len(spot_tokens):
            safe_universe.append(spot_info)
    safe_spot_meta = {"tokens": spot_tokens, "universe": safe_universe}

    info = Info(base, skip_ws=True, spot_meta=safe_spot_meta)
    exchange = Exchange(wallet, base, spot_meta=safe_spot_meta)

    cancelled_orders = 0
    failed_order_cancels = 0
    attempted_position_closes = 0
    failed_position_closes = 0
    order_cancel_errors: list[dict[str, Any]] = []
    position_close_errors: list[dict[str, Any]] = []

    open_orders_raw = info.open_orders(wallet.address)
    open_orders = open_orders_raw if isinstance(open_orders_raw, list) else []
    for order in open_orders:
        coin = str(order.get("coin", ""))
        oid_raw = order.get("oid")
        if not coin:
            failed_order_cancels += 1
            order_cancel_errors.append({"order": order, "error": "missing coin"})
            continue
        try:
            oid = int(oid_raw)
            exchange.cancel(name=coin, oid=oid)
            cancelled_orders += 1
        except Exception as e:
            failed_order_cancels += 1
            order_cancel_errors.append(
                {"coin": coin, "oid": oid_raw, "error": str(e)}
            )

    user_state_raw = info.user_state(wallet.address) or {}
    user_state = user_state_raw if isinstance(user_state_raw, dict) else {}
    mids_raw = info.all_mids() or {}
    mids = mids_raw if isinstance(mids_raw, dict) else {}
    for pos in user_state.get("assetPositions") or []:
        position = pos.get("position") if isinstance(pos, dict) else {}
        if not isinstance(position, dict):
            continue
        coin = str(position.get("coin", ""))
        szi = _as_float(position.get("szi"))
        if not coin or abs(szi) <= 1e-12:
            continue

        mark = _as_float(mids.get(coin))
        if mark <= 0:
            failed_position_closes += 1
            position_close_errors.append(
                {"coin": coin, "size": szi, "error": "missing mark/mid price"}
            )
            continue

        attempted_position_closes += 1
        close_is_buy = szi < 0
        close_size = abs(szi)
        ioc_px = round(mark * (1.015 if close_is_buy else 0.985), 8)
        try:
            exchange.order(
                name=coin,
                is_buy=close_is_buy,
                sz=close_size,
                limit_px=ioc_px,
                order_type=HLOrderType({"limit": {"tif": "Ioc"}}),
                reduce_only=True,
            )
        except Exception as e:
            failed_position_closes += 1
            position_close_errors.append(
                {"coin": coin, "size": szi, "price": ioc_px, "error": str(e)}
            )

    return {
        "cancelled_orders": cancelled_orders,
        "failed_order_cancels": failed_order_cancels,
        "attempted_position_closes": attempted_position_closes,
        "failed_position_closes": failed_position_closes,
        "details": {
            "open_orders_seen": len(open_orders),
            "order_cancel_errors": order_cancel_errors,
            "position_close_errors": position_close_errors,
        },
    }


@router.post("/sessions", response_model=BotSessionOut)
async def create_session(
    body: BotSessionCreate,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(get_current_user)],
) -> BotSession:
    # Preserve one custodial trading wallet across all sessions for this login.
    s = BotSession(
        user_id=user.id,
        name=body.name,
        config=body.config,
        status="stopped",
        encrypted_private_key=user.trading_encrypted_private_key,
        custodial_address=(user.trading_custodial_address or None),
    )
    db.add(s)
    await db.commit()
    await db.refresh(s)
    return s


@router.get("/sessions", response_model=list[BotSessionOut])
async def list_sessions(
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(get_current_user)],
) -> list[BotSession]:
    r = await db.execute(select(BotSession).where(BotSession.user_id == user.id))
    return list(r.scalars().all())


@router.post("/sessions/{session_id}/credentials")
async def upload_credentials(
    session_id: str,
    body: CredentialIn,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(get_current_user)],
) -> dict[str, str]:
    settings = get_settings()
    v = get_vault(settings.master_encryption_key)
    r = await db.execute(
        select(BotSession).where(BotSession.id == session_id, BotSession.user_id == user.id)
    )
    s = r.scalar_one_or_none()
    if not s:
        raise HTTPException(status_code=404, detail="Session not found")
    pk_clean = body.private_key.strip()
    try:
        new_addr = Account.from_key(pk_clean).address.lower()
    except Exception as e:
        raise HTTPException(status_code=400, detail="Invalid private key") from e
    if user.trading_custodial_address and user.trading_custodial_address != new_addr:
        raise HTTPException(
            status_code=409,
            detail=(
                f"This account already uses trading address {user.trading_custodial_address}; "
                "paste that wallet’s private key, or keep using the existing custodial key."
            ),
        )
    enc = v.encrypt(pk_clean)
    user.trading_encrypted_private_key = enc
    user.trading_custodial_address = new_addr
    await mirror_user_trading_wallet_to_sessions(db, user.id, new_addr, enc)
    await db.commit()
    return {"status": "stored", "custodial_address": new_addr}


@router.post("/sessions/{session_id}/export-private-key", response_model=ExportPrivateKeyOut)
async def export_private_key(
    session_id: str,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(get_current_user)],
) -> ExportPrivateKeyOut:
    """Explicitly export decrypted trading key for the selected session."""
    r = await db.execute(
        select(BotSession).where(BotSession.id == session_id, BotSession.user_id == user.id)
    )
    s = r.scalar_one_or_none()
    if not s:
        raise HTTPException(status_code=404, detail="Session not found")
    enc = _trading_encrypted_material(user, s)
    if not enc:
        raise HTTPException(status_code=400, detail="No stored trading key for this session")
    addr = _trading_address_resolved(user, s)
    if not addr:
        raise HTTPException(status_code=400, detail="No stored trading address for this session")
    settings = get_settings()
    v = get_vault(settings.master_encryption_key)
    pk = v.decrypt(enc).strip()
    if not pk:
        raise HTTPException(status_code=400, detail="Stored trading key is empty")
    return ExportPrivateKeyOut(private_key=pk, custodial_address=addr, session_id=session_id)


@router.post("/sessions/{session_id}/custodial-wallet", response_model=CustodialWalletOut)
async def spawn_custodial_wallet(
    session_id: str,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(get_current_user)],
) -> CustodialWalletOut:
    """Create or return the account’s single custodial HL wallet (shared by all bot sessions)."""
    settings = get_settings()
    v = get_vault(settings.master_encryption_key)
    r = await db.execute(
        select(BotSession).where(BotSession.id == session_id, BotSession.user_id == user.id)
    )
    s = r.scalar_one_or_none()
    if not s:
        raise HTTPException(status_code=404, detail="Session not found")
    if user.trading_encrypted_private_key and user.trading_custodial_address:
        await mirror_user_trading_wallet_to_sessions(
            db,
            user.id,
            user.trading_custodial_address,
            user.trading_encrypted_private_key,
        )
        await db.commit()
        return CustodialWalletOut(address=user.trading_custodial_address, session_id=session_id)
    if s.encrypted_private_key and not user.trading_encrypted_private_key:
        user.trading_encrypted_private_key = s.encrypted_private_key
        if s.custodial_address:
            user.trading_custodial_address = s.custodial_address.lower()
        else:
            try:
                user.trading_custodial_address = Account.from_key(
                    v.decrypt(s.encrypted_private_key).strip()
                ).address.lower()
            except Exception as e:
                raise HTTPException(
                    status_code=400,
                    detail="Could not derive address from stored session key",
                ) from e
        await mirror_user_trading_wallet_to_sessions(
            db,
            user.id,
            user.trading_custodial_address,
            user.trading_encrypted_private_key,
        )
        await db.commit()
        return CustodialWalletOut(address=user.trading_custodial_address or "", session_id=session_id)
    pk = "0x" + secrets.token_hex(32)
    acct = Account.from_key(pk)
    addr = acct.address.lower()
    enc = v.encrypt(pk)
    user.trading_encrypted_private_key = enc
    user.trading_custodial_address = addr
    await mirror_user_trading_wallet_to_sessions(db, user.id, addr, enc)
    await db.commit()
    return CustodialWalletOut(address=addr, session_id=session_id)


async def _session_hyperliquid_raw_bundle(
    session_id: str,
    db: AsyncSession,
    user: User,
) -> tuple[str, bool, dict, dict]:
    r = await db.execute(
        select(BotSession).where(BotSession.id == session_id, BotSession.user_id == user.id)
    )
    s = r.scalar_one_or_none()
    if not s:
        raise HTTPException(status_code=404, detail="Session not found")
    addr = _trading_address_resolved(user, s)
    if not addr:
        raise HTTPException(
            status_code=400,
            detail="No trading address yet; use Provision custodial wallet or Save encrypted key once per account",
        )
    cfg = dict(s.config or {})
    testnet = bool(cfg.get("testnet", True))
    try:
        perp_raw = await fetch_clearinghouse_state(addr, testnet)
    except httpx.HTTPStatusError as e:
        status_code = e.response.status_code if e.response is not None else None
        body = e.response.text if e.response is not None else ""
        logger.warning(
            "Hyperliquid clearinghouseState failed for %s (testnet=%s status=%s body=%s)",
            addr,
            testnet,
            status_code,
            body[:200],
        )
        if status_code is not None and 400 <= status_code < 500:
            # Treat unknown/new accounts as empty instead of surfacing a hard 502.
            perp_raw = {}
        else:
            raise HTTPException(
                status_code=502,
                detail=f"Hyperliquid perp info failed (status={status_code}): {e!s}",
            ) from e
    except httpx.HTTPError as e:
        logger.warning(
            "Hyperliquid clearinghouseState transport error for %s (testnet=%s): %s",
            addr,
            testnet,
            e,
        )
        # Read-only balance views should degrade gracefully when HL info is flaky.
        perp_raw = {}
    try:
        spot_raw = await fetch_spot_clearinghouse_state(addr, testnet)
    except httpx.HTTPError as e:
        logger.warning("Hyperliquid spotClearinghouseState failed for %s: %s", addr, e)
        spot_raw = {}
    return addr, testnet, perp_raw, spot_raw


@router.get("/sessions/{session_id}/hyperliquid-balance", response_model=HyperliquidBalanceOut)
async def hyperliquid_balance(
    session_id: str,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(get_current_user)],
) -> HyperliquidBalanceOut:
    """Perp margin + spot token balances for this session's trading address (HL public info API)."""
    address, testnet, perp_raw, spot_raw = await _session_hyperliquid_raw_bundle(session_id, db, user)
    margin = HyperliquidMarginSummary(**margin_summary_from_clearinghouse(perp_raw))
    spot_rows = [SpotBalanceRow(**row) for row in spot_balances_rows(spot_raw)]
    return HyperliquidBalanceOut(address=address, testnet=testnet, margin=margin, spot_balances=spot_rows)


@router.get("/sessions/{session_id}/hyperliquid-account", response_model=HyperliquidAccountSnapshot)
async def hyperliquid_account_snapshot(
    session_id: str,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(get_current_user)],
) -> HyperliquidAccountSnapshot:
    """Public perp + spot clearinghouse payloads for this session's trading address (no key material)."""
    address, testnet, perp_raw, spot_raw = await _session_hyperliquid_raw_bundle(session_id, db, user)
    margin = HyperliquidMarginSummary(**margin_summary_from_clearinghouse(perp_raw))
    spot_rows = [SpotBalanceRow(**row) for row in spot_balances_rows(spot_raw)]
    return HyperliquidAccountSnapshot(
        address=address,
        testnet=testnet,
        margin=margin,
        clearinghouse_state=perp_raw,
        spot_clearinghouse_state=spot_raw,
        spot_balances=spot_rows,
    )


@router.post("/sessions/{session_id}/usd-class-transfer", response_model=UsdClassTransferOut)
async def usd_class_transfer(
    session_id: str,
    body: UsdClassTransferIn,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(get_current_user)],
) -> UsdClassTransferOut:
    """
    Move USDC between Hyperliquid spot and perp collateral using the session wallet (server-signed).
    When ``to_perp`` is true and ``amount`` is omitted, transfers all free spot USDC (total minus hold).
    """
    from hyperliquid.utils.error import ClientError, ServerError

    r = await db.execute(
        select(BotSession).where(BotSession.id == session_id, BotSession.user_id == user.id)
    )
    s = r.scalar_one_or_none()
    if not s:
        raise HTTPException(status_code=404, detail="Session not found")
    enc = _trading_encrypted_material(user, s)
    if not enc:
        raise HTTPException(
            status_code=400,
            detail="No trading key; save an encrypted key or provision a custodial wallet once per account",
        )
    addr = _trading_address_resolved(user, s)
    if not addr:
        raise HTTPException(status_code=400, detail="No trading address on this account")

    settings = get_settings()
    v = get_vault(settings.master_encryption_key)
    pk = v.decrypt(enc)
    cfg = dict(s.config or {})
    testnet = bool(cfg.get("testnet", True))

    try:
        perp_raw = await fetch_clearinghouse_state(addr, testnet)
        spot_raw = await fetch_spot_clearinghouse_state(addr, testnet)
    except httpx.HTTPError as e:
        raise HTTPException(status_code=502, detail=f"Hyperliquid info failed: {e!s}") from e

    spot_free = spot_usdc_available(spot_raw)
    try:
        withdrawable = float(perp_raw.get("withdrawable", 0) or 0)
    except (TypeError, ValueError):
        withdrawable = 0.0

    _eps = 1e-6
    if body.to_perp:
        if body.amount is None:
            amt = max(0.0, spot_free - 1e-8)
        else:
            amt = body.amount
        if amt <= 0:
            raise HTTPException(status_code=400, detail="No spot USDC to transfer (amount is zero)")
        if amt > spot_free + _eps:
            raise HTTPException(
                status_code=400,
                detail=f"Amount {amt} exceeds free spot USDC ({spot_free})",
            )
    else:
        if body.amount is None:
            raise HTTPException(
                status_code=400,
                detail="Perp→spot requires an explicit amount in the request body",
            )
        amt = body.amount
        if amt <= 0:
            raise HTTPException(status_code=400, detail="Amount must be positive")
        if amt > withdrawable + _eps:
            raise HTTPException(
                status_code=400,
                detail=f"Amount {amt} exceeds perp withdrawable ({withdrawable})",
            )

    try:
        hl_out = await asyncio.to_thread(_sync_usd_class_transfer, pk, testnet, amt, body.to_perp)
    except ClientError as e:
        logger.warning(
            "usd_class_transfer client error (status=%s testnet=%s to_perp=%s amount=%s): %s",
            getattr(e, "status_code", None),
            testnet,
            body.to_perp,
            amt,
            getattr(e, "error_message", repr(e)),
        )
        sc = int(e.status_code or 502)
        st = sc if 400 <= sc < 500 else status.HTTP_502_BAD_GATEWAY
        raise HTTPException(
            status_code=st,
            detail=f"Hyperliquid: {e.error_message or repr(e)}",
        ) from e
    except ServerError as e:
        logger.warning(
            "usd_class_transfer server error (testnet=%s to_perp=%s amount=%s): %s",
            testnet,
            body.to_perp,
            amt,
            getattr(e, "message", repr(e)),
        )
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Hyperliquid server error: {e.message}",
        ) from e
    except Exception as e:
        logger.exception(
            "usd_class_transfer unexpected error (testnet=%s to_perp=%s amount=%s)",
            testnet,
            body.to_perp,
            amt,
        )
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Failed to submit usd-class-transfer ({type(e).__name__}): {e!s}",
        ) from e

    hl_payload: dict[str, Any] = hl_out if isinstance(hl_out, dict) else {"result": hl_out}
    return UsdClassTransferOut(amount=amt, to_perp=body.to_perp, hyperliquid=hl_payload)


@router.post("/sessions/{session_id}/start", response_model=SessionStartOut)
async def start_session(
    session_id: str,
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(get_current_user)],
) -> SessionStartOut:
    r = await db.execute(
        select(BotSession).where(BotSession.id == session_id, BotSession.user_id == user.id)
    )
    s = r.scalar_one_or_none()
    if not s:
        raise HTTPException(status_code=404, detail="Session not found")
    if not _trading_encrypted_material(user, s):
        raise HTTPException(status_code=400, detail="No credentials uploaded for this account")
    s.status = "running"
    await db.commit()
    worker_token = create_worker_token(user.id, session_id)
    settings = get_settings()
    spawn_payload: dict | None = None
    spawn_err: str | None = None
    outcome_status = "running"
    if settings.spawn_local_lite_worker:
        api_base = str(request.base_url).rstrip("/")
        try:
            spawn_payload = spawn_lite_worker(
                api_base=api_base, session_id=session_id, worker_token=worker_token
            )
        except Exception as e:
            logger.exception("Failed to spawn lite worker for session %s", session_id)
            spawn_err = str(e)
            await db.execute(update(BotSession).where(BotSession.id == session_id).values(status="stopped"))
            await db.commit()
            outcome_status = "stopped"
    return SessionStartOut(
        status=outcome_status,
        worker_token=worker_token,
        session_id=session_id,
        worker_spawn=spawn_payload,
        worker_spawn_error=spawn_err,
        local_worker_autostart=settings.spawn_local_lite_worker,
    )


@router.post("/sessions/{session_id}/stop", response_model=SessionStopOut)
async def stop_session(
    session_id: str,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(get_current_user)],
) -> SessionStopOut:
    r = await db.execute(
        select(BotSession).where(BotSession.id == session_id, BotSession.user_id == user.id)
    )
    s = r.scalar_one_or_none()
    if not s:
        raise HTTPException(status_code=404, detail="Session not found")
    s.status = "stopped"
    await db.commit()
    worker_meta = stop_lite_worker(session_id)
    return SessionStopOut(status="stopped", worker=worker_meta)


@router.post("/sessions/{session_id}/close-all", response_model=SessionCloseAllOut)
async def close_all_orders_and_positions(
    session_id: str,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(get_current_user)],
) -> SessionCloseAllOut:
    """
    Emergency flatten for this session's trading wallet:
    1) cancel all open orders
    2) submit reduce-only IOC closes for all non-zero perp positions.
    """
    r = await db.execute(
        select(BotSession).where(BotSession.id == session_id, BotSession.user_id == user.id)
    )
    s = r.scalar_one_or_none()
    if not s:
        raise HTTPException(status_code=404, detail="Session not found")
    enc = _trading_encrypted_material(user, s)
    if not enc:
        raise HTTPException(
            status_code=400,
            detail="No trading key; save an encrypted key or provision a custodial wallet first",
        )

    settings = get_settings()
    v = get_vault(settings.master_encryption_key)
    pk = v.decrypt(enc)
    cfg = dict(s.config or {})
    testnet = bool(cfg.get("testnet", True))

    try:
        out = await asyncio.to_thread(_sync_close_all_orders_and_positions, pk, testnet)
    except Exception as e:
        logger.exception("close-all failed for session %s", session_id)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Failed to close all orders/positions: {e!s}",
        ) from e

    return SessionCloseAllOut(status="ok", session_id=session_id, **out)


@router.delete("/sessions/{session_id}", response_model=SessionDeleteOut)
async def delete_session(
    session_id: str,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(get_current_user)],
) -> SessionDeleteOut:
    """
    Delete one bot session only.
    Account-level custodial wallet/key is intentionally preserved for other sessions.
    """
    r = await db.execute(
        select(BotSession).where(BotSession.id == session_id, BotSession.user_id == user.id)
    )
    s = r.scalar_one_or_none()
    if not s:
        raise HTTPException(status_code=404, detail="Session not found")
    stop_lite_worker(session_id)
    await db.delete(s)
    await db.commit()
    return SessionDeleteOut(status="deleted", session_id=session_id, wallet_conserved=True)


@router.get("/sessions/{session_id}/worker/bootstrap", response_model=WorkerBootstrap)
async def worker_bootstrap(
    session_id: str,
    db: Annotated[AsyncSession, Depends(get_db)],
    authorization: Annotated[str | None, Header()] = None,
) -> WorkerBootstrap:
    """Called by worker with worker JWT (Bearer). Returns decrypted key once for process startup."""
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing bearer token")
    token = authorization.replace("Bearer ", "").strip()
    try:
        user_id = verify_worker_token(token, session_id)
    except ValueError as e:
        raise HTTPException(status_code=401, detail=str(e)) from e

    r = await db.execute(select(BotSession).where(BotSession.id == session_id, BotSession.user_id == user_id))
    s = r.scalar_one_or_none()
    ru = await db.execute(select(User).where(User.id == user_id))
    u = ru.scalar_one_or_none()
    enc = _trading_encrypted_material(u, s) if u and s else None
    if not s or not enc:
        raise HTTPException(status_code=404, detail="Session or key not found")

    settings = get_settings()
    v = get_vault(settings.master_encryption_key)
    pk = v.decrypt(enc)
    cfg = dict(s.config or {})
    testnet = bool(cfg.get("testnet", True))
    return WorkerBootstrap(private_key=pk, config=cfg, testnet=testnet)


@router.get("/sessions/{session_id}/events")
async def session_events(
    session_id: str,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(get_current_user)],
    limit: int = 500,
) -> list[dict]:
    r = await db.execute(
        select(BotSession).where(BotSession.id == session_id, BotSession.user_id == user.id)
    )
    if not r.scalar_one_or_none():
        raise HTTPException(status_code=404, detail="Session not found")
    return hub.history(session_id, limit=limit)


@router.websocket("/sessions/{session_id}/stream")
async def session_stream(websocket: WebSocket, session_id: str) -> None:
    token = websocket.query_params.get("token")
    if not token:
        await websocket.close(code=4401)
        return
    try:
        payload = decode_token(token)
        if payload.get("scope") == "worker":
            await websocket.close(code=4401)
            return
        if payload.get("sub") is None:
            await websocket.close(code=4401)
            return
        uid = str(payload["sub"])
    except Exception:
        await websocket.close(code=4401)
        return

    from hft_platform.database import async_session_maker

    async with async_session_maker() as db:
        r = await db.execute(
            select(BotSession).where(BotSession.id == session_id, BotSession.user_id == uid)
        )
        if not r.scalar_one_or_none():
            await websocket.close(code=4404)
            return

    await hub.connect(session_id, websocket)
    try:
        while True:
            await websocket.receive_text()
    except Exception:
        pass
    finally:
        hub.disconnect(session_id, websocket)
