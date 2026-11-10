"""Strict, research-only parsing of GeckoTerminal's public pool trade page.

Trade-derived candles are deliberately separate from trusted direct OHLCV
until price parity, ordering, and public-quota behaviour have been verified.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation

from solders.pubkey import Pubkey


class TradeFeedError(ValueError):
    """A public trade page cannot support trustworthy candle reconstruction."""


@dataclass(frozen=True, slots=True)
class TradeDerivedCandle:
    mint: str
    pool: str
    opened_at: int
    open_usd: Decimal
    high_usd: Decimal
    low_usd: Decimal
    close_usd: Decimal
    volume_usd: Decimal
    trade_count: int
    anomalous_trade: bool


@dataclass(frozen=True, slots=True)
class TradePage:
    candles: tuple[TradeDerivedCandle, ...]
    returned_trades: int
    oldest_trade_at: int | None
    newest_trade_at: int | None
    quote_mint: str | None

    def has_lower_boundary(self, opened_at: int) -> bool:
        """An older trade rules out truncation at the start, not missing recent swaps."""
        return self.oldest_trade_at is not None and self.oldest_trade_at < opened_at


def _positive(value: object) -> Decimal:
    if isinstance(value, bool):
        raise TradeFeedError("Trade feed returned a nonnumeric value")
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise TradeFeedError("Trade feed returned a nonnumeric value") from None
    if not number.is_finite() or number <= 0:
        raise TradeFeedError("Trade feed returned a nonpositive value")
    return number


def _timestamp(value: object, *, now: int) -> int:
    if not isinstance(value, str) or len(value) > 40:
        raise TradeFeedError("Trade feed returned an invalid timestamp")
    try:
        observed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if observed.tzinfo is None:
            raise ValueError("timezone missing")
        seconds = int(observed.timestamp())
    except (ValueError, OverflowError):
        raise TradeFeedError("Trade feed returned an invalid timestamp") from None
    if not now - 86_460 <= seconds <= now + 30:
        raise TradeFeedError("Trade timestamp is outside the public page window")
    return seconds


def parse_trade_page(payload: object, *, mint: str, pool: str, now: int) -> TradePage:
    """Aggregate actual swaps without inventing missing or still-open intervals.

    The public endpoint returns at most 300 recent trades. A future caller
    must request anomalous trades explicitly and require a lower-boundary
    trade before using a history window. That still cannot prove the provider
    delivered every swap inside the window or the latest cached minute.
    """
    try:
        Pubkey.from_string(mint)
        Pubkey.from_string(pool)
    except ValueError:
        raise TradeFeedError("Invalid Solana mint or pool") from None
    if type(now) is not int or now <= 0:
        raise TradeFeedError("Invalid observation time")
    if not isinstance(payload, dict) or not isinstance((raw := payload.get("data")), list):
        raise TradeFeedError("Trade feed omitted its data list")
    if len(raw) > 300:
        raise TradeFeedError("Trade feed exceeded the public page limit")

    parsed: list[tuple[int, int, Decimal, Decimal, bool]] = []
    seen_ids: set[str] = set()
    quote_mint: str | None = None
    previous_slot: int | None = None
    previous_second: int | None = None
    for row in raw:
        if not isinstance(row, dict) or row.get("type") != "trade":
            raise TradeFeedError("Trade feed returned a malformed trade")
        trade_id = row.get("id")
        item = row.get("attributes")
        if (
            not isinstance(trade_id, str)
            or not 1 <= len(trade_id) <= 200
            or trade_id in seen_ids
            or not isinstance(item, dict)
        ):
            raise TradeFeedError("Trade feed returned duplicate or malformed trade data")
        seen_ids.add(trade_id)
        slot = item.get("block_number")
        if type(slot) is not int or slot <= 0:
            raise TradeFeedError("Trade feed returned an invalid block number")
        second = _timestamp(item.get("block_timestamp"), now=now)
        if (previous_slot is not None and slot > previous_slot) or (
            previous_second is not None and second > previous_second
        ):
            raise TradeFeedError("Trade feed is not newest-first")
        previous_slot, previous_second = slot, second

        from_mint = item.get("from_token_address")
        to_mint = item.get("to_token_address")
        if (from_mint == mint) == (to_mint == mint):
            raise TradeFeedError("Trade does not identify exactly one target token side")
        side = "from" if from_mint == mint else "to"
        other = to_mint if side == "from" else from_mint
        if not isinstance(other, str) or not other:
            raise TradeFeedError("Trade omitted its quote token")
        if quote_mint is not None and other != quote_mint:
            raise TradeFeedError("Pool trade page mixes quote tokens")
        quote_mint = other
        anomalous = item.get("is_anomalous")
        if type(anomalous) is not bool:
            raise TradeFeedError("Trade omitted its anomaly flag")
        price = _positive(item.get(f"price_{side}_in_usd"))
        amount = _positive(item.get(f"{side}_token_amount"))
        volume = _positive(item.get("volume_in_usd"))
        if abs(price * amount - volume) > volume * Decimal("0.01"):
            raise TradeFeedError("Trade price and volume disagree")
        parsed.append((slot, second, price, volume, anomalous))

    buckets: dict[int, list[tuple[Decimal, Decimal, bool]]] = {}
    for _slot, second, price, volume, anomalous in reversed(parsed):
        opened_at = second // 60 * 60
        if opened_at + 60 > now:
            continue
        buckets.setdefault(opened_at, []).append((price, volume, anomalous))
    candles = tuple(
        TradeDerivedCandle(
            mint=mint,
            pool=pool,
            opened_at=opened_at,
            open_usd=trades[0][0],
            high_usd=max(price for price, _, _ in trades),
            low_usd=min(price for price, _, _ in trades),
            close_usd=trades[-1][0],
            volume_usd=sum((volume for _, volume, _ in trades), Decimal(0)),
            trade_count=len(trades),
            anomalous_trade=any(anomalous for _, _, anomalous in trades),
        )
        for opened_at, trades in sorted(buckets.items())
    )
    return TradePage(
        candles=candles,
        returned_trades=len(parsed),
        oldest_trade_at=parsed[-1][1] if parsed else None,
        newest_trade_at=parsed[0][1] if parsed else None,
        quote_mint=quote_mint,
    )
