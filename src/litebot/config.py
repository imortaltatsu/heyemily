"""YAML config for lite HFT worker."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


@dataclass
class RiskConfig:
    max_orders_per_second: float = 5.0
    max_open_notional_usd: float = 5000.0
    max_position_per_symbol_usd: float = 2000.0
    max_consecutive_losses: int = 8
    max_daily_realized_loss_usd: float = 500.0
    kill_switch: bool = False


@dataclass
class TelemetryConfig:
    push_url: str | None = None
    push_token: str | None = None
    session_id: str | None = None
    buffer_max: int = 500
    # Per-tick JSON telemetry is expensive; disable in latency-sensitive runs.
    emit_tick_events: bool = True


@dataclass
class LiteBotConfig:
    name: str = "lite_hft"
    symbol: str = "BTC"
    testnet: bool = True
    loop_interval_ms: int = 100
    hold_timeout_ms: int = 800
    imbalance_threshold: float = 0.35
    micro_gap_min_bps: float = 1.0
    order_size_usd: float = 50.0
    depth_levels: int = 5
    cooldown_ms: int = 150
    # If set (e.g. 1000), places a market BUY on that wall-clock interval (see engine).
    # Micro-arb LONG/SHORT entries are disabled; CLOSE signals from the strategy still run.
    interval_buy_ms: int | None = None
    # If true, interval buys only fire when flat. If false, buys can stack each interval.
    interval_buy_flat_only: bool = False
    # If set (e.g. 1000), places a market SELL on that wall-clock interval.
    # By default sells reduce an existing long position and do not force shorts.
    interval_sell_ms: int | None = None
    leverage: int = 3
    risk: RiskConfig = field(default_factory=RiskConfig)
    telemetry: TelemetryConfig = field(default_factory=TelemetryConfig)

    def validate(self) -> None:
        if self.loop_interval_ms < 20:
            raise ValueError("loop_interval_ms must be >= 20")
        if self.hold_timeout_ms < 50:
            raise ValueError("hold_timeout_ms must be >= 50")
        if not 0 < self.imbalance_threshold <= 1:
            raise ValueError("imbalance_threshold must be in (0, 1]")
        if self.interval_buy_ms is not None:
            if self.interval_buy_ms < 50:
                raise ValueError("interval_buy_ms must be >= 50 when set")
            if self.interval_buy_ms > 86_400_000:
                raise ValueError("interval_buy_ms too large (max 24h)")
        if self.interval_sell_ms is not None:
            if self.interval_sell_ms < 50:
                raise ValueError("interval_sell_ms must be >= 50 when set")
            if self.interval_sell_ms > 86_400_000:
                raise ValueError("interval_sell_ms too large (max 24h)")
        if self.leverage < 1:
            raise ValueError("leverage must be >= 1")


def _parse_optional_positive_int(v: Any) -> int | None:
    if v is None or v is False:
        return None
    i = int(v)
    return i if i > 0 else None


def _build_from_mapping(raw: dict[str, Any]) -> LiteBotConfig:
    raw = dict(raw)
    risk_raw = raw.pop("risk", {}) or {}
    tel_raw = raw.pop("telemetry", {}) or {}

    risk = RiskConfig(
        max_orders_per_second=float(risk_raw.get("max_orders_per_second", 5.0)),
        max_open_notional_usd=float(risk_raw.get("max_open_notional_usd", 5000.0)),
        max_position_per_symbol_usd=float(
            risk_raw.get("max_position_per_symbol_usd", 2000.0)
        ),
        max_consecutive_losses=int(risk_raw.get("max_consecutive_losses", 8)),
        max_daily_realized_loss_usd=float(
            risk_raw.get("max_daily_realized_loss_usd", 500.0)
        ),
        kill_switch=bool(risk_raw.get("kill_switch", False)),
    )
    telemetry = TelemetryConfig(
        push_url=tel_raw.get("push_url"),
        push_token=tel_raw.get("push_token"),
        session_id=tel_raw.get("session_id"),
        buffer_max=int(tel_raw.get("buffer_max", 500)),
        emit_tick_events=bool(tel_raw.get("emit_tick_events", True)),
    )

    cfg = LiteBotConfig(
        name=str(raw.get("name", "lite_hft")),
        symbol=str(raw.get("symbol", "BTC")),
        testnet=bool(raw.get("testnet", True)),
        loop_interval_ms=int(raw.get("loop_interval_ms", 100)),
        hold_timeout_ms=int(raw.get("hold_timeout_ms", 800)),
        imbalance_threshold=float(raw.get("imbalance_threshold", 0.35)),
        micro_gap_min_bps=float(raw.get("micro_gap_min_bps", 1.0)),
        order_size_usd=float(raw.get("order_size_usd", 50.0)),
        depth_levels=int(raw.get("depth_levels", 5)),
        cooldown_ms=int(raw.get("cooldown_ms", 150)),
        interval_buy_ms=_parse_optional_positive_int(raw.get("interval_buy_ms")),
        interval_buy_flat_only=bool(raw.get("interval_buy_flat_only", False)),
        interval_sell_ms=_parse_optional_positive_int(raw.get("interval_sell_ms")),
        leverage=int(raw.get("leverage", 3)),
        risk=risk,
        telemetry=telemetry,
    )
    cfg.validate()
    return cfg


def load_lite_config(path: Path | str) -> LiteBotConfig:
    p = Path(path)
    with open(p, encoding="utf-8") as f:
        raw: dict[str, Any] = yaml.safe_load(f) or {}
    return _build_from_mapping(raw)


def lite_config_from_dict(raw: dict[str, Any]) -> LiteBotConfig:
    """Build config from a dict (e.g. platform API merged with local defaults)."""
    return _build_from_mapping(raw)
