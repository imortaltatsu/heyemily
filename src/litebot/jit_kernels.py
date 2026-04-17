"""
Numba-accelerated scalar math for the lite HFT hot path.

Uses ``cache=True`` so LLVM IR is cached on disk, and ``fastmath=True`` for faster
float ops (trading thresholds do not require strict IEEE denormal handling).

Set ``NUMBA_DISABLE_JIT=1`` to force interpreted mode for debugging.
"""

from __future__ import annotations

from numba import njit

# --- Book / mark microstructure -------------------------------------------------


@njit(cache=True, fastmath=True)
def mid_price(best_bid_px: float, best_ask_px: float) -> float:
    return 0.5 * (best_bid_px + best_ask_px)


@njit(cache=True, fastmath=True)
def book_imbalance(bid_depth_n: float, ask_depth_n: float) -> float:
    b = bid_depth_n + 1e-12
    a = ask_depth_n + 1e-12
    return (b - a) / (b + a)


@njit(cache=True, fastmath=True)
def micro_gap_bps(mid: float, mark: float) -> float:
    m = max(mark, 1e-12)
    return (mid - m) / m * 10000.0


@njit(cache=True, fastmath=True)
def notional_abs(size: float, price: float) -> float:
    return abs(size * price)


@njit(cache=True, fastmath=True)
def order_size_from_usd(order_size_usd: float, mid: float) -> float:
    return order_size_usd / max(mid, 1e-12)


# --- Strategy decision (flat numeric state machine) ----------------------------

# action: 0 = none, 1 = close, 2 = long, 3 = short
# reason: 0 = none, 1 = hold_timeout, 2 = imb_reversal, 3 = gap_reversal,
#         4 = imb_long_gap_up, 5 = imb_short_gap_down


@njit(cache=True, fastmath=True)
def micro_arb_decision(
    imb: float,
    gap_bps: float,
    pos_size: float,
    cash: float,
    now: float,
    last_trade_ts: float,
    opened_at: float,
    imb_th: float,
    gap_min: float,
    hold_timeout_s: float,
    cooldown_s: float,
    order_size_usd: float,
    best_bid: float,
    best_ask: float,
) -> tuple[int, float, int]:
    eps = 1e-8
    imb_half = 0.5 * imb_th
    gap_half = 0.5 * gap_min
    in_pos = abs(pos_size) > eps

    if not in_pos:
        if now - last_trade_ts < cooldown_s:
            return 0, 0.0, 0

    if in_pos:
        held = now - opened_at
        if held >= hold_timeout_s:
            return 1, abs(pos_size), 1
        if pos_size > eps:
            if imb < -imb_half:
                return 1, abs(pos_size), 2
            if gap_bps < -gap_half:
                return 1, abs(pos_size), 3
        elif pos_size < -eps:
            if imb > imb_half:
                return 1, abs(pos_size), 2
            if gap_bps > gap_half:
                return 1, abs(pos_size), 3
        return 0, 0.0, 0

    if cash < 0.5 * order_size_usd:
        return 0, 0.0, 0

    mid = 0.5 * (best_bid + best_ask)
    osz = order_size_usd / max(mid, 1e-12)

    if imb >= imb_th and gap_bps >= gap_min:
        return 2, osz, 4
    if imb <= -imb_th and gap_bps <= -gap_min:
        return 3, osz, 5
    return 0, 0.0, 0


@njit(cache=True, fastmath=True)
def projected_symbol_notional(current_sym: float, est: float) -> float:
    if current_sym > 1e-8:
        return current_sym + est
    return est


@njit(cache=True, fastmath=True)
def risk_allow_checks(
    kill_switch: int,
    orders_this_second: int,
    max_orders_per_second: int,
    open_notional: float,
    est_notional: float,
    max_open_notional: float,
    symbol_notional: float,
    max_position_symbol: float,
    consecutive_losses: int,
    max_consecutive: int,
    daily_realized: float,
    max_daily_loss: float,
) -> int:
    """Return 0 if allowed, else 1..7 rejection codes."""
    if kill_switch != 0:
        return 1
    if orders_this_second >= max_orders_per_second:
        return 2
    if open_notional + est_notional > max_open_notional:
        return 3
    if symbol_notional > max_position_symbol:
        return 4
    if consecutive_losses >= max_consecutive:
        return 5
    if daily_realized <= -max_daily_loss:
        return 6
    return 0


def warmup_numba_kernels() -> None:
    """
    Force one-time LLVM compilation before the first live tick.

    Without this, the first iterations pay full compile latency inside the hot path.
    """
    mid_price(95_000.0, 95_100.0)
    book_imbalance(10.0, 5.0)
    micro_gap_bps(95_050.0, 95_000.0)
    notional_abs(0.01, 95_000.0)
    order_size_from_usd(50.0, 95_050.0)
    projected_symbol_notional(100.0, 50.0)
    risk_allow_checks(0, 0, 100, 0.0, 50.0, 1e9, 100.0, 1e9, 0, 99, 0.0, 500.0)
    micro_arb_decision(
        0.4,
        2.0,
        0.0,
        10_000.0,
        1_700_000_000.0,
        0.0,
        0.0,
        0.35,
        1.0,
        0.8,
        0.15,
        50.0,
        95_000.0,
        95_100.0,
    )
