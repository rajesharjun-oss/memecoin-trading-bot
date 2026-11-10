from __future__ import annotations

import json
import os
from pathlib import Path

from solders.keypair import Keypair


class WalletError(RuntimeError):
    """Raised when the dedicated signing key cannot be loaded safely."""


def load_keypair(path: Path | None) -> Keypair:
    if path is None:
        raise WalletError("SOLANA_KEYPAIR_PATH is required for building or executing trades")
    if not path.is_file():
        raise WalletError(f"Keypair file does not exist: {path}")
    if path.stat().st_size > 4_096:
        raise WalletError("Keypair file is unexpectedly large")
    if os.name != "nt" and path.stat().st_mode & 0o077:
        raise WalletError("Keypair file must not be readable or writable by group/other users")
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise WalletError("Keypair file must be a valid Solana CLI JSON keypair") from exc
    if (
        not isinstance(raw, list)
        or len(raw) != 64
        or any(not isinstance(item, int) or not 0 <= item <= 255 for item in raw)
    ):
        raise WalletError("Keypair file must contain exactly 64 byte values")
    try:
        return Keypair.from_bytes(bytes(raw))
    except ValueError as exc:
        raise WalletError("Keypair bytes are invalid") from exc
