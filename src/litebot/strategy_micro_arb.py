"""Order-book imbalance + single-venue micro-gap filter (sub-second holds).

Core numeric decision path uses Numba JIT (see :mod:`litebot.jit_kernels`).
"""

from __future__ import annotations

from typing import Any

from litebot.interfaces import (
    MicroGap,
    OrderBookDepth,
    PositionState,
    SignalType,
    Strategy,
    TradingSignal,
)
from litebot.jit_kernels import micro_arb_decision

_REASONS: dict[int, str] = {
    0: "",
    1: "hold_timeout",
    2: "imb_reversal",
    3: "gap_reversal",
    4: "imb_long_gap_up",
    5: "imb_short_gap_down",
}


class MicroArbStrategy(Strategy):
    """
    Enter when top-of-book imbalance and mark/mid gap align; exit on reversal or timeout.

    gap_bps = (mid - mark) / mark * 1e4  (positive => mid above mark)
    """

    def __init__(self, config: dict[str, Any]):
        super().__init__(config)
        self._imbalance_threshold = float(config.get("imbalance_threshold", 0.35))
        self._micro_gap_min_bps = float(config.get("micro_gap_min_bps", 1.0))
        self._hold_timeout_s = float(config.get("hold_timeout_ms", 800)) / 1000.0
        self._cooldown_s = float(config.get("cooldown_ms", 150)) / 1000.0
        self._order_size_usd = float(config.get("order_size_usd", 50.0))
        self._last_trade_ts = 0.0

    def evaluate(
        self,
        book: OrderBookDepth,
        gap: MicroGap,
        position: PositionState,
        cash: float,
        now: float,
    ) -> TradingSignal | None:
        symbol = book.symbol
        imb = book.imbalance
        gap_bps = gap.gap_bps

        action, size, rcode = micro_arb_decision(
            imb,
            gap_bps,
            position.size,
            cash,
            now,
            self._last_trade_ts,
            position.opened_at,
            self._imbalance_threshold,
            self._micro_gap_min_bps,
            self._hold_timeout_s,
            self._cooldown_s,
            self._order_size_usd,
            book.best_bid_px,
            book.best_ask_px,
        )

        if action == 0:
            return None

        reason = _REASONS.get(rcode, "unknown")
        meta: dict[str, Any] = {"imbalance": imb, "gap_bps": gap_bps}

        if action == 1:
            self._last_trade_ts = now
            return TradingSignal(
                SignalType.CLOSE,
                symbol,
                size,
                reason=reason,
                metadata={**meta, "side": "flatten"},
            )
        if action == 2:
            self._last_trade_ts = now
            return TradingSignal(
                SignalType.LONG,
                symbol,
                size,
                reason=reason,
                metadata=meta,
            )
        if action == 3:
            self._last_trade_ts = now
            return TradingSignal(
                SignalType.SHORT,
                symbol,
                size,
                reason=reason,
                metadata=meta,
            )
        return None


def strategy_config_from_lite(cfg: Any) -> dict[str, Any]:
    """Build strategy dict from LiteBotConfig dataclass."""
    return {
        "imbalance_threshold": cfg.imbalance_threshold,
        "micro_gap_min_bps": cfg.micro_gap_min_bps,
        "hold_timeout_ms": cfg.hold_timeout_ms,
        "cooldown_ms": cfg.cooldown_ms,
        "order_size_usd": cfg.order_size_usd,
    }
