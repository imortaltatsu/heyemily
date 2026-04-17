"""Aggressive HFT risk gates with mandatory kill-switches (Numba JIT comparisons)."""

from __future__ import annotations

import math
import time
from dataclasses import dataclass
from typing import Any

from litebot.interfaces import OrderSide, SignalType, TradingSignal
from litebot.jit_kernels import risk_allow_checks

_RISK_REASONS: dict[int, str] = {
    1: "kill_switch",
    2: "max_orders_per_second",
    3: "max_open_notional",
    4: "max_position_symbol",
    5: "max_consecutive_losses",
    6: "max_daily_loss",
}


@dataclass
class RiskState:
    orders_this_second: int = 0
    second_bucket: int = 0
    consecutive_losses: int = 0
    daily_realized_pnl: float = 0.0
    day_bucket: str = ""
    open_notional_usd: float = 0.0


class RiskManager:
    def __init__(self, risk_cfg: Any) -> None:
        self.cfg = risk_cfg
        self.state = RiskState()

    def _roll_windows(self) -> None:
        now = int(time.time())
        if self.state.second_bucket != now:
            self.state.second_bucket = now
            self.state.orders_this_second = 0
        day = time.strftime("%Y-%m-%d", time.gmtime(now))
        if self.state.day_bucket != day:
            self.state.day_bucket = day
            self.state.daily_realized_pnl = 0.0

    def allow_new_order(self, est_notional: float, symbol_notional: float) -> tuple[bool, str]:
        self._roll_windows()
        max_rps = max(1, int(math.ceil(float(self.cfg.max_orders_per_second))))
        code = risk_allow_checks(
            1 if self.cfg.kill_switch else 0,
            self.state.orders_this_second,
            max_rps,
            self.state.open_notional_usd,
            est_notional,
            float(self.cfg.max_open_notional_usd),
            symbol_notional,
            float(self.cfg.max_position_per_symbol_usd),
            self.state.consecutive_losses,
            int(self.cfg.max_consecutive_losses),
            self.state.daily_realized_pnl,
            abs(float(self.cfg.max_daily_realized_loss_usd)),
        )
        if code == 0:
            return True, "ok"
        return False, _RISK_REASONS.get(code, "risk_block")

    def record_order_submitted(self) -> None:
        self._roll_windows()
        self.state.orders_this_second += 1

    def update_open_notional(self, n: float) -> None:
        self.state.open_notional_usd = max(0.0, n)

    def record_closed_pnl(self, pnl_usd: float) -> None:
        self._roll_windows()
        self.state.daily_realized_pnl += pnl_usd
        if pnl_usd < 0:
            self.state.consecutive_losses += 1
        else:
            self.state.consecutive_losses = 0


def signal_to_side(sig: TradingSignal) -> OrderSide | None:
    if sig.signal_type == SignalType.LONG:
        return OrderSide.BUY
    if sig.signal_type == SignalType.SHORT:
        return OrderSide.SELL
    return None
