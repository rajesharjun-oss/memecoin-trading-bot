from __future__ import annotations

import itertools
import threading
import time
from collections import Counter
from typing import Any

import httpx

from .telemetry import http_client


class RpcError(RuntimeError):
    """Raised for transport, protocol, or Solana RPC errors."""


_RPC_PACING_LOCK = threading.Lock()
_RPC_NEXT_REQUEST_AT = 0.0
_RPC_MIN_INTERVAL_SECONDS = 0.125  # Shared process ceiling of eight requests per second.


def _pace_rpc_request() -> None:
    """Coordinate independent clients so local workers do not burst a shared RPC key."""
    global _RPC_NEXT_REQUEST_AT
    with _RPC_PACING_LOCK:
        now = time.monotonic()
        delay = max(0.0, _RPC_NEXT_REQUEST_AT - now)
        if delay:
            time.sleep(delay)
        _RPC_NEXT_REQUEST_AT = max(_RPC_NEXT_REQUEST_AT, now) + _RPC_MIN_INTERVAL_SECONDS


def _retry_after_seconds(response: httpx.Response) -> float | None:
    value = response.headers.get("Retry-After")
    if not value:
        return None
    try:
        parsed = float(value)
    except ValueError:
        return None
    return min(5.0, max(0.0, parsed))


class RpcClient:
    def __init__(self, url: str, *, timeout_seconds: float = 12.0) -> None:
        self._url = url
        self._ids = itertools.count(1)
        self._request_counts: Counter[str] = Counter()
        self._request_counts_lock = threading.Lock()
        self._client = http_client(
            "rpc",
            timeout=httpx.Timeout(timeout_seconds, connect=5.0),
            limits=httpx.Limits(max_connections=10, max_keepalive_connections=5),
            follow_redirects=False,
            headers={"Content-Type": "application/json", "User-Agent": "memecoin-bot/0.1"},
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> RpcClient:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def request_counts(self) -> dict[str, int]:
        """Return outbound HTTP attempts by RPC method, including retries."""
        with self._request_counts_lock:
            return dict(self._request_counts)

    def call(self, method: str, params: list[Any], *, attempts: int = 3) -> Any:
        payload = {"jsonrpc": "2.0", "id": next(self._ids), "method": method, "params": params}
        last_error: Exception | None = None
        for attempt in range(attempts):
            response: httpx.Response | None = None
            try:
                _pace_rpc_request()
                with self._request_counts_lock:
                    self._request_counts[method] += 1
                response = self._client.post(self._url, json=payload)
                if response.status_code == 429 or response.status_code >= 500:
                    raise RpcError(f"RPC temporarily unavailable (HTTP {response.status_code})")
                response.raise_for_status()
                if len(response.content) > 5_000_000:
                    raise RpcError("RPC response exceeded the 5 MB safety limit")
                body = response.json()
                if not isinstance(body, dict) or body.get("jsonrpc") != "2.0":
                    raise RpcError("RPC returned an invalid JSON-RPC response")
                if body.get("error") is not None:
                    raise RpcError(f"RPC {method} failed: {body['error']!r}")
                if "result" not in body:
                    raise RpcError(f"RPC {method} omitted result")
                return body["result"]
            except (httpx.HTTPError, ValueError, RpcError) as exc:
                last_error = exc
                if attempt + 1 == attempts:
                    break
                delay = 0.5 * (2**attempt)
                if response is not None and response.status_code == 429:
                    retry_after = _retry_after_seconds(response)
                    if retry_after is not None:
                        delay = max(delay, retry_after)
                time.sleep(min(delay, 5.0))
        raise RpcError(f"RPC {method} failed after {attempts} attempts") from last_error
