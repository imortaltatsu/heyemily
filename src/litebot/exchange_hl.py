"""Async Hyperliquid reads (l2, mids, mark) + order placement via existing SDK adapter."""

from __future__ import annotations

import time
from typing import Any

import httpx

from exchanges.hyperliquid.adapter import HyperliquidAdapter
from interfaces.exchange import Order, OrderSide as LegacyOrderSide, OrderType
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
        self._mids_cache: tuple[float, dict[str, Any]] | None = None
        self._meta_ctx_cache: tuple[float, list[Any]] | None = None
        self._user_state_cache: tuple[float, dict[str, Any]] | None = None
        self._book_cache: dict[str, tuple[float, OrderBookDepth]] = {}

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

    async def _get_all_mids_cached(self, ttl_s: float = 0.5) -> dict[str, Any]:
        now = time.time()
        if self._mids_cache and (now - self._mids_cache[0]) <= ttl_s:
            return self._mids_cache[1]
        try:
            mids = await self._post_info({"type": "allMids"})
            if isinstance(mids, dict):
                self._mids_cache = (now, mids)
                return mids
        except Exception:
            if self._mids_cache:
                return self._mids_cache[1]
            raise
        if self._mids_cache:
            return self._mids_cache[1]
        return {}

    async def _get_meta_ctx_cached(self, ttl_s: float = 3.0) -> list[Any]:
        now = time.time()
        if self._meta_ctx_cache and (now - self._meta_ctx_cache[0]) <= ttl_s:
            return self._meta_ctx_cache[1]
        try:
            meta_ctx = await self._post_info({"type": "metaAndAssetCtxs"})
            if isinstance(meta_ctx, list):
                self._meta_ctx_cache = (now, meta_ctx)
                return meta_ctx
        except Exception:
            if self._meta_ctx_cache:
                return self._meta_ctx_cache[1]
            raise
        if self._meta_ctx_cache:
            return self._meta_ctx_cache[1]
        return []

    async def _get_user_state_cached(self, ttl_s: float = 0.8) -> dict[str, Any]:
        now = time.time()
        if self._user_state_cache and (now - self._user_state_cache[0]) <= ttl_s:
            return self._user_state_cache[1]
        try:
            if not self._adapter.exchange or not self._adapter.info:
                return {}
            state = self._adapter.info.user_state(self._adapter.exchange.wallet.address)
            if isinstance(state, dict):
                self._user_state_cache = (now, state)
                return state
        except Exception:
            if self._user_state_cache:
                return self._user_state_cache[1]
            raise
        if self._user_state_cache:
            return self._user_state_cache[1]
        return {}

    async def get_orderbook_depth(self, symbol: str, depth_levels: int) -> OrderBookDepth:
        try:
            data = await self._post_info({"type": "l2Book", "coin": symbol})
        except Exception:
            cached = self._book_cache.get(symbol)
            if cached:
                return cached[1]
            raise
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

        depth = OrderBookDepth(
            symbol=symbol,
            best_bid_px=bb_px,
            best_ask_px=ba_px,
            bid_sz_top=bb_sz,
            ask_sz_top=ba_sz,
            bid_depth_n=bid_d,
            ask_depth_n=ask_d,
            timestamp=now,
        )
        self._book_cache[symbol] = (now, depth)
        return depth

    async def get_micro_gap(self, symbol: str) -> MicroGap:
        mids = await self._get_all_mids_cached()
        mid = float(mids.get(symbol, 0) or 0)
        ma = await self._get_meta_ctx_cached()
        if len(ma) < 2:
            return MicroGap(
                symbol=symbol,
                mid=mid,
                mark=mid,
                gap_bps=0.0,
                timestamp=time.time(),
            )
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
        user_state = await self._get_user_state_cached()
        mids = await self._get_all_mids_cached()
        cur = float(mids.get(symbol, 0) or 0)
        for pos_info in user_state.get("assetPositions") or []:
            position = pos_info.get("position") if isinstance(pos_info, dict) else None
            if not isinstance(position, dict):
                continue
            if position.get("coin") != symbol:
                continue
            size = float(position.get("szi", 0) or 0)
            if abs(size) <= 1e-12:
                continue
            entry = float(position.get("entryPx") or 0)
            return PositionState(
                symbol=symbol,
                size=size,
                entry_price=entry,
                current_price=cur if cur > 0 else entry,
                opened_at=time.time(),
            )
        return PositionState(
            symbol=symbol,
            size=0.0,
            entry_price=cur if cur > 0 else 0.0,
            current_price=cur if cur > 0 else 0.0,
            opened_at=time.time(),
        )

    async def get_cash_balance(self) -> float:
        user_state = await self._get_user_state_cached()
        withdrawable = float(user_state.get("withdrawable", 0) or 0)
        if withdrawable > 0:
            return withdrawable
        ms = user_state.get("marginSummary") or user_state.get("crossMarginSummary") or {}
        return float(ms.get("accountValue", 0) or 0)

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
