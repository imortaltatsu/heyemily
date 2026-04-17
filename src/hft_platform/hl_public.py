from __future__ import annotations

from typing import Any

import httpx

_TESTNET_INFO = "https://api.hyperliquid-testnet.xyz/info"
_MAINNET_INFO = "https://api.hyperliquid.xyz/info"


async def fetch_clearinghouse_state(address: str, testnet: bool) -> dict[str, Any]:
    url = _TESTNET_INFO if testnet else _MAINNET_INFO
    async with httpx.AsyncClient(timeout=20.0) as client:
        r = await client.post(url, json={"type": "clearinghouseState", "user": address})
        r.raise_for_status()
        return r.json()


async def fetch_spot_clearinghouse_state(address: str, testnet: bool) -> dict[str, Any]:
    """Spot token balances (often where new USDC deposits appear before perp transfer)."""
    url = _TESTNET_INFO if testnet else _MAINNET_INFO
    async with httpx.AsyncClient(timeout=20.0) as client:
        r = await client.post(url, json={"type": "spotClearinghouseState", "user": address})
        r.raise_for_status()
        return r.json()


def spot_usdc_available(spot: dict[str, Any]) -> float:
    """Free spot USDC (total minus hold) from spotClearinghouseState; 0 if no USDC row."""
    for row in spot_balances_rows(spot):
        if row["coin"] == "USDC":
            try:
                total = float(row["total"])
                hold = float(row["hold"])
            except ValueError:
                return 0.0
            return max(0.0, total - hold)
    return 0.0


def spot_balances_rows(spot: dict[str, Any]) -> list[dict[str, str]]:
    bals = spot.get("balances")
    if not isinstance(bals, list):
        return []
    out: list[dict[str, str]] = []
    for b in bals:
        if not isinstance(b, dict):
            continue
        out.append(
            {
                "coin": str(b.get("coin", "")),
                "total": str(b.get("total", "0")),
                "hold": str(b.get("hold", "0")),
                "entry_ntl": str(b.get("entryNtl", "0")),
            }
        )
    return out


def margin_summary_from_clearinghouse(state: dict[str, Any]) -> dict[str, Any]:
    """Normalize Hyperliquid clearinghouseState for UI (values are HL string decimals)."""
    ms = state.get("marginSummary")
    if not isinstance(ms, dict):
        ms = {}
    positions = state.get("assetPositions")
    if not isinstance(positions, list):
        positions = []
    return {
        "account_value": str(ms.get("accountValue", "0")),
        "withdrawable": str(state.get("withdrawable", "0")),
        "total_margin_used": str(ms.get("totalMarginUsed", "0")),
        "total_ntl_pos": str(ms.get("totalNtlPos", "0")),
        "total_raw_usd": str(ms.get("totalRawUsd", "0")),
        "open_positions": len(positions),
    }
