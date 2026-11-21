"""Bounded, read-only Solana pool log coverage probe.

This research tool never decodes prices or supplies candles to the bot. A log
mention is a transaction event, not necessarily a swap. Run explicitly with
``python -m memecoin_bot.shadow_swap_stream`` after installing the research extra.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import binascii
import json
import os
import time
from collections import defaultdict
from dataclasses import dataclass
from decimal import Decimal
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import httpx
from dotenv import load_dotenv
from solders.pubkey import Pubkey

from .ohlcv import parse_ohlcv
from .pump_amm_shadow import PoolIdentity, SwapObservation, decode_program_logs, parse_pool_account


class ProbeError(RuntimeError):
    """The probe cannot produce a complete, trustworthy comparison."""


@dataclass(frozen=True, slots=True)
class PoolResult:
    pool: str
    stream_events: int
    reference_events: int | None
    missed_events: int | None
    extra_events: int | None
    coverage_pct: float | None
    reference_complete: bool


def websocket_url(rpc_url: str) -> str:
    """Use only the configured Helius mainnet endpoint; never display this URL."""
    parsed = urlsplit(rpc_url)
    if (
        parsed.scheme != "https"
        or parsed.hostname != "mainnet.helius-rpc.com"
        or parsed.port is not None
        or parsed.username is not None
        or parsed.password is not None
        or not parsed.query.startswith("api-key=")
        or not parsed.query.removeprefix("api-key=")
        or "&" in parsed.query
    ):
        raise ProbeError("A Helius mainnet RPC URL is required")
    return urlunsplit(("wss", parsed.netloc, parsed.path or "/", parsed.query, ""))


def _rpc_signatures(client: httpx.Client, rpc_url: str, pool: str, before: str | None) -> list[str]:
    config: dict[str, str | int] = {"commitment": "confirmed", "limit": 1000}
    if before is not None:
        config["before"] = before
    response = client.post(
        rpc_url,
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "getSignaturesForAddress",
            "params": [pool, config],
        },
    )
    if response.status_code != 200 or len(response.content) > 1_000_000:
        raise ProbeError("Signature reference request failed")
    try:
        payload = response.json()
    except ValueError:
        raise ProbeError("Signature reference response was invalid") from None
    if not isinstance(payload, dict) or not isinstance(payload.get("result"), list):
        raise ProbeError("Signature reference response was invalid")
    signatures: list[str] = []
    for row in payload["result"]:
        if not isinstance(row, dict) or not isinstance(row.get("signature"), str):
            raise ProbeError("Signature reference response was invalid")
        signatures.append(row["signature"])
    return signatures


def _anchor(client: httpx.Client, rpc_url: str, pool: str) -> str:
    signatures = _rpc_signatures(client, rpc_url, pool, None)
    if not signatures:
        raise ProbeError("Pool has no reference signatures")
    return signatures[0]


def _pump_identity(client: httpx.Client, rpc_url: str, pool: str, mint: str) -> PoolIdentity:
    """Bind an exact on-chain PumpSwap pool and mint before interpreting logs."""

    def result(method: str, params: list[Any]) -> Any:
        response = client.post(
            rpc_url, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
        )
        if response.status_code != 200 or len(response.content) > 500_000:
            raise ProbeError("Pool identity RPC request failed")
        payload = response.json()
        if not isinstance(payload, dict) or "error" in payload or "result" not in payload:
            raise ProbeError("Pool identity RPC response was invalid")
        return payload["result"]

    account = result("getAccountInfo", [pool, {"encoding": "base64"}])
    value = account.get("value") if isinstance(account, dict) else None
    encoded = value.get("data") if isinstance(value, dict) else None
    if (
        not isinstance(value, dict)
        or not isinstance(encoded, list)
        or len(encoded) != 2
        or encoded[1] != "base64"
    ):
        raise ProbeError("Pool account encoding was invalid")
    try:
        raw = base64.b64decode(encoded[0], validate=True)
    except (binascii.Error, ValueError):
        raise ProbeError("Pool account encoding was invalid") from None
    supply = result("getTokenSupply", [mint])
    supply_value = supply.get("value") if isinstance(supply, dict) else None
    decimals = supply_value.get("decimals") if isinstance(supply_value, dict) else None
    if type(decimals) is not int:
        raise ProbeError("Token decimals were unavailable")
    return parse_pool_account(
        raw, owner=value.get("owner", ""), pool=pool, target_mint=mint, target_decimals=decimals
    )


def _reference_window(
    client: httpx.Client, rpc_url: str, pool: str, start: str, end: str
) -> tuple[set[str], bool, int]:
    """Page newest-first to the start anchor, with a hard five-call limit."""
    collected: list[str] = []
    before: str | None = None
    calls = 0
    for _ in range(5):
        page = _rpc_signatures(client, rpc_url, pool, before)
        calls += 1
        collected.extend(page)
        if start in page or len(page) < 1000:
            break
        before = page[-1]
    if start not in collected or end not in collected:
        return set(), False, calls
    end_index, start_index = collected.index(end), collected.index(start)
    if end_index >= start_index:
        return set(), False, calls
    return set(collected[end_index:start_index]), True, calls


def _notification(message: object, subscriptions: dict[int, str]) -> tuple[str, str] | None:
    if not isinstance(message, dict) or message.get("method") != "logsNotification":
        return None
    params = message.get("params")
    if not isinstance(params, dict) or type(params.get("subscription")) is not int:
        raise ProbeError("Malformed log notification")
    pool = subscriptions.get(params["subscription"])
    result = params.get("result")
    value = result.get("value") if isinstance(result, dict) else None
    signature = value.get("signature") if isinstance(value, dict) else None
    if pool is None or not isinstance(signature, str) or not signature:
        raise ProbeError("Malformed log notification")
    return pool, signature


def _message_bytes(raw: str | bytes) -> int:
    return len(raw.encode("utf-8")) if isinstance(raw, str) else len(raw)


def _record_trade_events(
    message: dict[str, Any], identity: PoolIdentity, trades: dict[str, tuple[SwapObservation, ...]]
) -> None:
    params = message.get("params")
    result = params.get("result") if isinstance(params, dict) else None
    value = result.get("value") if isinstance(result, dict) else None
    if not isinstance(value, dict) or not isinstance(value.get("signature"), str):
        raise ProbeError("Malformed trade log notification")
    if value.get("err") is not None:
        return
    signature = value["signature"]
    decoded = decode_program_logs(value.get("logs"), identity)
    if signature in trades and trades[signature] != decoded:
        raise ProbeError("Conflicting duplicate trade logs")
    trades[signature] = decoded


def _compare_native_prices(
    client: httpx.Client,
    identity: PoolIdentity,
    reference: set[str],
    trades: dict[str, tuple[SwapObservation, ...]],
) -> dict[str, Any]:
    """Compare event prices with Gecko's quote-token bars; never certify OHLCV."""
    pool = identity.pool
    response = client.get(
        f"https://api.geckoterminal.com/api/v2/networks/solana/pools/{pool}/ohlcv/minute",
        params={
            "aggregate": 1,
            "limit": 10,
            "currency": "token",
            "token": identity.target_mint,
            "include_empty_intervals": "false",
        },
        headers={"Accept": "application/json;version=20230203"},
    )
    if response.status_code != 200 or len(response.content) > 1_000_000:
        raise ProbeError("Native-price reference request failed")
    now = int(time.time())
    candles = parse_ohlcv(
        response.json(), mint=identity.target_mint, pool=pool, timeframe="1m", now=now
    )
    bars = {bar.opened_at: bar for bar in candles}
    matched = outside = without_bar = 0
    largest_outside_bps = Decimal(0)
    for signature in reference:
        for trade in trades.get(signature, ()):
            if trade.timestamp + 120 > now:
                continue  # Give the public provider a full minute to finalize.
            bar = bars.get(trade.timestamp // 60 * 60)
            if bar is None:
                without_bar += 1
                continue
            matched += 1
            low, high = Decimal(bar.low), Decimal(bar.high)
            if not low <= trade.price_sol <= high:
                outside += 1
                nearest = low if trade.price_sol < low else high
                largest_outside_bps = max(
                    largest_outside_bps, abs(trade.price_sol - nearest) * 10_000 / nearest
                )
    return {
        "status": "provisional_event_price_check" if matched else "insufficient_matched_events",
        "matched_events": matched,
        "outside_reference_range": outside,
        "events_without_reference_bar": without_bar,
        "largest_outside_bps": round(float(largest_outside_bps), 2),
        "note": "No volume or OHLC ordering validation; Gecko bars may later revise",
    }


def _compare(pool: str, stream: set[str], reference: set[str], complete: bool) -> PoolResult:
    if not complete:
        return PoolResult(pool, len(stream), None, None, None, None, False)
    missing = reference - stream
    extra = stream - reference  # May precede/follow the two reference anchors.
    return PoolResult(
        pool,
        len(stream),
        len(reference),
        len(missing),
        len(extra),
        round(100 * (len(reference) - len(missing)) / len(reference), 2) if reference else None,
        True,
    )


async def probe(
    rpc_url: str, pools: tuple[str, ...], duration: int, *, target_mint: str | None = None
) -> dict[str, Any]:
    """Observe only fixed pool mentions for up to five minutes, with bounded backfill."""
    from websockets.asyncio.client import connect  # Optional research dependency.

    if not 1 <= len(pools) <= 3 or not 10 <= duration <= 300 or (target_mint and len(pools) != 1):
        raise ProbeError("Invalid bounded research request")
    ws_url = websocket_url(rpc_url)
    with httpx.Client(timeout=10, follow_redirects=False, trust_env=False) as client:
        identity = _pump_identity(client, rpc_url, pools[0], target_mint) if target_mint else None
        http_calls = 2 if identity else 0
        observed: dict[str, set[str]] = defaultdict(set)
        trade_events: dict[str, tuple[SwapObservation, ...]] = {}
        received_bytes = 0
        started_at = int(time.time())
        async with connect(
            ws_url,
            open_timeout=10,
            ping_timeout=10,
            max_size=262_144,
            max_queue=8,
            compression=None,
        ) as socket:
            for index, pool in enumerate(pools, start=1):
                await socket.send(
                    json.dumps(
                        {
                            "jsonrpc": "2.0",
                            "id": index,
                            "method": "logsSubscribe",
                            "params": [{"mentions": [pool]}, {"commitment": "confirmed"}],
                        }
                    )
                )
            subscriptions: dict[int, str] = {}
            pending_notifications: list[dict[str, Any]] = []
            deadline = time.monotonic() + 15
            while len(subscriptions) < len(pools):
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise ProbeError("Log subscription was not acknowledged")
                raw = await asyncio.wait_for(socket.recv(), remaining)
                received_bytes += _message_bytes(raw)
                if received_bytes > 2_000_000:
                    raise ProbeError("Stream byte budget exceeded")
                message = json.loads(raw)
                if not isinstance(message, dict):
                    raise ProbeError("Malformed stream response")
                if message.get("error") is not None:
                    raise ProbeError("Log subscription was rejected")
                request_id, subscription_id = message.get("id"), message.get("result")
                if type(request_id) is int and type(subscription_id) is int:
                    if request_id not in range(1, len(pools) + 1):
                        raise ProbeError("Unexpected subscription response")
                    subscriptions[subscription_id] = pools[request_id - 1]
                else:
                    pending_notifications.append(message)
                    if len(pending_notifications) > 100:
                        raise ProbeError("Subscription acknowledgement backlog exceeded")

            for message in pending_notifications:
                event = _notification(message, subscriptions)
                if event is not None:
                    observed[event[0]].add(event[1])
                    if identity is not None:
                        _record_trade_events(message, identity, trade_events)

            # Subscribe first, then anchor the reference. Events arriving while
            # these calls run remain in the socket queue and can be drained.
            starts = {
                pool: await asyncio.to_thread(_anchor, client, rpc_url, pool) for pool in pools
            }
            http_calls += len(pools)
            stop_at = time.monotonic() + duration
            while time.monotonic() < stop_at:
                try:
                    raw = await asyncio.wait_for(socket.recv(), min(1, stop_at - time.monotonic()))
                except TimeoutError:
                    continue
                received_bytes += _message_bytes(raw)
                if received_bytes > 2_000_000:
                    raise ProbeError("Stream byte budget exceeded")
                event = _notification(json.loads(raw), subscriptions)
                if event is not None:
                    observed[event[0]].add(event[1])
                    if identity is not None:
                        _record_trade_events(json.loads(raw), identity, trade_events)
                if sum(map(len, observed.values())) > 2000:
                    raise ProbeError("Stream event budget exceeded")
            ends = {pool: await asyncio.to_thread(_anchor, client, rpc_url, pool) for pool in pools}
            http_calls += len(pools)
            # Allow notifications already in transit at the end anchor to arrive.
            linger_until = time.monotonic() + 2
            while time.monotonic() < linger_until:
                try:
                    raw = await asyncio.wait_for(socket.recv(), linger_until - time.monotonic())
                except TimeoutError:
                    break
                received_bytes += _message_bytes(raw)
                if received_bytes > 2_000_000:
                    raise ProbeError("Stream byte budget exceeded")
                event = _notification(json.loads(raw), subscriptions)
                if event is not None:
                    observed[event[0]].add(event[1])
                    if identity is not None:
                        _record_trade_events(json.loads(raw), identity, trade_events)

        results = []
        references: dict[str, set[str]] = {}
        for pool in pools:
            reference, complete, calls = _reference_window(
                client, rpc_url, pool, starts[pool], ends[pool]
            )
            http_calls += calls
            references[pool] = reference
            results.append(_compare(pool, observed[pool], reference, complete))
        native_price_check: dict[str, Any] | None = None
        gecko_http_calls = 0
        if identity is not None:
            if results[0].reference_complete and results[0].missed_events == 0:
                try:
                    gecko_http_calls = 1
                    native_price_check = _compare_native_prices(
                        client, identity, references[pools[0]], trade_events
                    )
                except Exception:
                    native_price_check = {"status": "reference_unavailable"}
            else:
                native_price_check = {"status": "undefined_incomplete_stream"}
        return {
            "started_at_utc_epoch": started_at,
            "duration_seconds": duration,
            "received_bytes": received_bytes,
            "estimated_stream_credits_at_20_per_mb": round(received_bytes * 20 / 1_000_000, 3),
            "http_rpc_calls": http_calls,
            "gecko_http_calls": gecko_http_calls,
            "results": [
                {
                    "pool": result.pool,
                    "stream_events": result.stream_events,
                    "reference_events": result.reference_events,
                    "missed_events": result.missed_events,
                    "extra_events": result.extra_events,
                    "coverage_pct": result.coverage_pct,
                    "reference_complete": result.reference_complete,
                }
                for result in results
            ],
            "native_price_check": native_price_check,
            "price_comparison": "event prices only; complete OHLCV remains unverified",
            "status": "research_only",
        }


def main() -> int:
    parser = argparse.ArgumentParser(description="Bounded read-only pool log coverage probe")
    parser.add_argument("--pool", action="append", required=True)
    parser.add_argument("--seconds", type=int, default=120)
    parser.add_argument("--mint", help="Optional PumpSwap token for native-price research")
    args = parser.parse_args()
    pools = tuple(dict.fromkeys(args.pool))
    if not 1 <= len(pools) <= 3 or not 10 <= args.seconds <= 300:
        parser.error("Choose 1-3 pools and 10-300 seconds")
    if args.mint is not None and len(pools) != 1:
        parser.error("Native-price research requires one exact pool")
    try:
        for pool in pools:
            Pubkey.from_string(pool)
        if args.mint is not None:
            Pubkey.from_string(args.mint)
    except ValueError:
        parser.error("Invalid Solana pool address")
    load_dotenv()
    try:
        print(
            json.dumps(
                asyncio.run(
                    probe(
                        os.getenv("SOLANA_RPC_URL", ""), pools, args.seconds, target_mint=args.mint
                    )
                )
            )
        )
    except Exception as exc:
        # Never print exception text: HTTP and WebSocket errors can embed the API key URL.
        print(json.dumps({"status": "failed", "error_type": type(exc).__name__}))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
