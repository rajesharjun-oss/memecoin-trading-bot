from __future__ import annotations

from dataclasses import asdict, dataclass
from decimal import Decimal, InvalidOperation
from typing import Any

import httpx
from solders.pubkey import Pubkey


class MarketDataError(RuntimeError):
    """Raised when the fixed market-data provider returns unsafe data."""


@dataclass(frozen=True, slots=True)
class TokenMarket:
    mint: str
    name: str | None
    symbol: str | None
    dex_id: str | None
    pair_address: str | None
    price_usd: str | None
    liquidity_usd: str | None
    volume_24h_usd: str | None
    price_change_24h: str | None
    pair_created_at: int | None
    market_cap_usd: str | None = None
    fdv_usd: str | None = None
    volume_5m_usd: str | None = None
    volume_1h_usd: str | None = None
    volume_6h_usd: str | None = None
    price_change_5m: str | None = None
    price_change_1h: str | None = None
    price_change_6h: str | None = None
    txns_5m_buys: int = 0
    txns_5m_sells: int = 0
    txns_1h_buys: int = 0
    txns_1h_sells: int = 0
    txns_6h_buys: int = 0
    txns_6h_sells: int = 0
    txns_24h_buys: int = 0
    txns_24h_sells: int = 0
    boosts_active: int = 0

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _short_text(value: Any, *, limit: int) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = " ".join(value.split())
    return normalized[:limit] if normalized else None


def _decimal_text(value: Any) -> str | None:
    if value in (None, ""):
        return None
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    if not parsed.is_finite():
        return None
    return format(parsed, "f")


def _nested_decimal(data: dict[str, Any], key: str, nested: str) -> str | None:
    value = data.get(key)
    return _decimal_text(value.get(nested)) if isinstance(value, dict) else None


def _nested_count(data: dict[str, Any], period: str, side: str) -> int:
    txns = data.get("txns")
    period_value = txns.get(period) if isinstance(txns, dict) else None
    value = period_value.get(side) if isinstance(period_value, dict) else None
    return value if isinstance(value, int) and value >= 0 else 0


class DexScreenerClient:
    """Bounded client for DEX Screener's documented, keyless Solana endpoint."""

    BASE_URL = "https://api.dexscreener.com"

    def __init__(self, *, timeout_seconds: float = 8.0) -> None:
        self._client = httpx.Client(
            base_url=self.BASE_URL,
            headers={"Accept": "application/json", "User-Agent": "memecoin-bot/0.1"},
            timeout=httpx.Timeout(timeout_seconds, connect=4.0),
            limits=httpx.Limits(max_connections=4, max_keepalive_connections=2),
            follow_redirects=False,
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> DexScreenerClient:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def _get(self, path: str) -> httpx.Response:
        try:
            return self._client.get(path)
        except httpx.RequestError as exc:
            raise MarketDataError("DEX Screener transport is unavailable") from exc

    def tokens(self, mints: list[str]) -> dict[str, TokenMarket]:
        unique = list(dict.fromkeys(mints))
        if not unique:
            return {}
        if len(unique) > 30:
            raise MarketDataError("DEX Screener accepts at most 30 token addresses")
        for mint in unique:
            try:
                Pubkey.from_string(mint)
            except ValueError as exc:
                raise MarketDataError("Market-data request contains an invalid mint") from exc

        response = self._get(f"/tokens/v1/solana/{','.join(unique)}")
        if len(response.content) > 2_000_000:
            raise MarketDataError("DEX Screener response exceeded the 2 MB safety limit")
        try:
            response.raise_for_status()
            payload = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise MarketDataError(
                f"DEX Screener request failed with HTTP {response.status_code}"
            ) from exc
        if not isinstance(payload, list):
            raise MarketDataError("DEX Screener returned malformed token data")

        best: dict[str, tuple[Decimal, TokenMarket]] = {}
        requested = set(unique)
        for pair in payload:
            if not isinstance(pair, dict) or pair.get("chainId") != "solana":
                continue
            base_value = pair.get("baseToken")
            quote_value = pair.get("quoteToken")
            base: dict[str, Any] = base_value if isinstance(base_value, dict) else {}
            quote: dict[str, Any] = quote_value if isinstance(quote_value, dict) else {}
            base_address = base.get("address")
            quote_address = quote.get("address")
            candidate_mint = base_address if base_address in requested else quote_address
            token = base if candidate_mint == base_address else quote
            if not isinstance(candidate_mint, str) or candidate_mint not in requested:
                continue
            mint = candidate_mint
            liquidity_text = _nested_decimal(pair, "liquidity", "usd")
            liquidity = Decimal(liquidity_text or "0")
            market = TokenMarket(
                mint=mint,
                name=_short_text(token.get("name"), limit=80),
                symbol=_short_text(token.get("symbol"), limit=20),
                dex_id=_short_text(pair.get("dexId"), limit=40),
                pair_address=_short_text(pair.get("pairAddress"), limit=64),
                price_usd=_decimal_text(pair.get("priceUsd")),
                liquidity_usd=liquidity_text,
                volume_24h_usd=_nested_decimal(pair, "volume", "h24"),
                price_change_24h=_nested_decimal(pair, "priceChange", "h24"),
                pair_created_at=(
                    int(pair["pairCreatedAt"])
                    if isinstance(pair.get("pairCreatedAt"), int)
                    else None
                ),
                market_cap_usd=_decimal_text(pair.get("marketCap")),
                fdv_usd=_decimal_text(pair.get("fdv")),
                volume_5m_usd=_nested_decimal(pair, "volume", "m5"),
                volume_1h_usd=_nested_decimal(pair, "volume", "h1"),
                volume_6h_usd=_nested_decimal(pair, "volume", "h6"),
                price_change_5m=_nested_decimal(pair, "priceChange", "m5"),
                price_change_1h=_nested_decimal(pair, "priceChange", "h1"),
                price_change_6h=_nested_decimal(pair, "priceChange", "h6"),
                txns_5m_buys=_nested_count(pair, "m5", "buys"),
                txns_5m_sells=_nested_count(pair, "m5", "sells"),
                txns_1h_buys=_nested_count(pair, "h1", "buys"),
                txns_1h_sells=_nested_count(pair, "h1", "sells"),
                txns_6h_buys=_nested_count(pair, "h6", "buys"),
                txns_6h_sells=_nested_count(pair, "h6", "sells"),
                txns_24h_buys=_nested_count(pair, "h24", "buys"),
                txns_24h_sells=_nested_count(pair, "h24", "sells"),
                boosts_active=(
                    int(pair["boosts"]["active"])
                    if isinstance(pair.get("boosts"), dict)
                    and isinstance(pair["boosts"].get("active"), int)
                    and pair["boosts"]["active"] >= 0
                    else 0
                ),
            )
            previous = best.get(mint)
            if previous is None or liquidity > previous[0]:
                best[mint] = (liquidity, market)
        return {mint: item[1] for mint, item in best.items()}

    def latest_solana_profiles(self, *, limit: int = 20) -> list[str]:
        """Return a bounded list of recently published Solana token profiles."""
        if not 1 <= limit <= 30:
            raise MarketDataError("DEX Screener profile limit must be between 1 and 30")
        return self._latest_solana_mints("/token-profiles/latest/v1", limit=limit, label="profile")

    def latest_solana_boosts(self, *, limit: int = 20) -> list[str]:
        return self._latest_solana_mints("/token-boosts/latest/v1", limit=limit, label="boost")

    def top_solana_boosts(self, *, limit: int = 20) -> list[str]:
        return self._latest_solana_mints("/token-boosts/top/v1", limit=limit, label="top boost")

    def latest_solana_takeovers(self, *, limit: int = 20) -> list[str]:
        return self._latest_solana_mints(
            "/community-takeovers/latest/v1", limit=limit, label="community takeover"
        )

    def _latest_solana_mints(self, path: str, *, limit: int, label: str) -> list[str]:
        """Return a bounded list from a documented DEX Screener discovery feed."""
        if not 1 <= limit <= 30:
            raise MarketDataError(f"DEX Screener {label} limit must be between 1 and 30")
        response = self._get(path)
        if len(response.content) > 1_000_000:
            raise MarketDataError(f"DEX Screener {label} response exceeded the 1 MB safety limit")
        try:
            response.raise_for_status()
            payload = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise MarketDataError(
                f"DEX Screener {label} request failed with HTTP {response.status_code}"
            ) from exc
        if not isinstance(payload, list):
            raise MarketDataError(f"DEX Screener returned malformed {label} data")

        mints: list[str] = []
        for profile in payload:
            if not isinstance(profile, dict) or profile.get("chainId") != "solana":
                continue
            mint = profile.get("tokenAddress")
            if not isinstance(mint, str):
                continue
            try:
                Pubkey.from_string(mint)
            except ValueError:
                continue
            if mint not in mints:
                mints.append(mint)
                if len(mints) >= limit:
                    break
        return mints
