from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from threading import Lock
from typing import Any, Literal

import httpx
from solders.pubkey import Pubkey

from .telemetry import http_client

TIMEFRAME_SECONDS = {"1m": 60, "5m": 300, "15m": 900, "1h": 3600}
_GECKO_PERIOD = {"1m": ("minute", 1), "5m": ("minute", 5), "15m": ("minute", 15), "1h": ("hour", 1)}


class OhlcvError(RuntimeError):
    """A provider request or candle failed validation."""


class OhlcvRateLimited(OhlcvError):
    """The public OHLCV quota is temporarily exhausted."""

    def __init__(self, message: str, *, retry_after_seconds: int = 60) -> None:
        super().__init__(message)
        self.retry_after_seconds = retry_after_seconds


@dataclass(frozen=True, slots=True)
class PoolCandle:
    mint: str
    pool: str
    timeframe: str
    opened_at: int
    open: str
    high: str
    low: str
    close: str
    volume_usd: str


@dataclass(frozen=True, slots=True)
class PoolMarketObservation:
    mint: str
    pool: str
    price_usd: str
    liquidity_usd: str
    volume_24h_usd: str | None
    price_change_24h: str | None
    pair_created_at: int | None


_CACHE_LOCK = Lock()
_CACHE: dict[tuple[str, str, str, str, int, int | None], tuple[float, list[PoolCandle]]] = {}
_REQUEST_LOCK = Lock()
_NEXT_REQUEST_AT = 0.0
PUBLIC_REQUEST_INTERVAL_SECONDS = 15.0  # At most four calls/minute per process.
TRIAL_REQUEST_INTERVAL_SECONDS = 10.0  # Cautious six-call trial; revert on HTTP 429.
MINUTE_CACHE_TTL_SECONDS = 20.0
OTHER_CACHE_TTL_SECONDS = 45.0


def _pace_public_request(interval_seconds: float = PUBLIC_REQUEST_INTERVAL_SECONDS) -> None:
    global _NEXT_REQUEST_AT
    with _REQUEST_LOCK:
        now = time.monotonic()
        slot = max(now, _NEXT_REQUEST_AT)
        _NEXT_REQUEST_AT = slot + interval_seconds
    if slot > now:
        time.sleep(slot - now)


def _positive(value: object, *, allow_zero: bool = False) -> Decimal:
    if isinstance(value, bool):
        raise OhlcvError("Provider returned a nonnumeric OHLCV value")
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise OhlcvError("Provider returned a nonnumeric OHLCV value") from exc
    if not number.is_finite() or number < 0 or (not allow_zero and number == 0):
        raise OhlcvError("Provider returned an invalid OHLCV value")
    return number


def parse_ohlcv(
    payload: object, *, mint: str, pool: str, timeframe: str, now: int
) -> list[PoolCandle]:
    if timeframe not in TIMEFRAME_SECONDS:
        raise OhlcvError("Unsupported candle timeframe")
    if not isinstance(payload, dict):
        raise OhlcvError("Provider returned malformed OHLCV data")
    data = payload.get("data")
    attributes = data.get("attributes") if isinstance(data, dict) else None
    rows = attributes.get("ohlcv_list") if isinstance(attributes, dict) else None
    if not isinstance(rows, list):
        raise OhlcvError("Provider omitted OHLCV candles")
    meta = payload.get("meta")
    if not isinstance(meta, dict):
        raise OhlcvError("Provider omitted pool token identity")
    addresses = {
        value.get("address")
        for key in ("base", "quote")
        if isinstance((value := meta.get(key)), dict)
    }
    if mint not in addresses:
        raise OhlcvError("Provider pool does not contain the requested token")
    seconds = TIMEFRAME_SECONDS[timeframe]
    unique: dict[int, PoolCandle] = {}
    conflicted: set[int] = set()
    for row in rows:
        if not isinstance(row, list) or len(row) != 6 or not isinstance(row[0], int):
            raise OhlcvError("Provider returned a malformed candle")
        stamp = row[0]
        if stamp < 0 or stamp % seconds:
            raise OhlcvError("Provider returned a misaligned candle timestamp")
        if stamp + seconds > now:
            continue  # Incomplete candles must never become signals or replay data.
        opened, high, low, closed = (_positive(value) for value in row[1:5])
        volume = _positive(row[5], allow_zero=True)
        if high < max(opened, closed, low) or low > min(opened, closed, high):
            raise OhlcvError("Provider returned inconsistent OHLC prices")
        candle = PoolCandle(
            mint,
            pool,
            timeframe,
            stamp,
            str(opened),
            str(high),
            str(low),
            str(closed),
            str(volume),
        )
        if stamp in conflicted:
            continue
        previous = unique.get(stamp)
        if previous is not None and previous != candle:
            # No version of this interval is trustworthy. Keep the other
            # intervals; downstream signal/replay gates reject recent gaps.
            unique.pop(stamp)
            conflicted.add(stamp)
            continue
        unique[stamp] = candle
    if conflicted and not unique:
        raise OhlcvError("Provider returned only conflicting duplicate candles")
    return [unique[stamp] for stamp in sorted(unique)]


def parse_pool_markets(
    payload: object, *, requested: dict[str, str]
) -> list[PoolMarketObservation]:
    """Accept only exact Solana pool/token matches from the fixed provider."""
    if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
        raise OhlcvError("Provider returned malformed pool market data")
    output: list[PoolMarketObservation] = []
    for row in payload["data"]:
        if not isinstance(row, dict):
            continue
        attrs = row.get("attributes")
        relationships = row.get("relationships")
        if not isinstance(attrs, dict) or not isinstance(relationships, dict):
            continue
        pool = attrs.get("address")
        if not isinstance(pool, str) or row.get("id") != f"solana_{pool}":
            continue
        token_ids: dict[str, str] = {}
        for token_side in ("base_token", "quote_token"):
            relationship = relationships.get(token_side)
            data = relationship.get("data") if isinstance(relationship, dict) else None
            token_id = data.get("id") if isinstance(data, dict) else None
            if isinstance(token_id, str):
                token_ids[token_side] = token_id
        volumes = attrs.get("volume_usd")
        changes = attrs.get("price_change_percentage")
        created_raw = attrs.get("pool_created_at")
        try:
            created_time = (
                datetime.fromisoformat(created_raw.replace("Z", "+00:00"))
                if isinstance(created_raw, str)
                else None
            )
            created = (
                int(created_time.timestamp() * 1_000)
                if created_time is not None and created_time.tzinfo is not None
                else None
            )
        except (ValueError, OverflowError):
            created = None
        for mint, expected_pool in requested.items():
            if pool != expected_pool or f"solana_{mint}" not in token_ids.values():
                continue
            side = "base" if token_ids.get("base_token") == f"solana_{mint}" else "quote"
            try:
                price = _positive(attrs.get(f"{side}_token_price_usd"))
                liquidity = _positive(attrs.get("reserve_in_usd"), allow_zero=True)
                volume = (
                    _positive(volumes.get("h24"), allow_zero=True)
                    if isinstance(volumes, dict) and volumes.get("h24") is not None
                    else None
                )
                change = (
                    Decimal(str(changes.get("h24")))
                    if isinstance(changes, dict) and changes.get("h24") is not None
                    else None
                )
                if change is not None and not change.is_finite():
                    continue
            except (OhlcvError, InvalidOperation, ValueError):
                continue
            output.append(
                PoolMarketObservation(
                    mint,
                    pool,
                    str(price),
                    str(liquidity),
                    str(volume) if volume is not None else None,
                    str(change) if change is not None else None,
                    created,
                )
            )
    return output


class GeckoTerminalOhlcvClient:
    """Fixed-host, pool-specific OHLCV provider; never accepts a URL."""

    def __init__(
        self,
        *,
        client: httpx.Client | None = None,
        request_interval_seconds: float = PUBLIC_REQUEST_INTERVAL_SECONDS,
        provider: Literal["geckoterminal", "coingecko-demo"] = "geckoterminal",
        demo_api_key: str | None = None,
    ) -> None:
        if request_interval_seconds not in (
            PUBLIC_REQUEST_INTERVAL_SECONDS,
            TRIAL_REQUEST_INTERVAL_SECONDS,
        ):
            raise ValueError("Unsupported public OHLCV request pace")
        if provider not in ("geckoterminal", "coingecko-demo"):
            raise ValueError("Unsupported OHLCV provider")
        if provider == "coingecko-demo" and not demo_api_key:
            raise ValueError("CoinGecko Demo backfill requires COINGECKO_DEMO_API_KEY")
        self._owned = client is None
        self.provider = provider
        self.last_call_cache_hit = False
        self.request_interval_seconds = request_interval_seconds
        base_url = (
            "https://api.coingecko.com"
            if provider == "coingecko-demo"
            else "https://api.geckoterminal.com"
        )
        headers = (
            {"Accept": "application/json", "x-cg-demo-api-key": demo_api_key or ""}
            if provider == "coingecko-demo"
            else {"Accept": "application/json;version=20230203"}
        )
        self._headers = headers
        self._client = client or http_client(
            "ohlcv",
            base_url=base_url,
            headers=headers,
            timeout=httpx.Timeout(12.0, connect=4.0),
            follow_redirects=False,
        )

    def __enter__(self) -> GeckoTerminalOhlcvClient:
        return self

    def __exit__(self, *_: object) -> None:
        if self._owned:
            self._client.close()

    def candles(
        self,
        mint: str,
        pool: str,
        timeframe: str,
        *,
        limit: int = 200,
        now: int | None = None,
        before_timestamp: int | None = None,
    ) -> list[PoolCandle]:
        self.last_call_cache_hit = False
        if timeframe not in _GECKO_PERIOD or not 1 <= limit <= 1000:
            raise OhlcvError("Invalid OHLCV request")
        current = now if now is not None else int(time.time())
        if before_timestamp is not None and (
            type(before_timestamp) is not int or not 0 < before_timestamp <= current
        ):
            raise OhlcvError("Invalid historical OHLCV cursor")
        try:
            Pubkey.from_string(mint)
            Pubkey.from_string(pool)
        except ValueError as exc:
            raise OhlcvError("Invalid Solana mint or pool address") from exc
        period, aggregate = _GECKO_PERIOD[timeframe]
        cache_key = (self.provider, mint, pool, timeframe, limit, before_timestamp)
        if self._owned and now is None:
            with _CACHE_LOCK:
                cached = _CACHE.get(cache_key)
            cache_ttl = MINUTE_CACHE_TTL_SECONDS if timeframe == "1m" else OTHER_CACHE_TTL_SECONDS
            if cached is not None and time.monotonic() - cached[0] < cache_ttl:
                self.last_call_cache_hit = True
                return list(cached[1])
        prefix = "/api/v3/onchain" if self.provider == "coingecko-demo" else "/api/v2"
        path = f"{prefix}/networks/solana/pools/{pool}/ohlcv/{period}"
        params: dict[str, Any] = {
            "aggregate": aggregate,
            "limit": limit,
            "currency": "usd",
            "token": mint,
            "include_empty_intervals": "false",
        }
        if before_timestamp is not None:
            params["before_timestamp"] = before_timestamp
        for attempt in range(3):
            try:
                if self._owned:
                    _pace_public_request(self.request_interval_seconds)
                response = self._client.get(path, params=params, headers=self._headers)
            except httpx.HTTPError as exc:
                if attempt == 2:
                    raise OhlcvError("OHLCV provider is unavailable") from exc
                time.sleep(0.25 * (attempt + 1))
                continue
            if len(response.content) > 2_000_000:
                raise OhlcvError("OHLCV response exceeded size limit")
            if response.status_code == 429:
                retry_after = response.headers.get("Retry-After", "")
                seconds = max(60, min(300, int(retry_after))) if retry_after.isdigit() else 60
                raise OhlcvRateLimited(
                    "OHLCV provider rate limit reached", retry_after_seconds=seconds
                )
            if response.status_code in (500, 502, 503, 504) and attempt < 2:
                time.sleep(0.5 * (attempt + 1))
                continue
            if response.status_code != 200:
                raise OhlcvError(f"OHLCV provider returned HTTP {response.status_code}")
            try:
                payload = response.json()
            except ValueError as exc:
                raise OhlcvError("OHLCV provider returned invalid JSON") from exc
            candles = parse_ohlcv(
                payload,
                mint=mint,
                pool=pool,
                timeframe=timeframe,
                # The public request may wait for its rate-limit slot. A bar
                # that closes during that wait is closed when we receive it.
                # Explicit historical cutoffs must retain their caller time.
                now=current if now is not None else int(time.time()),
            )
            if before_timestamp is not None:
                candles = [candle for candle in candles if candle.opened_at < before_timestamp]
            if self._owned and now is None:
                with _CACHE_LOCK:
                    if len(_CACHE) >= 128:
                        _CACHE.clear()
                    _CACHE[cache_key] = (time.monotonic(), list(candles))
            return candles
        raise OhlcvError("OHLCV provider exhausted retries")

    def pool_markets(self, requested: dict[str, str]) -> list[PoolMarketObservation]:
        """Fetch exact-pool liquidity for a bounded research cohort in one request."""
        if self.provider != "geckoterminal" or not 1 <= len(requested) <= 10:
            raise OhlcvError("Invalid pool market request")
        for mint, pool in requested.items():
            try:
                Pubkey.from_string(mint)
                Pubkey.from_string(pool)
            except ValueError as exc:
                raise OhlcvError("Invalid Solana mint or pool address") from exc
        pools = list(dict.fromkeys(requested.values()))
        path = f"/api/v2/networks/solana/pools/multi/{','.join(pools)}"
        try:
            if self._owned:
                _pace_public_request(self.request_interval_seconds)
            response = self._client.get(path, headers=self._headers)
        except httpx.HTTPError as exc:
            raise OhlcvError("Pool market provider is unavailable") from exc
        if len(response.content) > 500_000:
            raise OhlcvError("Pool market response exceeded size limit")
        if response.status_code == 429:
            retry_after = response.headers.get("Retry-After", "")
            seconds = max(60, min(300, int(retry_after))) if retry_after.isdigit() else 60
            raise OhlcvRateLimited(
                "Pool market provider rate limit reached", retry_after_seconds=seconds
            )
        if response.status_code != 200:
            raise OhlcvError(f"Pool market provider returned HTTP {response.status_code}")
        try:
            payload = response.json()
        except ValueError as exc:
            raise OhlcvError("Pool market provider returned invalid JSON") from exc
        return parse_pool_markets(payload, requested=requested)
