"""Sub-second lite HFT engine: strategy + risk + telemetry (decoupled)."""

from __future__ import annotations

import asyncio
import time
from typing import Any

from litebot.config import LiteBotConfig
from litebot.exchange_hl import LiteHyperliquidExchange
from litebot.jit_kernels import (
    mid_price,
    notional_abs,
    order_size_from_usd,
    projected_symbol_notional,
    warmup_numba_kernels,
)
from litebot.interfaces import (
    MetricsEvent,
    OrderSide,
    PositionState,
    SignalType,
    TradingSignal,
)
from litebot.risk import RiskManager, signal_to_side
from litebot.strategy_micro_arb import MicroArbStrategy, strategy_config_from_lite
from litebot.telemetry import TelemetryHub

MIN_ORDER_USD = 10.0


class LiteHFTEngine:
    def __init__(
        self,
        cfg: LiteBotConfig,
        private_key: str,
        telemetry: TelemetryHub | None = None,
    ):
        self.cfg = cfg
        self._private_key = private_key
        self.telemetry = telemetry or TelemetryHub(buffer_max=cfg.telemetry.buffer_max)
        self._exchange = LiteHyperliquidExchange(
            private_key,
            testnet=cfg.testnet,
            symbol=cfg.symbol,
            leverage=cfg.leverage,
        )
        self._strategy = MicroArbStrategy(strategy_config_from_lite(cfg))
        self._risk = RiskManager(cfg.risk)
        self._running = False
        self._entry_ts: float | None = None
        self._session_id = cfg.telemetry.session_id
        self._last_pnl_emit_ts: float = 0.0

    async def run_forever(self) -> None:
        self._running = True
        self.telemetry.configure_push(
            self.cfg.telemetry.push_url,
            self.cfg.telemetry.push_token,
            self.cfg.telemetry.session_id,
        )
        self.telemetry.start_background()

        if not await self._exchange.connect():
            await self.telemetry.emit(
                MetricsEvent(
                    "error",
                    time.time(),
                    self._session_id,
                    self.cfg.symbol,
                    {"msg": "connect_failed"},
                )
            )
            return

        warmup_numba_kernels()

        await self.telemetry.emit(
            MetricsEvent(
                "started",
                time.time(),
                self._session_id,
                self.cfg.symbol,
                {"testnet": self.cfg.testnet},
            )
        )

        self._last_interval_buy_ts = time.time()
        self._last_interval_sell_ts = time.time()

        while self._running:
            loop_t0 = time.perf_counter()
            now = time.time()
            try:
                await self._tick(now)
            except Exception as e:
                await self.telemetry.emit(
                    MetricsEvent(
                        "error",
                        time.time(),
                        self._session_id,
                        self.cfg.symbol,
                        {"msg": str(e)},
                    )
                )
            elapsed = time.perf_counter() - loop_t0
            sleep_ms = max(
                0.0, self.cfg.loop_interval_ms / 1000.0 - elapsed
            )
            await asyncio.sleep(sleep_ms)

    def stop(self) -> None:
        self._running = False

    async def shutdown(self) -> None:
        self.stop()
        await self._exchange.disconnect()

    async def _tick(self, now: float) -> None:
        sym = self.cfg.symbol
        t_io0 = time.perf_counter()
        book, gap = await asyncio.gather(
            self._exchange.get_orderbook_depth(sym, self.cfg.depth_levels),
            self._exchange.get_micro_gap(sym),
        )
        raw, cash = await asyncio.gather(
            self._exchange.get_position_state(sym),
            self._exchange.get_cash_balance(),
        )
        io_ms = (time.perf_counter() - t_io0) * 1000.0
        pos = self._merge_position_clock(raw)
        unrealized_pnl = (pos.current_price - pos.entry_price) * pos.size
        position_notional = notional_abs(pos.size, pos.current_price)

        # Emit compact PnL telemetry even when tick events are disabled.
        if now - self._last_pnl_emit_ts >= 0.5:
            await self.telemetry.emit(
                MetricsEvent(
                    "pnl",
                    now,
                    self._session_id,
                    sym,
                    {
                        "unrealized_pnl": unrealized_pnl,
                        "realized_pnl": 0.0,
                        "position_size": pos.size,
                        "position_notional": position_notional,
                        "entry_price": pos.entry_price,
                        "mark_price": pos.current_price,
                        "cash": cash,
                    },
                )
            )
            self._last_pnl_emit_ts = now

        # Estimate open notional for risk (JIT)
        notional = position_notional
        self._risk.update_open_notional(notional)

        t_dec0 = time.perf_counter()
        sig = self._strategy.evaluate(book, gap, pos, cash, now)
        dec_ms = (time.perf_counter() - t_dec0) * 1000.0

        if self.cfg.telemetry.emit_tick_events:
            await self.telemetry.emit(
                MetricsEvent(
                    "tick",
                    now,
                    self._session_id,
                    sym,
                    {
                        "imbalance": book.imbalance,
                        "gap_bps": gap.gap_bps,
                        "mid": gap.mid,
                        "mark": gap.mark,
                        "position_size": pos.size,
                        "position_notional": notional,
                        "entry_price": pos.entry_price,
                        "unrealized_pnl": unrealized_pnl,
                        "cash": cash,
                        "io_ms": io_ms,
                        "strategy_eval_ms": dec_ms,
                    },
                )
            )

        if sig and sig.signal_type == SignalType.CLOSE:
            await self.telemetry.emit(
                MetricsEvent(
                    "signal",
                    now,
                    self._session_id,
                    sym,
                    {
                        "type": sig.signal_type.value,
                        "reason": sig.reason,
                        "size": sig.size,
                    },
                )
            )
            if self.cfg.risk.kill_switch:
                await self.telemetry.emit(
                    MetricsEvent(
                        "risk_block",
                        now,
                        self._session_id,
                        sym,
                        {"reason": "kill_switch", "signal": "close"},
                    )
                )
                return
            success = await self._exchange.close_position(sym, None)
            err_msg = self._exchange.pop_last_order_error() if not success else None
            if success:
                self._risk.record_order_submitted()
                self._entry_ts = None
            await self._emit_order_telemetry("close", success, now, error=err_msg)
            return

        ib_ms = self.cfg.interval_buy_ms or 0
        is_ms = self.cfg.interval_sell_ms or 0
        if ib_ms > 0 or is_ms > 0:
            if ib_ms > 0:
                buy_interval_s = ib_ms / 1000.0
                flat = abs(pos.size) < 1e-8
                buy_due = (now - self._last_interval_buy_ts) >= buy_interval_s
                if buy_due and (not self.cfg.interval_buy_flat_only or flat):
                    await self._place_interval_buy(sym, book, pos, cash, now)
                    self._last_interval_buy_ts = now
            if is_ms > 0:
                sell_interval_s = is_ms / 1000.0
                sell_due = (now - self._last_interval_sell_ts) >= sell_interval_s
                if sell_due:
                    await self._place_interval_sell(sym, book, pos, now)
                    self._last_interval_sell_ts = now
            return

        if not sig:
            return

        await self.telemetry.emit(
            MetricsEvent(
                "signal",
                now,
                self._session_id,
                sym,
                {
                    "type": sig.signal_type.value,
                    "reason": sig.reason,
                    "size": sig.size,
                },
            )
        )

        side = signal_to_side(sig)
        if side is None:
            return

        est = sig.size * mid_price(book.best_bid_px, book.best_ask_px)
        current_sym = notional_abs(pos.size, pos.current_price)
        projected_sym = projected_symbol_notional(current_sym, est)
        ok, reason = self._risk.allow_new_order(
            est_notional=est,
            symbol_notional=projected_sym,
        )
        if not ok:
            await self.telemetry.emit(
                MetricsEvent(
                    "risk_block",
                    now,
                    self._session_id,
                    sym,
                    {"reason": reason},
                )
            )
            return

        self._risk.record_order_submitted()
        success = await self._exchange.place_market_order(sym, side, sig.size)
        err_msg = self._exchange.pop_last_order_error() if not success else None
        if success:
            self._entry_ts = now
        await self._emit_order_telemetry("order", success, now, side=side.value, error=err_msg)

    async def _place_interval_buy(
        self,
        sym: str,
        book: OrderBookDepth,
        pos: PositionState,
        cash: float,
        now: float,
    ) -> bool:
        """One market BUY sized by ``order_size_usd``; respects risk gates."""
        order_usd = max(float(self.cfg.order_size_usd), MIN_ORDER_USD)
        if cash < 0.5 * order_usd:
            await self.telemetry.emit(
                MetricsEvent(
                    "risk_block",
                    now,
                    self._session_id,
                    sym,
                    {"reason": "insufficient_cash", "mode": "interval_buy", "required_order_usd": order_usd},
                )
            )
            return False
        mid = mid_price(book.best_bid_px, book.best_ask_px)
        sz = float(order_size_from_usd(order_usd, mid))
        est = sz * mid
        current_sym = notional_abs(pos.size, pos.current_price)
        projected_sym = projected_symbol_notional(current_sym, est)
        ok, reason = self._risk.allow_new_order(
            est_notional=est,
            symbol_notional=projected_sym,
        )
        if not ok:
            await self.telemetry.emit(
                MetricsEvent(
                    "risk_block",
                    now,
                    self._session_id,
                    sym,
                    {"reason": reason, "mode": "interval_buy"},
                )
            )
            return False

        await self.telemetry.emit(
            MetricsEvent(
                "signal",
                now,
                self._session_id,
                sym,
                {
                    "type": SignalType.LONG.value,
                    "reason": "interval_buy",
                    "size": sz,
                    "order_usd": order_usd,
                },
            )
        )
        self._risk.record_order_submitted()
        success = await self._exchange.place_market_order(sym, OrderSide.BUY, sz)
        err_msg = self._exchange.pop_last_order_error() if not success else None
        if success:
            self._entry_ts = now
        await self._emit_order_telemetry("order", success, now, side=OrderSide.BUY.value, error=err_msg)
        return success

    async def _place_interval_sell(
        self,
        sym: str,
        book: OrderBookDepth,
        pos: PositionState,
        now: float,
    ) -> bool:
        """One market SELL per interval; reduce long exposure first."""
        if pos.size <= 1e-8:
            await self.telemetry.emit(
                MetricsEvent(
                    "risk_block",
                    now,
                    self._session_id,
                    sym,
                    {"reason": "no_long_position", "mode": "interval_sell"},
                )
            )
            return False
        mid = mid_price(book.best_bid_px, book.best_ask_px)
        order_usd = max(float(self.cfg.order_size_usd), MIN_ORDER_USD)
        target_sz = float(order_size_from_usd(order_usd, mid))
        sz = min(abs(pos.size), target_sz)
        await self.telemetry.emit(
            MetricsEvent(
                "signal",
                now,
                self._session_id,
                sym,
                {
                    "type": SignalType.SHORT.value,
                    "reason": "interval_sell",
                    "size": sz,
                    "order_usd": order_usd,
                },
            )
        )
        self._risk.record_order_submitted()
        success = await self._exchange.place_market_order(sym, OrderSide.SELL, sz)
        err_msg = self._exchange.pop_last_order_error() if not success else None
        await self._emit_order_telemetry("order", success, now, side=OrderSide.SELL.value, error=err_msg)
        return success

    def _merge_position_clock(self, raw: PositionState) -> PositionState:
        if self._entry_ts is not None:
            return PositionState(
                symbol=raw.symbol,
                size=raw.size,
                entry_price=raw.entry_price,
                current_price=raw.current_price,
                opened_at=self._entry_ts,
            )
        return raw

    async def _emit_order_telemetry(
        self,
        kind: str,
        success: bool,
        now: float,
        side: str | None = None,
        error: str | None = None,
    ) -> None:
        data: dict[str, Any] = {"success": success}
        if side:
            data["side"] = side
        if error:
            data["error"] = error
        await self.telemetry.emit(
            MetricsEvent(kind, now, self._session_id, self.cfg.symbol, data)
        )
