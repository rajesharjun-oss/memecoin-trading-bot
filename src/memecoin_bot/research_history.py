"""Bounded retrospective pool-candle collection for research outcomes only."""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Protocol

from .journal import TradeJournal
from .ohlcv import GeckoTerminalOhlcvClient, PoolCandle


class _CandleSource(Protocol):
    def candles(
        self,
        mint: str,
        pool: str,
        timeframe: str,
        *,
        limit: int,
        before_timestamp: int,
    ) -> list[PoolCandle]: ...


@dataclass(frozen=True, slots=True)
class BackfillResult:
    requests: int
    new_candles: int
    oldest_opened_at: int | None
    newest_opened_at: int | None


def backfill_research_candles(
    journal: TradeJournal,
    source: _CandleSource | GeckoTerminalOhlcvClient,
    *,
    mint: str,
    pool: str,
    start: int,
    end: int,
    limit: int = 500,
    max_requests: int = 3,
) -> BackfillResult:
    """Store historical 1m outcomes without certifying them as past entry data."""
    if (
        type(start) is not int
        or type(end) is not int
        or not 0 < start < end <= int(time.time())
        or end - start > 86_400
        or type(limit) is not int
        or not 1 <= limit <= 1000
        or type(max_requests) is not int
        or not 1 <= max_requests <= 5
    ):
        raise ValueError("Research backfill range or request limit is invalid")
    cursor = end
    requests = 0
    new_candles = 0
    oldest: int | None = None
    newest: int | None = None
    for _ in range(max_requests):
        page = source.candles(mint, pool, "1m", limit=limit, before_timestamp=cursor)
        requests += 1
        if not page:
            break
        page_oldest = min(candle.opened_at for candle in page)
        if page_oldest >= cursor:
            break
        bounded = [
            candle
            for candle in page
            if start <= candle.opened_at < end and candle.opened_at < cursor
        ]
        if bounded:
            new_candles += journal.record_pool_candles(bounded, fetched_at=int(time.time()))
            oldest = (
                min(oldest, bounded[0].opened_at) if oldest is not None else bounded[0].opened_at
            )
            newest = (
                max(newest, bounded[-1].opened_at) if newest is not None else bounded[-1].opened_at
            )
        if page_oldest <= start:
            break
        cursor = page_oldest
    return BackfillResult(requests, new_candles, oldest, newest)
