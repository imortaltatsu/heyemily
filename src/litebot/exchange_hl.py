"""Async Hyperliquid reads (l2, mids, mark) + order placement via existing SDK adapter."""

from __future__ import annotations

import time
from typing import Any

import httpx

from exchanges.hyperliquid.adapter import HyperliquidAdapter
from interfaces.exchange import Order, OrderSide as LegacyOrderSide, OrderType
from interfaces.strategy import Position as LegacyPos
from litebot.interfaces import Exchange, MicroGap, OrderBookDepth, OrderSide, PositionState
from litebot.jit_kernels import micro_gap_bps


def _hl_base(testnet: bool) -> str:
    return (
        "https://api.hyperliquid-testnet.xyz"
        if testnet
        else "https://api.hyperliquid.xyz"
    )


class LiteHyperliquidExchange(Exchange):
    def __init__(self, private_key: str, testnet: bool = True, symbol: str = "BTC", leverage: int = 3):
        self._adapter = HyperliquidAdapter(private_key, testnet=testnet)
        self._testnet = testnet
        self._base = _hl_base(testnet)
        self._client: httpx.AsyncClient | None = None
        self._last_order_error: str | None = None
        self._symbol = symbol
        self._leverage = max(1, int(leverage))

    async def connect(self) -> bool:
        ok = await self._adapter.connect()
        if ok:
            if self._leverage > 1:
                try:
                    # Set cross leverage for the traded symbol once on startup.
                    self._adapter.exchange.update_leverage(self._leverage, self._symbol, is_cross=True)
                except Exception as e:
                    self._last_order_error = f"Failed to set leverage {self._leverage}x on {self._symbol}: {e}"
            self._client = httpx.AsyncClient(
                timeout=httpx.Timeout(8.0, connect=1.5),
                limits=httpx.Limits(max_keepalive_connections=32, max_connections=32),
                http2=False,
            )
        return ok

    async def disconnect(self) -> None:
        if self._client:
            await self._client.aclose()
            self._client = None
        await self._adapter.disconnect()

    async def _post_info(self, body: dict[str, Any]) -> Any:
        if not self._client:
            raise RuntimeError("exchange not connected")
        r = await self._client.post(
            f"{self._base}/info",
            json=body,
            headers={"Content-Type": "application/json"},
        )
        r.raise_for_status()
        return r.json()

    async def get_orderbook_depth(self, symbol: str, depth_levels: int) -> OrderBookDepth:
        data = await self._post_info({"type": "l2Book", "coin": symbol})
        levels = data.get("levels") or [[], []]
        bids = levels[0] if len(levels) > 0 else []
        asks = levels[1] if len(levels) > 1 else []
        now = time.time()

        def _sum_sz(rows: list[dict[str, Any]], n: int) -> tuple[float, float, float]:
            if not rows:
                return 0.0, 0.0, 0.0
            top_px = float(rows[0].get("px", 0))
            top_sz = float(rows[0].get("sz", 0))
            depth = sum(float(x.get("sz", 0)) for x in rows[:n])
            return top_px, top_sz, depth

        bb_px, bb_sz, bid_d = _sum_sz(bids, depth_levels)
        ba_px, ba_sz, ask_d = _sum_sz(asks, depth_levels)

        return OrderBookDepth(
            symbol=symbol,
            best_bid_px=bb_px,
            best_ask_px=ba_px,
            bid_sz_top=bb_sz,
            ask_sz_top=ba_sz,
            bid_depth_n=bid_d,
            ask_depth_n=ask_d,
            timestamp=now,
        )

    async def get_micro_gap(self, symbol: str) -> MicroGap:
        mids = await self._post_info({"type": "allMids"})
        mid = float(mids.get(symbol, 0) or 0)
        ma = await self._post_info({"type": "metaAndAssetCtxs"})
        meta, ctxs = ma[0], ma[1]
        universe = meta.get("universe", [])
        idx = next((i for i, u in enumerate(universe) if u.get("name") == symbol), None)
        if idx is None or idx >= len(ctxs):
            mark = mid
        else:
            mark = float(ctxs[idx].get("markPx", mid) or mid)
        if mark <= 0:
            mark = mid
        gap_bps = micro_gap_bps(mid, mark)
        return MicroGap(
            symbol=symbol,
            mid=mid,
            mark=mark,
            gap_bps=gap_bps,
            timestamp=time.time(),
        )

    async def place_market_order(self, symbol: str, side: OrderSide, size: float) -> bool:
        self._last_order_error = None
        legacy_side = LegacyOrderSide.BUY if side == OrderSide.BUY else LegacyOrderSide.SELL
        order = Order(
            id=f"lite_{int(time.time() * 1000)}",
            asset=symbol,
            side=legacy_side,
            size=size,
            order_type=OrderType.MARKET,
            price=None,
            created_at=time.time(),
        )
        try:
            oid = await self._adapter.place_order(order)
            return bool(oid)
        except Exception as e:
            self._last_order_error = str(e)
            return False

    def pop_last_order_error(self) -> str | None:
        err = self._last_order_error
        self._last_order_error = None
        return err

    async def get_position_state(self, symbol: str) -> PositionState:
        positions = await self._adapter.get_positions()

        for p in positions:
            if isinstance(p, LegacyPos) and p.asset == symbol:
                cur = await self._adapter.get_market_price(symbol)
                return PositionState(
                    symbol=symbol,
                    size=float(p.size),
                    entry_price=float(p.entry_price),
                    current_price=float(cur),
                    opened_at=float(p.timestamp),
                )
        cur = await self._adapter.get_market_price(symbol)
        return PositionState(
            symbol=symbol,
            size=0.0,
            entry_price=cur,
            current_price=cur,
            opened_at=time.time(),
        )

    async def get_cash_balance(self) -> float:
        for asset in ("USDC", "USD"):
            try:
                b = await self._adapter.get_balance(asset)
                if b.total > 0 or b.available > 0:
                    return float(b.available)
            except Exception:
                continue
        return 0.0

    async def close_position(self, symbol: str, size: float | None = None) -> bool:
        self._last_order_error = None
        try:
            ok = await self._adapter.close_position(symbol, size)
            if not ok and self._last_order_error is None:
                self._last_order_error = f"Close position failed for {symbol}"
            return ok
        except Exception as e:
            self._last_order_error = str(e)
            return False
