from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field, field_validator


class UserOut(BaseModel):
    id: str
    wallet_address: str
    created_at: datetime
    trading_custodial_address: str | None = None

    class Config:
        from_attributes = True


class Token(BaseModel):
    access_token: str
    token_type: str = "bearer"


class WalletChallengeIn(BaseModel):
    address: str = Field(min_length=42, max_length=42)


class ChallengeOut(BaseModel):
    message: str


class WalletVerifyIn(BaseModel):
    address: str = Field(min_length=42, max_length=42)
    message: str = Field(min_length=1)
    signature: str = Field(min_length=130, max_length=200)


class BotSessionCreate(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    config: dict[str, Any] = Field(default_factory=dict)


class BotSessionOut(BaseModel):
    id: str
    name: str
    config: dict[str, Any]
    status: str
    custodial_address: str | None = None
    created_at: datetime

    class Config:
        from_attributes = True


class CredentialIn(BaseModel):
    """Hex-encoded EVM private key (with or without 0x). Validated again in the route with eth_account."""

    private_key: str = Field(min_length=1, max_length=512)

    @field_validator("private_key")
    @classmethod
    def strip_private_key(cls, v: str) -> str:
        s = v.strip()
        if not s:
            raise ValueError("private_key must not be empty")
        return s


class CustodialWalletOut(BaseModel):
    address: str
    session_id: str


class ExportPrivateKeyOut(BaseModel):
    """Decrypted trading key material for explicit user export actions."""

    private_key: str
    custodial_address: str
    session_id: str


class HyperliquidMarginSummary(BaseModel):
    """Cross-margin headline fields from clearinghouseState (HL returns string decimals)."""

    account_value: str
    withdrawable: str
    total_margin_used: str
    total_ntl_pos: str
    total_raw_usd: str
    open_positions: int = 0


class SpotBalanceRow(BaseModel):
    """One spot token row from spotClearinghouseState.balances."""

    coin: str
    total: str
    hold: str
    entry_ntl: str = "0"


class HyperliquidBalanceOut(BaseModel):
    """Session trading address + perp margin + spot token balances."""

    address: str
    testnet: bool
    margin: HyperliquidMarginSummary
    spot_balances: list[SpotBalanceRow] = Field(default_factory=list)


class HyperliquidAccountSnapshot(BaseModel):
    address: str
    testnet: bool
    margin: HyperliquidMarginSummary
    clearinghouse_state: dict[str, Any]
    spot_clearinghouse_state: dict[str, Any]
    spot_balances: list[SpotBalanceRow] = Field(default_factory=list)


class WorkerBootstrap(BaseModel):
    private_key: str
    config: dict[str, Any]
    testnet: bool


class SessionStartOut(BaseModel):
    status: str
    worker_token: str
    session_id: str
    worker_spawn: dict[str, Any] | None = None
    worker_spawn_error: str | None = None
    local_worker_autostart: bool = True


class SessionStopOut(BaseModel):
    status: str
    worker: dict[str, Any] | None = None


class SessionCloseAllOut(BaseModel):
    status: str
    session_id: str
    cancelled_orders: int = 0
    attempted_position_closes: int = 0
    failed_order_cancels: int = 0
    failed_position_closes: int = 0
    details: dict[str, Any] = Field(default_factory=dict)


class SessionDeleteOut(BaseModel):
    status: str
    session_id: str
    wallet_conserved: bool = True


class UsdClassTransferIn(BaseModel):
    """Spot ↔ perp USDC (collateral) via Hyperliquid `usdClassTransfer`."""

    to_perp: bool = True
    amount: float | None = None
    """
    USDC notional to move. If omitted and to_perp is true, moves all free spot USDC (total−hold).
    If to_perp is false, amount is required (capped by perp withdrawable).
    """


class UsdClassTransferOut(BaseModel):
    amount: float
    to_perp: bool
    hyperliquid: dict[str, Any] = Field(default_factory=dict)


class TelemetryIn(BaseModel):
    kind: str
    ts: float
    session_id: str | None = None
    symbol: str | None = None
    data: dict[str, Any] = Field(default_factory=dict)
