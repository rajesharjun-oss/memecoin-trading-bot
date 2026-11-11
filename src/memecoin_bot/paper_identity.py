"""Create an unfunded, volume-local identity required by the existing paper worker."""

from __future__ import annotations

import json
import os

from solders.keypair import Keypair

from .config import ConfigurationError, Settings


def ensure_paper_identity(settings: Settings) -> bool:
    """Create once with owner-only access; never print or replace key material."""
    if settings.live_trading_enabled:
        raise ConfigurationError("Paper identity requires live trading to be disabled")
    path = settings.keypair_path
    if path is None or path.parent != settings.state_path.parent:
        raise ConfigurationError("Paper identity must be beside the state journal")
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return False
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(list(bytes(Keypair())), handle, separators=(",", ":"))
    except BaseException:
        path.unlink(missing_ok=True)
        raise
    return True
