from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from litebot.jit_kernels import book_imbalance, mid_price, notional_abs


class SignalType(Enum):
    """Trading signal for the lite HFT engine."""

    LONG = "long"
    SHORT = "short"
    FLAT = "flat"
    CLOSE = "close"


class OrderSide(Enum):
    BUY = "buy"
    SELL = "sell"


@dataclass(slots=True)
class MarketTick:
    symbol: str
    price: float
    timestamp: float


@dataclass(slots=True)
class OrderBookDepth:
    """Top-of-book and shallow depth for imbalance calculation."""

    symbol: str
    best_bid_px: float
    best_ask_px: float
    bid_sz_top: float
    ask_sz_top: float
    bid_depth_n: float
    ask_depth_n: float
    timestamp: float

    @property
    def mid(self) -> float:
        return mid_price(self.best_bid_px, self.best_ask_px)

    @property
    def imbalance(self) -> float:
        """Range [-1, 1]: positive = more bid pressure (Numba JIT)."""
        return book_imbalance(self.bid_depth_n, self.ask_depth_n)


@dataclass(slots=True)
class MicroGap:
    """Single-venue micro gap: mid vs mark (or index) in basis points."""

    symbol: str
    mid: float
    mark: float
    gap_bps: float
    timestamp: float


@dataclass(slots=True)
class PositionState:
    symbol: str
    size: float
    entry_price: float
    current_price: float
    opened_at: float

    @property
    def notional(self) -> float:
        return notional_abs(self.size, self.current_price)

    @property
    def side_sign(self) -> int:
        if self.size > 1e-12:
            return 1
        if self.size < -1e-12:
            return -1
        return 0


@dataclass(slots=True)
class TradingSignal:
    signal_type: SignalType
    symbol: str
    size: float
    reason: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class MetricsEvent:
    """Telemetry for dashboard / analysis (non-blocking publish)."""

    kind: str
    ts: float
    session_id: str | None
    symbol: str | None
    data: dict[str, Any] = field(default_factory=dict)


class Exchange(ABC):
    """Minimal exchange surface for the lite worker."""

    @abstractmethod
    async def connect(self) -> bool:
        raise NotImplementedError

    @abstractmethod
    async def disconnect(self) -> None:
        raise NotImplementedError

    @abstractmethod
    async def get_orderbook_depth(self, symbol: str, depth_levels: int) -> OrderBookDepth:
        raise NotImplementedError

    @abstractmethod
    async def get_micro_gap(self, symbol: str) -> MicroGap:
        raise NotImplementedError

    @abstractmethod
    async def place_market_order(self, symbol: str, side: OrderSide, size: float) -> bool:
        raise NotImplementedError

    @abstractmethod
    async def get_position_state(self, symbol: str) -> PositionState:
        raise NotImplementedError

    @abstractmethod
    async def get_cash_balance(self) -> float:
        raise NotImplementedError


class Strategy(ABC):
    def __init__(self, config: dict[str, Any]):
        self.config = config
        self.active = True

    @abstractmethod
    def evaluate(
        self,
        book: OrderBookDepth,
        gap: MicroGap,
        position: PositionState,
        cash: float,
        now: float,
    ) -> TradingSignal | None:
        raise NotImplementedError
