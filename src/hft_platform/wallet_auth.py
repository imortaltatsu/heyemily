from __future__ import annotations

import re
import secrets
from datetime import datetime, timedelta, timezone

from eth_account import Account
from eth_account.messages import encode_defunct

_NONCE_BYTES = 16
_ADDR = re.compile(r"^0x[a-fA-F0-9]{40}$")


def normalize_wallet_address(raw: str) -> str:
    s = raw.strip()
    if not _ADDR.match(s):
        msg = "Invalid EVM address"
        raise ValueError(msg)
    return s.lower()


def new_nonce_hex() -> str:
    return secrets.token_hex(_NONCE_BYTES)


def build_login_message(address_lower: str, nonce: str) -> str:
    issued = datetime.now(timezone.utc).isoformat()
    return (
        "Hyperbot HFT Platform sign-in\n\n"
        f"Wallet: {address_lower}\n"
        f"Nonce: {nonce}\n"
        f"Issued: {issued}\n"
    )


def verify_wallet_signature(address_lower: str, message: str, signature: str) -> bool:
    try:
        msg = encode_defunct(text=message)
        recovered = Account.recover_message(msg, signature=signature)
    except Exception:
        return False
    return recovered.lower() == address_lower


def challenge_ttl() -> datetime:
    return datetime.now(timezone.utc) + timedelta(minutes=10)
