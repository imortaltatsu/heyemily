#!/usr/bin/env python3
"""Lite HFT worker: local YAML or platform bootstrap (encrypted custodial key)."""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

import httpx
from dotenv import load_dotenv

load_dotenv()

# Repo layout: src/run_lite_worker.py -> add src to path
_SRC = Path(__file__).resolve().parent
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from litebot.asyncio_setup import install_fast_asyncio
from litebot.config import LiteBotConfig, lite_config_from_dict, load_lite_config
from litebot.engine import LiteHFTEngine


async def _bootstrap_config(
    api_base: str, session_id: str, worker_token: str
) -> tuple[LiteBotConfig, str]:
    url = f"{api_base.rstrip('/')}/api/bots/sessions/{session_id}/worker/bootstrap"
    async with httpx.AsyncClient(timeout=30.0) as client:
        r = await client.get(
            url,
            headers={"Authorization": f"Bearer {worker_token}"},
        )
        r.raise_for_status()
        data = r.json()
    pk = data["private_key"]
    cfg_dict = data.get("config") or {}
    if isinstance(cfg_dict, str):
        import json

        cfg_dict = json.loads(cfg_dict)
    cfg = lite_config_from_dict(cfg_dict)
    push = f"{api_base.rstrip('/')}/api/internal/telemetry"
    cfg.telemetry.push_url = push
    cfg.telemetry.push_token = worker_token
    cfg.telemetry.session_id = session_id
    return cfg, pk


async def _amain() -> None:
    p = argparse.ArgumentParser(description="Lite HFT worker")
    p.add_argument("--config", type=str, default=None, help="Path to lite YAML config")
    p.add_argument("--api-base", type=str, default=os.getenv("HFT_API_BASE", ""))
    p.add_argument("--session-id", type=str, default=os.getenv("HFT_SESSION_ID", ""))
    p.add_argument("--worker-token", type=str, default=os.getenv("HFT_WORKER_TOKEN", ""))
    args = p.parse_args()

    private_key = os.getenv("LITEBOT_PRIVATE_KEY", "").strip()

    if args.api_base and args.session_id and args.worker_token:
        cfg, private_key = await _bootstrap_config(
            args.api_base, args.session_id, args.worker_token
        )
    elif args.config:
        cfg = load_lite_config(args.config)
        if not private_key:
            print("Set LITEBOT_PRIVATE_KEY or use --api-base + session bootstrap.")
            raise SystemExit(1)
    else:
        print("Provide --config file or --api-base, --session-id, --worker-token")
        raise SystemExit(1)

    eng = LiteHFTEngine(cfg, private_key)
    try:
        await eng.run_forever()
    except KeyboardInterrupt:
        eng.stop()
    finally:
        await eng.shutdown()


def main() -> None:
    install_fast_asyncio()
    asyncio.run(_amain())


if __name__ == "__main__":
    main()
