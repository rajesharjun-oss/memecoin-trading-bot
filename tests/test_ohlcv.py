from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any, Literal, cast

import httpx
import pytest
from solders.pubkey import Pubkey

from memecoin_bot import ohlcv
from memecoin_bot.journal import TradeJournal
from memecoin_bot.ohlcv import (
    GeckoTerminalOhlcvClient,
    OhlcvError,
    OhlcvRateLimited,
    PoolCandle,
    parse_ohlcv,
    parse_pool_markets,
)
from memecoin_bot.strategy_lab import (
    _available_for_replay,
    _has_conflicting_ohlc,
    analyze_entry,
    five_minute_overlap_diagnostic,
    load_verified_candles,
    timeframe_overlap_diagnostic,
)

MINT = "DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263"
POOL = "5zpyutJu9ee6jFymDGoK7F6S5Kczqtc9FomP3ueKuyA9"
BASE = 1_700_000_040  # Aligned to one-minute boundaries.


def _payload(rows: list[list[object]], *, token: str = MINT) -> dict[str, object]:
    return {
        "data": {"attributes": {"ohlcv_list": rows}},
        "meta": {"base": {"address": token}, "quote": {"address": "SOL"}},
    }


def _row(stamp: int, *, close: str = "1.1", volume: str = "120") -> list[object]:
    return [stamp, "1", "1.2", "0.9", close, volume]


def test_public_request_pacing_stays_at_four_calls_per_minute(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pauses: list[float] = []
    monkeypatch.setattr(ohlcv, "_NEXT_REQUEST_AT", 0.0)
    monkeypatch.setattr("memecoin_bot.ohlcv.time.monotonic", lambda: 100.0)
    monkeypatch.setattr("memecoin_bot.ohlcv.time.sleep", pauses.append)
    ohlcv._pace_public_request()
    ohlcv._pace_public_request()
    assert pauses == [15.0]


def test_trial_request_pace_is_ten_seconds_and_rejects_faster_configuration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pauses: list[float] = []
    monkeypatch.setattr(ohlcv, "_NEXT_REQUEST_AT", 0.0)
    monkeypatch.setattr("memecoin_bot.ohlcv.time.monotonic", lambda: 100.0)
    monkeypatch.setattr("memecoin_bot.ohlcv.time.sleep", pauses.append)
    ohlcv._pace_public_request(ohlcv.TRIAL_REQUEST_INTERVAL_SECONDS)
    ohlcv._pace_public_request(ohlcv.TRIAL_REQUEST_INTERVAL_SECONDS)
    assert pauses == [10.0]
    with pytest.raises(ValueError, match="Unsupported public OHLCV request pace"):
        GeckoTerminalOhlcvClient(request_interval_seconds=1.0)


def test_parser_sorts_deduplicates_and_excludes_open_interval() -> None:
    rows = [_row(BASE + 120), _row(BASE), _row(BASE + 60), _row(BASE)]
    candles = parse_ohlcv(_payload(rows), mint=MINT, pool=POOL, timeframe="1m", now=BASE + 160)
    assert [candle.opened_at for candle in candles] == [BASE, BASE + 60]
    assert candles[1].volume_usd == "120"


def test_client_checks_candle_close_after_network_wait_and_preserves_explicit_cutoff(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = {"now": BASE + 59}

    def respond(_request: httpx.Request) -> httpx.Response:
        clock["now"] = BASE + 61
        return httpx.Response(200, json=_payload([_row(BASE), _row(BASE + 60)]))

    monkeypatch.setattr(cast(Any, ohlcv).time, "time", lambda: clock["now"])
    with (
        httpx.Client(
            base_url="https://api.geckoterminal.com", transport=httpx.MockTransport(respond)
        ) as http,
        GeckoTerminalOhlcvClient(client=http) as gecko,
    ):
        observed = gecko.candles(MINT, POOL, "1m")
        explicit = gecko.candles(MINT, POOL, "1m", now=BASE + 59)
    assert [candle.opened_at for candle in observed] == [BASE]
    assert explicit == []


def test_parser_discards_conflicted_interval_without_losing_valid_history() -> None:
    rows = [_row(BASE), _row(BASE, volume="999"), _row(BASE + 60)]
    candles = parse_ohlcv(_payload(rows), mint=MINT, pool=POOL, timeframe="1m", now=BASE + 120)
    assert [candle.opened_at for candle in candles] == [BASE + 60]


@pytest.mark.parametrize(
    "payload",
    [
        _payload([_row(BASE), _row(BASE, volume="999")]),
        _payload([_row(BASE)], token=MINT + "other"),
        _payload([[BASE, "1", "0.8", "0.9", "1.1", "100"]]),
        _payload([_row(BASE + 1)]),
        {"data": {"attributes": {"ohlcv_list": [_row(BASE)]}}},
    ],
)
def test_parser_rejects_conflicting_or_untrusted_candles(payload: object) -> None:
    with pytest.raises(OhlcvError):
        parse_ohlcv(payload, mint=MINT, pool=POOL, timeframe="1m", now=BASE + 120)


def test_pool_market_parser_requires_exact_pool_and_token_identity() -> None:
    row = {
        "id": f"solana_{POOL}",
        "attributes": {
            "address": POOL,
            "base_token_price_usd": "0.000001",
            "reserve_in_usd": "0.0",
            "volume_usd": {"h24": "123"},
            "price_change_percentage": {"h24": "-12.5"},
            "pool_created_at": "2026-11-03T14:03:22Z",
        },
        "relationships": {
            "base_token": {"data": {"id": f"solana_{MINT}"}},
            "quote_token": {"data": {"id": "solana_So11111111111111111111111111111111111111112"}},
        },
    }
    observed = parse_pool_markets({"data": [row]}, requested={MINT: POOL})
    assert len(observed) == 1
    assert observed[0].price_usd == "0.000001"
    assert observed[0].liquidity_usd == "0.0"
    assert observed[0].volume_24h_usd == "123"
    assert observed[0].price_change_24h == "-12.5"
    assert observed[0].pair_created_at is not None
    assert (
        parse_pool_markets(
            {"data": [row]},
            requested={"So11111111111111111111111111111111111111112": POOL},
        )
        == []
    )
    assert parse_pool_markets({"data": [row]}, requested={MINT: str(Pubkey.new_unique())}) == []
    cast(dict[str, object], row["attributes"])["reserve_in_usd"] = "-1"
    assert parse_pool_markets({"data": [row]}, requested={MINT: POOL}) == []


def test_pool_market_client_uses_fixed_host_and_bounded_exact_pool_request() -> None:
    requests: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"data": []})

    with (
        httpx.Client(
            base_url="https://api.geckoterminal.com", transport=httpx.MockTransport(respond)
        ) as http,
        GeckoTerminalOhlcvClient(client=http) as gecko,
    ):
        assert gecko.pool_markets({MINT: POOL}) == []
        with pytest.raises(OhlcvError, match="Invalid Solana"):
            gecko.pool_markets({MINT: "https://internal.example/"})
        with pytest.raises(OhlcvError, match="Invalid pool market request"):
            gecko.pool_markets({str(Pubkey.new_unique()): POOL for _ in range(11)})
    assert len(requests) == 1
    assert requests[0].url.host == "api.geckoterminal.com"
    assert requests[0].url.path.endswith(f"/networks/solana/pools/multi/{POOL}")


def test_client_stops_on_rate_limit_without_changing_fixed_host() -> None:
    requests: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(429, headers={"Retry-After": "90"})

    with (
        httpx.Client(
            base_url="https://api.geckoterminal.com", transport=httpx.MockTransport(respond)
        ) as http,
        GeckoTerminalOhlcvClient(client=http) as gecko,
        pytest.raises(OhlcvRateLimited) as error,
    ):
        gecko.candles(MINT, POOL, "1m", now=BASE + 120)
    assert error.value.retry_after_seconds == 90
    assert len(requests) == 1
    assert all(request.url.host == "api.geckoterminal.com" for request in requests)
    assert all(request.url.params.get("token") == MINT for request in requests)


def test_client_marks_cached_response_separately_from_network_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requests: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json=_payload([_row(BASE)]))

    monkeypatch.setattr(ohlcv, "_pace_public_request", lambda _interval: None)
    monkeypatch.setattr(ohlcv, "_CACHE", {})
    with httpx.Client(
        base_url="https://api.geckoterminal.com", transport=httpx.MockTransport(respond)
    ) as http:
        gecko = GeckoTerminalOhlcvClient(client=http)
        gecko._owned = True
        first = gecko.candles(MINT, POOL, "1m")
        assert gecko.last_call_cache_hit is False
        assert gecko.candles(MINT, POOL, "1m") == first
        assert gecko.last_call_cache_hit is True
        cache_key = ("geckoterminal", MINT, POOL, "1m", 200, None)
        cached_at, cached_rows = ohlcv._CACHE[cache_key]
        ohlcv._CACHE[cache_key] = (
            cached_at - ohlcv.MINUTE_CACHE_TTL_SECONDS - 1,
            cached_rows,
        )
        assert gecko.candles(MINT, POOL, "1m") == first
        assert gecko.last_call_cache_hit is False
    assert len(requests) == 2


def test_coingecko_demo_backfill_uses_fixed_host_and_header() -> None:
    requests: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json=_payload([_row(BASE)]))

    with (
        httpx.Client(
            base_url="https://api.coingecko.com", transport=httpx.MockTransport(respond)
        ) as http,
        GeckoTerminalOhlcvClient(
            client=http, provider="coingecko-demo", demo_api_key="test-only-key"
        ) as source,
    ):
        candles = source.candles(MINT, POOL, "1m", now=BASE + 120)
    assert len(candles) == 1
    assert len(requests) == 1
    assert requests[0].url.host == "api.coingecko.com"
    assert requests[0].url.path == f"/api/v3/onchain/networks/solana/pools/{POOL}/ohlcv/minute"
    assert requests[0].headers["x-cg-demo-api-key"] == "test-only-key"
    assert requests[0].url.params["token"] == MINT
    assert requests[0].url.params["include_empty_intervals"] == "false"
    with GeckoTerminalOhlcvClient(provider="coingecko-demo", demo_api_key="test-only-key") as owned:
        assert owned._client.base_url == httpx.URL("https://api.coingecko.com")
    with pytest.raises(ValueError, match="COINGECKO_DEMO_API_KEY"):
        GeckoTerminalOhlcvClient(provider="coingecko-demo")


def test_historical_cursor_is_bounded_and_filters_provider_response() -> None:
    requests: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json=_payload([_row(BASE + step * 60) for step in range(3)]))

    with (
        httpx.Client(
            base_url="https://api.geckoterminal.com", transport=httpx.MockTransport(respond)
        ) as http,
        GeckoTerminalOhlcvClient(client=http) as gecko,
    ):
        older = gecko.candles(MINT, POOL, "1m", now=BASE + 240, before_timestamp=BASE + 120)
        assert [candle.opened_at for candle in older] == [BASE, BASE + 60]
        assert requests[0].url.host == "api.geckoterminal.com"
        assert requests[0].url.params["before_timestamp"] == str(BASE + 120)
        assert requests[0].url.params["token"] == MINT
        with pytest.raises(OhlcvError, match="historical OHLCV cursor"):
            gecko.candles(MINT, POOL, "1m", now=BASE + 240, before_timestamp=BASE + 300)
        assert len(requests) == 1


def test_storage_keeps_pool_identity_and_requires_matching_liquidity(tmp_path: Path) -> None:
    candles = parse_ohlcv(
        _payload([_row(BASE)]), mint=MINT, pool=POOL, timeframe="1m", now=BASE + 120
    )
    with TradeJournal(tmp_path / "journal.sqlite3") as journal:
        journal.record_pool_candles(candles, fetched_at=BASE + 120)
        journal.record_market_snapshot(
            mint=MINT,
            pool="different-pool",
            price_usd="1.1",
            liquidity_usd="50000",
            volume_24h_usd="1000",
            observed_at=BASE + 59,
        )
        mismatch = load_verified_candles(journal, MINT, POOL, "1m", now=BASE + 120)
        assert mismatch[0].liquidity_usd == 0
        journal.record_market_snapshot(
            mint=MINT,
            pool=POOL,
            price_usd="1.1",
            liquidity_usd="50000",
            volume_24h_usd="1000",
            observed_at=BASE + 60,
        )
        verified = load_verified_candles(journal, MINT, POOL, "1m", now=BASE + 120)
        journal.record_market_snapshot(
            mint=MINT,
            pool=POOL,
            price_usd="1.1",
            liquidity_usd="50000",
            volume_24h_usd="1000",
            observed_at=BASE + 119,
        )
        late_quote = load_verified_candles(journal, MINT, POOL, "1m", now=BASE + 120)
    assert verified[0].liquidity_usd == 50_000
    assert late_quote[0].liquidity_usd == 50_000
    assert verified[0].volume_delta_usd == 120
    assert verified[0].source == "geckoterminal_pool_trades"


def test_revised_closed_candle_blocks_current_signal_but_preserves_prior_observation(
    tmp_path: Path,
) -> None:
    first_fetch = BASE + 8 * 60
    revision_fetch = first_fetch + 30
    minute_candles = [
        PoolCandle(MINT, POOL, "1m", BASE + index * 60, "1.0", "1.2", "0.9", "1.1", "100")
        for index in range(8)
    ]
    with TradeJournal(tmp_path / "revised-candles.sqlite3") as journal:
        journal.record_pool_candles(minute_candles, fetched_at=first_fetch)
        # Formatting alone does not constitute a changed market observation.
        journal.record_pool_candles(
            [replace(minute_candles[0], open="1")], fetched_at=first_fetch + 5
        )
        assert journal.pool_candles(MINT, POOL, "1m")[0]["revision_detected_at"] is None
        assert (
            journal._connection.execute(
                "SELECT max_price_revision_bps FROM pool_candles WHERE mint=? AND opened_at=?",
                (MINT, minute_candles[0].opened_at),
            ).fetchone()[0]
            == 0
        )
        journal.record_pool_candles(
            [replace(minute_candles[2], high="1.4")], fetched_at=revision_fetch
        )
        stored = journal.pool_candles(MINT, POOL, "1m")
        assert stored[2]["high"] == "1.2"
        assert stored[2]["first_observed_values_at"] == first_fetch
        assert stored[2]["revision_detected_at"] == revision_fetch
        magnitude = journal._connection.execute(
            """SELECT max_price_revision_bps, max_volume_revision_usd FROM pool_candles
               WHERE mint=? AND opened_at=?""",
            (MINT, minute_candles[2].opened_at),
        ).fetchone()
        assert magnitude[0] == pytest.approx(1_666.6666667)
        assert magnitude[1] == 0
        before = load_verified_candles(journal, MINT, POOL, "1m", now=first_fetch + 10)
        after = load_verified_candles(journal, MINT, POOL, "1m", now=revision_fetch + 1)
        assert (
            analyze_entry(before, minimum_liquidity_usd=0, as_of=first_fetch + 10).status == "ready"
        )
        assert (
            analyze_entry(after, minimum_liquidity_usd=0, as_of=revision_fetch + 1).status
            == "revised_values"
        )
        journal.record_pool_candles(minute_candles[2:3], fetched_at=revision_fetch + 5)
        assert journal.pool_candles(MINT, POOL, "1m")[2]["revision_detected_at"] == revision_fetch
        assert journal._connection.execute(
            "SELECT max_price_revision_bps FROM pool_candles WHERE mint=? AND opened_at=?",
            (MINT, minute_candles[2].opened_at),
        ).fetchone()[0] == pytest.approx(1_666.6666667)
        journal.record_pool_candles(
            [replace(minute_candles[3], volume_usd="120")], fetched_at=revision_fetch + 10
        )
        volume_magnitude = journal._connection.execute(
            """SELECT max_price_revision_bps, max_volume_revision_usd FROM pool_candles
               WHERE mint=? AND opened_at=?""",
            (MINT, minute_candles[3].opened_at),
        ).fetchone()
        assert volume_magnitude == (0, 20)


def test_minute_revision_propagates_to_derived_and_direct_five_minute_bar(
    tmp_path: Path,
) -> None:
    bucket = BASE - BASE % 300
    minute_candles = [
        PoolCandle(MINT, POOL, "1m", bucket + index * 60, "1", "1.2", "0.9", "1.1", "10")
        for index in range(5)
    ]
    observed_at = bucket + 320
    revised_at = bucket + 330
    with TradeJournal(tmp_path / "derived-revision.sqlite3") as journal:
        journal.record_pool_candles(minute_candles, fetched_at=observed_at)
        journal.record_pool_candles(
            [replace(minute_candles[1], volume_usd="12")], fetched_at=revised_at
        )
        derived = load_verified_candles(journal, MINT, POOL, "5m", now=revised_at + 1)
        assert len(derived) == 1
        assert derived[0].samples == 5
        assert derived[0].revision_detected_at == revised_at
        journal.record_pool_candles(
            [PoolCandle(MINT, POOL, "5m", bucket, "1", "1.2", "0.9", "1.1", "50")],
            fetched_at=revised_at + 1,
        )
        direct = load_verified_candles(journal, MINT, POOL, "5m", now=revised_at + 2)
        assert direct[0].samples == 1
        assert direct[0].revision_detected_at == revised_at


def test_liquidity_observed_after_candle_close_is_not_used(tmp_path: Path) -> None:
    candles = parse_ohlcv(
        _payload([_row(BASE)]), mint=MINT, pool=POOL, timeframe="1m", now=BASE + 120
    )
    with TradeJournal(tmp_path / "late-liquidity.sqlite3") as journal:
        journal.record_pool_candles(candles, fetched_at=BASE + 120)
        journal.record_market_snapshot(
            mint=MINT,
            pool=POOL,
            price_usd="1.1",
            liquidity_usd="50000",
            volume_24h_usd="1000",
            observed_at=BASE + 119,
        )
        verified = load_verified_candles(journal, MINT, POOL, "1m", now=BASE + 120)
    assert verified[0].liquidity_usd == 0


def test_candle_stats_separate_legacy_rows_from_first_observed_values(tmp_path: Path) -> None:
    candles = [
        PoolCandle(MINT, POOL, "1m", stamp, "1", "1.2", "0.9", "1.1", "120")
        for stamp in (BASE, BASE + 60)
    ]
    with TradeJournal(tmp_path / "stats.sqlite3") as journal:
        journal.record_pool_candles(candles, fetched_at=BASE + 120)
        journal._connection.execute(
            "UPDATE pool_candles SET first_observed_values_at=NULL WHERE opened_at=?",
            (BASE,),
        )
        stats = journal.pool_candle_stats(MINT, POOL, "1m")
    assert stats == {
        "count": 2,
        "last_opened_at": BASE + 60,
        "trusted_count": 1,
        "last_trusted_opened_at": BASE + 60,
    }


def test_five_minute_candle_requires_all_five_closed_minutes_and_prefers_direct_bar(
    tmp_path: Path,
) -> None:
    bucket = BASE - BASE % 300
    minute_candles = [
        PoolCandle(
            MINT,
            POOL,
            "1m",
            bucket + index * 60,
            str(1 + index / 10),
            str(1.2 + index / 10),
            str(0.9 + index / 10),
            str(1.1 + index / 10),
            str(index + 1),
        )
        for index in range(5)
    ]
    with TradeJournal(tmp_path / "five-minute.sqlite3") as journal:
        journal.record_pool_candles(minute_candles, fetched_at=bucket + 300)
        journal.record_market_snapshot(
            mint=MINT,
            pool=POOL,
            price_usd="1.5",
            liquidity_usd="50000",
            volume_24h_usd="1000",
            observed_at=bucket + 299,
        )
        assert load_verified_candles(journal, MINT, POOL, "5m", now=bucket + 240) == []
        derived = load_verified_candles(journal, MINT, POOL, "5m", now=bucket + 360)
        assert len(derived) == 1
        assert derived[0].samples == 5
        assert derived[0].opened_at == bucket
        assert derived[0].open == 1.0
        assert derived[0].high == 1.6
        assert derived[0].low == 0.9
        assert derived[0].close == 1.5
        assert derived[0].volume_delta_usd == 15.0
        assert derived[0].liquidity_usd == 50_000

        journal.record_pool_candles(
            [PoolCandle(MINT, POOL, "5m", bucket, "2", "2.2", "1.9", "2.1", "16")],
            fetched_at=bucket + 300,
        )
        direct = load_verified_candles(journal, MINT, POOL, "5m", now=bucket + 360)
        assert len(direct) == 1
        assert direct[0].samples == 1
        assert direct[0].open == 2.0
        assert direct[0].close_disagreement_pct == pytest.approx(28.5714, abs=0.0001)
        assert direct[0].close_disagreement_pct is not None
        assert direct[0].ohlc_disagreement_pct is not None
        assert direct[0].ohlc_disagreement_pct > direct[0].close_disagreement_pct
        comparison = five_minute_overlap_diagnostic(journal, MINT, POOL)
        assert comparison["compared"] == 1
        assert comparison["bars_over_1pct"] == 1
        assert comparison["max_price_difference_pct"] > 1

    with TradeJournal(tmp_path / "high-only-conflict.sqlite3") as journal:
        journal.record_pool_candles(minute_candles, fetched_at=bucket + 600)
        journal.record_pool_candles(
            [PoolCandle(MINT, POOL, "5m", bucket, "1", "2", "0.9", "1.5", "15")],
            fetched_at=bucket + 300,
        )
        high_conflict = load_verified_candles(journal, MINT, POOL, "5m", now=bucket + 660)
        assert high_conflict[0].close_disagreement_pct == 0
        assert high_conflict[0].ohlc_disagreement_pct == pytest.approx(20.0)
        assert high_conflict[0].overlap_observed_at == bucket + 600
        assert five_minute_overlap_diagnostic(journal, MINT, POOL)["bars_over_1pct"] == 1
        journal._connection.execute(
            "UPDATE pool_candles SET first_fetched_at=NULL, first_observed_values_at=NULL "
            "WHERE mint=? AND pool=? AND timeframe='1m'",
            (MINT, POOL),
        )
        legacy_overlap = load_verified_candles(journal, MINT, POOL, "5m", now=bucket + 660)
        assert legacy_overlap[0].overlap_observed_at == bucket + 600

    with TradeJournal(tmp_path / "gap.sqlite3") as journal:
        journal.record_pool_candles(
            minute_candles[:2] + minute_candles[3:], fetched_at=bucket + 300
        )
        assert load_verified_candles(journal, MINT, POOL, "5m", now=bucket + 360) == []
        assert five_minute_overlap_diagnostic(journal, MINT, POOL)["compared"] == 0


def test_point_in_time_loader_keeps_earlier_derived_bar_when_direct_arrives_later(
    tmp_path: Path,
) -> None:
    bucket = BASE - BASE % 900
    minutes = [
        PoolCandle(MINT, POOL, "1m", bucket + index * 60, "1", "1.1", "0.9", "1", "1")
        for index in range(15)
    ]
    with TradeJournal(tmp_path / "historical-direct-arrival.sqlite3") as journal:
        journal.record_pool_candles(minutes, fetched_at=bucket + 900)
        journal.record_market_snapshot(
            mint=MINT,
            pool=POOL,
            price_usd="1",
            liquidity_usd="50000",
            volume_24h_usd="1000",
            observed_at=bucket + 899,
        )
        journal.record_pool_candles(
            [PoolCandle(MINT, POOL, "15m", bucket, "2", "2.1", "1.9", "2", "15")],
            fetched_at=bucket + 1_200,
        )
        historical = load_verified_candles(journal, MINT, POOL, "15m", now=bucket + 1_000)
        assert len(historical) == 1
        assert historical[0].samples == 15
        assert historical[0].close == 1
        assert historical[0].first_observed_values_at == bucket + 900

        current = load_verified_candles(journal, MINT, POOL, "15m", now=bucket + 1_300)
        assert len(current) == 1
        assert current[0].samples == 1
        assert current[0].close == 2
        assert current[0].close_disagreement_pct == 50


@pytest.mark.parametrize(("timeframe", "period"), [("15m", 900), ("1h", 3_600)])
def test_higher_timeframe_uses_only_complete_real_minute_bars(
    tmp_path: Path, timeframe: Literal["15m", "1h"], period: int
) -> None:
    bucket = BASE - BASE % period
    minute_count = period // 60
    rows = [
        PoolCandle(MINT, POOL, "1m", bucket + index * 60, "1", "1.2", "0.9", "1.1", "10")
        for index in range(minute_count)
    ]
    with TradeJournal(tmp_path / f"derived-{timeframe}.sqlite3") as journal:
        journal.record_pool_candles(rows[:-1], fetched_at=bucket + period + 30)
        journal.record_pool_candles(rows[-1:], fetched_at=bucket + period + 90)
        journal.record_market_snapshot(
            mint=MINT,
            pool=POOL,
            price_usd="1.1",
            liquidity_usd="50000",
            volume_24h_usd="1000",
            observed_at=bucket + period - 1,
        )
        assert load_verified_candles(journal, MINT, POOL, timeframe, now=bucket + period - 1) == []
        derived = load_verified_candles(journal, MINT, POOL, timeframe, now=bucket + period + 100)
        assert len(derived) == 1
        assert derived[0].samples == minute_count
        assert derived[0].opened_at == bucket
        assert derived[0].volume_delta_usd == minute_count * 10
        assert derived[0].first_fetched_at == bucket + period + 90
        assert derived[0].first_observed_values_at == bucket + period + 90
        assert derived[0].liquidity_usd == 50_000
        assert not _available_for_replay(derived[0], bucket + period + 60, timeframe)
        assert _available_for_replay(derived[0], bucket + period + 120, timeframe)

        journal.record_pool_candles(
            [PoolCandle(MINT, POOL, timeframe, bucket, "2", "2.2", "1.9", "2.1", "20")],
            fetched_at=bucket + period + 30,
        )
        direct = load_verified_candles(journal, MINT, POOL, timeframe, now=bucket + period + 100)
        assert direct[0].samples == 1
        assert direct[0].ohlc_disagreement_pct is not None
        assert direct[0].ohlc_disagreement_pct > 1
        assert direct[0].overlap_observed_at == bucket + period + 90
        assert not _has_conflicting_ohlc(direct[0], bucket + period + 60)
        assert _has_conflicting_ohlc(direct[0], bucket + period + 120)
        assert timeframe_overlap_diagnostic(journal, MINT, POOL, timeframe)["bars_over_1pct"] == 1
        context = [
            replace(
                direct[0],
                opened_at=bucket - (7 - index) * period,
                close_disagreement_pct=None,
                ohlc_disagreement_pct=None,
                overlap_observed_at=None,
            )
            for index in range(7)
        ] + direct
        assert (
            analyze_entry(
                context,
                minimum_liquidity_usd=25_000,
                timeframe=timeframe,
                as_of=bucket + period + 60,
            ).status
            != "conflicting_prices"
        )
        assert (
            analyze_entry(
                context,
                minimum_liquidity_usd=25_000,
                timeframe=timeframe,
                as_of=bucket + period + 120,
            ).status
            == "conflicting_prices"
        )
        mixed = [replace(context[0], samples=minute_count), *context[1:]]
        assert (
            analyze_entry(
                mixed,
                minimum_liquidity_usd=25_000,
                timeframe=timeframe,
                as_of=bucket + period + 120,
            ).status
            == "mixed_provenance"
        )

    with TradeJournal(tmp_path / f"gap-{timeframe}.sqlite3") as journal:
        journal.record_pool_candles(
            rows[: minute_count // 2] + rows[minute_count // 2 + 1 :],
            fetched_at=bucket + period + 30,
        )
        assert (
            load_verified_candles(journal, MINT, POOL, timeframe, now=bucket + period + 100) == []
        )


def test_rate_limit_without_header_uses_minimum_cooldown() -> None:
    calls = 0

    def respond(_: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(429)

    with (
        httpx.Client(
            base_url="https://api.geckoterminal.com", transport=httpx.MockTransport(respond)
        ) as http,
        GeckoTerminalOhlcvClient(client=http) as gecko,
        pytest.raises(OhlcvRateLimited) as error,
    ):
        gecko.candles(MINT, POOL, "1m", now=BASE + 120)
    assert error.value.retry_after_seconds == 60
    assert calls == 1
