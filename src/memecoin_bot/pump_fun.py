from __future__ import annotations

from typing import Any

import httpx
from solders.pubkey import Pubkey


class PumpFunError(RuntimeError):
    """Raised when the fixed pump.fun developer API returns unsafe data."""


class PumpFunClient:
    """Bounded server-side client for pump.fun's official read-only API."""

    BASE_URL = "https://api.pump.fun/v1"

    def __init__(self, api_key: str, *, timeout_seconds: float = 8.0) -> None:
        if not api_key or len(api_key) > 512:
            raise PumpFunError("A valid pump.fun API key is required")
        self._client = httpx.Client(
            base_url=self.BASE_URL,
            headers={
                "Accept": "application/json",
                "Authorization": f"Bearer {api_key}",
                "User-Agent": "memecoin-bot/0.1",
            },
            timeout=httpx.Timeout(timeout_seconds, connect=4.0),
            limits=httpx.Limits(max_connections=2, max_keepalive_connections=1),
            follow_redirects=False,
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> PumpFunClient:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def new_callout_mints(self, *, limit: int = 20) -> list[str]:
        """Return recent Solana Pump coin mints from the official New callouts feed."""
        if not 1 <= limit <= 50:
            raise PumpFunError("pump.fun feed limit must be between 1 and 50")
        try:
            response = self._client.get(
                "/home-feed/new",
                params={"pageSize": limit, "chain": "solana", "updatesLimit": 1},
            )
        except httpx.RequestError as exc:
            raise PumpFunError("pump.fun transport is unavailable") from exc
        if len(response.content) > 2_000_000:
            raise PumpFunError("pump.fun response exceeded the 2 MB safety limit")
        try:
            response.raise_for_status()
            payload: Any = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise PumpFunError(f"pump.fun request failed with HTTP {response.status_code}") from exc
        if not isinstance(payload, dict) or not isinstance(payload.get("coins"), list):
            raise PumpFunError("pump.fun returned malformed feed data")

        mints: list[str] = []
        for item in payload["coins"]:
            if not isinstance(item, dict):
                continue
            if item.get("chain") != "solana" or item.get("program") != "pump":
                continue
            mint = item.get("coinMint")
            if not isinstance(mint, str):
                continue
            try:
                Pubkey.from_string(mint)
            except ValueError:
                continue
            if mint not in mints:
                mints.append(mint)
        return mints
