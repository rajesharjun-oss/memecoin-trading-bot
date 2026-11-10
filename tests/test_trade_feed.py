from __future__ import annotations

from decimal import Decimal

import pytest

from memecoin_bot.trade_feed import TradeFeedError, parse_trade_page

MINT = "DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263"
POOL = "5zpyutJu9ee6jFymDGoK7F6S5Kczqtc9FomP3ueKuyA9"
QUOTE = "So11111111111111111111111111111111111111112"
BASE = 1_700_000_040


def _trade(
    stamp: int,
    *,
    slot: int,
    price: str,
    side: str = "to",
    anomalous: bool = False,
) -> dict[str, object]:
    from datetime import UTC, datetime

    amount = Decimal("10")
    return {
        "id": f"solana_{slot}_{stamp}",
        "type": "trade",
        "attributes": {
            "block_number": slot,
            "block_timestamp": datetime.fromtimestamp(stamp, UTC).isoformat(),
            "from_token_address": MINT if side == "from" else QUOTE,
            "to_token_address": QUOTE if side == "from" else MINT,
            f"price_{side}_in_usd": price,
            f"{side}_token_amount": str(amount),
            "volume_in_usd": str(Decimal(price) * amount),
            "is_anomalous": anomalous,
        },
    }


def test_trade_page_builds_closed_real_trade_candles_with_coverage_boundary() -> None:
    rows = [
        _trade(BASE + 185, slot=105, price="6"),  # Still open at the observation cutoff.
        _trade(BASE + 130, slot=104, price="5"),
        _trade(BASE + 75, slot=103, price="4", anomalous=True),
        _trade(BASE + 30, slot=102, price="3", side="from"),
        _trade(BASE + 15, slot=101, price="2"),
        _trade(BASE - 15, slot=100, price="1"),
    ]
    page = parse_trade_page({"data": rows}, mint=MINT, pool=POOL, now=BASE + 180)
    assert page.returned_trades == 6
    assert page.quote_mint == QUOTE
    assert page.oldest_trade_at == BASE - 15
    assert page.newest_trade_at == BASE + 185
    assert page.has_lower_boundary(BASE)
    assert [candle.opened_at for candle in page.candles] == [BASE - 60, BASE, BASE + 60, BASE + 120]
    candle = page.candles[1]
    assert (candle.open_usd, candle.high_usd, candle.low_usd, candle.close_usd) == (
        Decimal("2"),
        Decimal("3"),
        Decimal("2"),
        Decimal("3"),
    )
    assert candle.volume_usd == Decimal("50")
    assert candle.trade_count == 2
    assert candle.anomalous_trade is False
    assert page.candles[2].anomalous_trade is True


def test_trade_page_never_claims_full_window_from_a_truncated_recent_page() -> None:
    page = parse_trade_page(
        {"data": [_trade(BASE + 65, slot=2, price="2"), _trade(BASE + 5, slot=1, price="1")]},
        mint=MINT,
        pool=POOL,
        now=BASE + 120,
    )
    assert page.has_lower_boundary(BASE) is False
    assert page.has_lower_boundary(BASE + 60) is True


@pytest.mark.parametrize(
    ("change", "message"),
    [
        (lambda rows: rows.reverse(), "not newest-first"),
        (lambda rows: rows[0].update(id=rows[1]["id"]), "duplicate"),
        (
            lambda rows: rows[0]["attributes"].update(to_token_address=QUOTE),
            "exactly one target",
        ),
        (
            lambda rows: rows[0]["attributes"].update(volume_in_usd="999"),
            "price and volume disagree",
        ),
        (
            lambda rows: rows[0]["attributes"].update(block_timestamp="not-a-date"),
            "invalid timestamp",
        ),
        (
            lambda rows: rows[0]["attributes"].update(is_anomalous="false"),
            "anomaly flag",
        ),
    ],
)
def test_trade_page_rejects_untrusted_or_ambiguous_data(change: object, message: str) -> None:
    rows = [_trade(BASE + 65, slot=2, price="2"), _trade(BASE + 5, slot=1, price="1")]
    assert callable(change)
    change(rows)
    with pytest.raises(TradeFeedError, match=message):
        parse_trade_page({"data": rows}, mint=MINT, pool=POOL, now=BASE + 120)


def test_trade_page_rejects_invalid_pool_and_oversized_page() -> None:
    with pytest.raises(TradeFeedError, match="Invalid Solana"):
        parse_trade_page({"data": []}, mint=MINT, pool="http://localhost", now=BASE)
    with pytest.raises(TradeFeedError, match="page limit"):
        parse_trade_page({"data": [{}] * 301}, mint=MINT, pool=POOL, now=BASE)
