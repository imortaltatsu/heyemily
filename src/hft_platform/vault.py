"""Envelope-style encryption at rest using Fernet (single master key)."""

from __future__ import annotations

import logging
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken

logger = logging.getLogger(__name__)

_DEV_KEY_FILE = Path(__file__).resolve().parents[2] / "data" / ".dev_fernet_key"


class KeyVault:
    def __init__(self, master_key_b64: str) -> None:
        self._fernet = Fernet(
            master_key_b64.encode() if isinstance(master_key_b64, str) else master_key_b64
        )

    def encrypt(self, plaintext: str) -> str:
        return self._fernet.encrypt(plaintext.encode()).decode()

    def decrypt(self, ciphertext: str) -> str:
        try:
            return self._fernet.decrypt(ciphertext.encode()).decode()
        except InvalidToken as e:
            raise ValueError("decryption failed") from e


_vault: KeyVault | None = None


def _load_or_create_dev_key() -> str:
    _DEV_KEY_FILE.parent.mkdir(parents=True, exist_ok=True)
    if _DEV_KEY_FILE.exists():
        return _DEV_KEY_FILE.read_text().strip()
    key = Fernet.generate_key().decode()
    _DEV_KEY_FILE.write_text(key)
    logger.warning(
        "Created dev-only Fernet key at %s — set MASTER_ENCRYPTION_KEY in production",
        _DEV_KEY_FILE,
    )
    return key


def get_vault(master_key_b64: str) -> KeyVault:
    global _vault
    if _vault is None:
        key = master_key_b64.strip() if master_key_b64 else ""
        if not key:
            key = _load_or_create_dev_key()
        _vault = KeyVault(key)
    return _vault
