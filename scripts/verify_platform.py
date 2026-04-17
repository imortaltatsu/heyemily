#!/usr/bin/env python3
"""Smoke-test HFT platform auth + health in a fresh process (set DATABASE_URL before imports)."""
from __future__ import annotations

import os
import sys
from pathlib import Path


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    os.chdir(root)
    Path("data").mkdir(exist_ok=True)
    db = root / "data" / "verify_platform_auth.db"
    if db.exists():
        db.unlink()
    os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///./{db.relative_to(root).as_posix()}"

    from eth_account import Account
    from eth_account.messages import encode_defunct
    from fastapi.testclient import TestClient

    from hft_platform.main import app

    acct = Account.create()
    addr = acct.address

    with TestClient(app) as client:
        r = client.get("/api/health")
        r.raise_for_status()
        assert r.json().get("ok") is True

        r = client.post("/api/auth/challenge", json={"address": addr})
        r.raise_for_status()
        message = r.json()["message"]

        sig = Account.sign_message(encode_defunct(text=message), acct.key).signature
        sig_hex = sig.hex() if hasattr(sig, "hex") else str(sig)
        if not sig_hex.startswith("0x"):
            sig_hex = "0x" + sig_hex

        r = client.post(
            "/api/auth/verify",
            json={"address": addr, "message": message, "signature": sig_hex},
        )
        r.raise_for_status()
        token = r.json()["access_token"]

        r = client.get("/api/auth/me", headers={"Authorization": f"Bearer {token}"})
        r.raise_for_status()
        body = r.json()
        assert body["wallet_address"] == addr.lower()
        assert "id" in body

    db.unlink(missing_ok=True)
    print("verify_platform: health + challenge + verify + me OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
