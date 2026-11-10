from __future__ import annotations

import time
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

import pytest
from solders.pubkey import Pubkey

from memecoin_bot.journal import TradeJournal
from memecoin_bot.market_data import MarketDataError, TokenMarket
from memecoin_bot.ohlcv import (
    TIMEFRAME_SECONDS,
    OhlcvError,
    OhlcvRateLimited,
    PoolCandle,
    PoolMarketObservation,
)
from memecoin_bot.strategy_lab import (
    DECISION_FOLLOWUP_KEY,
    DECISION_FOLLOWUP_METHOD,
    DECISION_FOLLOWUP_SECONDS,
    RESEARCH_COHORT_KEY,
    RESEARCH_COHORT_SECONDS,
    RESEARCH_FOCUS_SIZE,
    RESEARCH_SELECTION_METHOD,
    Candle,
    EntrySignal,
    aggregate_candles,
    analyze_entry,
    collect_market_snapshots,
    load_verified_candles,
    multi_frame_entry_verdict,
    research_cohort_mints,
    research_focus_size,
    run_backtest,
    select_decision_followup,
    select_research_cohort,
)


@pytest.fixture(autouse=True)
def no_network_research_pool_probe(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("memecoin_bot.strategy_lab.fetch_research_pool_markets", lambda _: [])


def _recent_pool_candle(mint: str, pool: str, frame: str) -> PoolCandle:
    period = TIMEFRAME_SECONDS[frame]
    opened_at = int(time.time()) // period * period - period
    return PoolCandle(mint, pool, frame, opened_at, "1", "1", "1", "1", "100")


def test_higher_frame_integrity_failure_blocks_otherwise_confirmed_entry() -> None:
    ready = EntrySignal("ready", True, 85, "up", 5.0, 1.5, 50_000, ())
    collecting = EntrySignal("collecting", False, 0, "unavailable", None, None, None, ())
    assert multi_frame_entry_verdict(
        {"1m": ready, "5m": ready, "15m": collecting, "1h": collecting}
    )[0]
    for status in (
        "conflicting_prices",
        "mixed_provenance",
        "revised_values",
        "unverified_values",
    ):
        invalid = replace(collecting, status=status)
        allowed, reason = multi_frame_entry_verdict(
            {"1m": ready, "5m": ready, "15m": invalid, "1h": collecting}
        )
        assert allowed is False
        assert "higher timeframe" in reason
        assert (
            multi_frame_entry_verdict({"1m": ready, "5m": ready, "15m": collecting, "1h": invalid})[
                0
            ]
            is False
        )


def test_aggregates_snapshots_into_requested_timeframes() -> None:
    snapshots = [
        {
            "observed_at": 1_700_000_000 + index * 60,
            "price_usd": str(1 + index / 100),
            "liquidity_usd": "50000",
            "volume_24h_usd": str(100_000 + index * 1_000),
        }
        for index in range(10)
    ]

    one_minute = aggregate_candles(snapshots, "1m")
    five_minute = aggregate_candles(snapshots, "5m")

    assert len(one_minute) == 10
    assert len(five_minute) == 3
    assert five_minute[0].high >= five_minute[0].low
    assert sum(candle.samples for candle in five_minute) == 10


def test_entry_confirmation_reports_collection_then_confirms() -> None:
    candles = [
        Candle(
            opened_at=index * 60,
            open=1 + index * 0.02,
            high=1.02 + index * 0.02,
            low=0.99 + index * 0.02,
            close=1.01 + index * 0.02,
            volume_delta_usd=100 + index * 50,
            liquidity_usd=50_000,
            samples=1,
        )
        for index in range(8)
    ]

    assert analyze_entry(candles[:4], minimum_liquidity_usd=25_000).status == "collecting"
    signal = analyze_entry(candles, minimum_liquidity_usd=25_000)
    assert signal.status == "ready"
    assert signal.confirmed is True
    assert signal.score == 100


def test_five_minute_entry_waits_for_consistent_candle_provenance() -> None:
    direct = _five_minute_trend(1_700_000_000, rising=True)
    conflicting = [*direct[:-1], replace(direct[-1], close_disagreement_pct=1.5)]
    assert analyze_entry(conflicting, minimum_liquidity_usd=25_000, timeframe="5m").status == (
        "conflicting_prices"
    )
    high_only_conflict = [
        *direct[:-1],
        replace(direct[-1], close_disagreement_pct=0, ohlc_disagreement_pct=1.5),
    ]
    assert (
        analyze_entry(high_only_conflict, minimum_liquidity_usd=25_000, timeframe="5m").status
        == "conflicting_prices"
    )
    later_overlap = [
        *direct[:-1],
        replace(
            direct[-1],
            close_disagreement_pct=0,
            ohlc_disagreement_pct=1.5,
            overlap_observed_at=direct[-1].opened_at + 420,
        ),
    ]
    assert (
        analyze_entry(
            later_overlap,
            minimum_liquidity_usd=25_000,
            timeframe="5m",
            as_of=direct[-1].opened_at + 360,
        ).status
        == "ready"
    )
    assert (
        analyze_entry(
            later_overlap,
            minimum_liquidity_usd=25_000,
            timeframe="5m",
            as_of=direct[-1].opened_at + 420,
        ).status
        == "conflicting_prices"
    )
    mixed = [*direct[:-1], replace(direct[-1], samples=5)]
    assert analyze_entry(mixed, minimum_liquidity_usd=25_000, timeframe="5m").status == (
        "mixed_provenance"
    )
    derived = [replace(candle, samples=5) for candle in direct]
    assert analyze_entry(derived, minimum_liquidity_usd=25_000, timeframe="5m").status == ("ready")


def test_backtest_is_pessimistic_when_stop_and_target_share_candle() -> None:
    candles = [
        Candle(
            opened_at=index * 60,
            open=1 + index * 0.02,
            high=1.03 + index * 0.02,
            low=0.99 + index * 0.02,
            close=1.01 + index * 0.02,
            volume_delta_usd=100 + index * 50,
            liquidity_usd=50_000,
            samples=1,
            source="geckoterminal_pool_trades",
            first_fetched_at=(index + 1) * 60 + 1,
            first_observed_values_at=(index + 1) * 60 + 1,
        )
        for index in range(10)
    ]
    entry = candles[8].close
    candles[9] = Candle(
        opened_at=9 * 60,
        open=entry,
        high=entry * 2.1,
        low=entry * 0.5,
        close=entry,
        volume_delta_usd=1_000,
        liquidity_usd=50_000,
        samples=1,
        source="geckoterminal_pool_trades",
        first_fetched_at=10 * 60 + 1,
        first_observed_values_at=10 * 60 + 1,
    )

    result = run_backtest(
        candles,
        minimum_liquidity_usd=25_000,
        tp_levels_bps=(10_000,),
        tp_sell_bps=(10_000,),
        stop_loss_bps=3_500,
    )

    assert result["status"] == "complete"
    assert result["trades"] == 1
    assert result["return_pct"] == -0.35
    assert result["trade_log"][0]["return_pct"] == -35.0
    assert result["trade_log"][0]["exit_reason"] == "stop_loss"


def test_replay_rejects_token_without_recorded_cohort_membership() -> None:
    result = run_backtest(
        _verified_trend(40),
        minimum_liquidity_usd=25_000,
        tp_levels_bps=(10_000,),
        tp_sell_bps=(10_000,),
        stop_loss_bps=3_500,
        entry_windows=(),
    )
    assert result["status"] == "outside_research_cohort"
    assert result["trades"] == 0


def test_journal_persists_snapshots_and_backtest_runs(tmp_path: Path) -> None:
    with TradeJournal(tmp_path / "lab.sqlite3") as journal:
        journal.record_market_snapshot(
            mint="mint",
            price_usd="0.01",
            liquidity_usd="50000",
            volume_24h_usd="100000",
            observed_at=1_700_000_001,
        )
        journal.record_market_snapshot(
            mint="mint",
            price_usd="0.02",
            liquidity_usd="51000",
            volume_24h_usd="101000",
            observed_at=1_700_000_030,
        )
        run_id = journal.record_backtest(
            mint="mint",
            timeframe="1m",
            strategy_name="test",
            result={"trades": 1, "return_pct": 5.0},
        )

        snapshots = journal.market_snapshots("mint")
        runs = journal.backtest_runs()

    assert len(snapshots) == 2
    assert [row["price_usd"] for row in snapshots] == ["0.01", "0.02"]
    assert runs[0]["run_id"] == run_id


def _verified_trend(count: int = 12) -> list[Candle]:
    return [
        Candle(
            opened_at=1_700_000_040 + index * 60,
            open=1 + index * 0.02,
            high=1.03 + index * 0.02,
            low=0.99 + index * 0.02,
            close=1.01 + index * 0.02,
            volume_delta_usd=100 + index * 50,
            liquidity_usd=50_000,
            samples=1,
            source="geckoterminal_pool_trades",
            first_fetched_at=1_700_000_040 + (index + 1) * 60 + 1,
            first_observed_values_at=1_700_000_040 + (index + 1) * 60 + 1,
        )
        for index in range(count)
    ]


def test_liquidity_ablation_keeps_hard_entry_floor() -> None:
    candles = _verified_trend(8)
    candles[-1] = replace(candles[-1], liquidity_usd=0)

    signal = analyze_entry(
        candles,
        minimum_liquidity_usd=25_000,
        disabled_factors=frozenset({"liquidity"}),
    )

    assert signal.status == "ready"
    assert signal.liquidity_usd == 0
    assert signal.confirmed is False


def _single_entry_replay(
    candles: list[Candle],
    *,
    exit_model: Literal["intrabar_ohlc", "paper_polling_proxy"],
    max_hold_seconds: int = 600,
) -> dict[str, Any]:
    return run_backtest(
        candles,
        minimum_liquidity_usd=25_000,
        tp_levels_bps=(10_000, 20_000),
        tp_sell_bps=(5_000, 5_000),
        stop_loss_bps=3_500,
        max_hold_seconds=max_hold_seconds,
        entry_windows=((candles[9].opened_at, candles[10].opened_at),),
        exit_model=exit_model,
    )


def test_paper_polling_replay_ignores_unobserved_intrabar_spike() -> None:
    candles = _verified_trend(40)
    entry = candles[9].open
    candles[12] = replace(candles[12], high=entry * 3, low=entry * 0.4)

    intrabar = _single_entry_replay(candles, exit_model="intrabar_ohlc")
    polling = _single_entry_replay(candles, exit_model="paper_polling_proxy")

    assert intrabar["trade_log"][0]["exit_reason"] == "stop_loss"
    assert polling["trade_log"][0]["exit_reason"] == "max_hold_time"
    assert polling["paper_exit_rule_parity"] is True
    assert polling["paper_exit_model_parity"] is False
    assert polling["historical_market_gate_parity"] is False
    assert polling["paper_entry_schedule_parity"] is False
    assert polling["exit_execution_model"] == "closed_candle_poll_next_open_proxy"


def test_paper_polling_replay_cannot_use_a_bar_fetched_after_poll() -> None:
    candles = _verified_trend(40)
    entry = candles[9].open
    candles[13] = replace(candles[13], high=entry * 2.2, close=entry * 2.1)
    result = _single_entry_replay(candles, exit_model="paper_polling_proxy")

    assert result["trades"] == 1
    assert result["trade_log"][0]["tp_stages"] == 0
    assert result["trade_log"][0]["exit_reason"] == "max_hold_time"


def test_paper_polling_stop_uses_later_open_after_observed_mark() -> None:
    candles = _verified_trend(40)
    entry = candles[9].open
    candles[12] = replace(candles[12], low=entry * 0.4, close=entry * 0.5)
    candles[15] = replace(candles[15], open=entry * 0.3, low=entry * 0.25)
    result = _single_entry_replay(candles, exit_model="paper_polling_proxy")

    assert result["trades"] == 1
    assert result["trade_log"][0]["exit_reason"] == "stop_loss"
    assert result["trade_log"][0]["return_pct"] == -70.0


def test_paper_polling_fill_uses_liquidity_known_at_order_time() -> None:
    candles = _verified_trend(40)
    # Liquidity attached to the fill candle is observed by that candle's close,
    # after the assumed next-open fill. It cannot veto an earlier order.
    candles[20] = replace(candles[20], liquidity_usd=0)
    result = _single_entry_replay(candles, exit_model="paper_polling_proxy")

    assert result["trades"] == 1
    assert result["trade_log"][0]["exit_reason"] == "max_hold_time"
    assert result["censored_trades"] == 0


def test_paper_polling_replay_censors_late_exit_observation() -> None:
    candles = _verified_trend(40)
    for index in range(9, 14):
        candles[index] = replace(
            candles[index], first_observed_values_at=candles[index].opened_at + 60 + 180
        )
    result = _single_entry_replay(candles, exit_model="paper_polling_proxy")

    assert result["trades"] == 0
    assert result["censored_log"][0]["reason"] == "exit_candle_unobserved"


def test_paper_polling_replay_censors_stale_mark_after_missed_polls() -> None:
    candles = _verified_trend(40)
    for index in range(14, 24):
        candles[index] = replace(
            candles[index], first_observed_values_at=candles[index].opened_at + 60 + 180
        )
    candles[17] = replace(candles[17], first_observed_values_at=candles[17].opened_at + 60 + 61)
    result = _single_entry_replay(candles, exit_model="paper_polling_proxy", max_hold_seconds=1_800)

    assert result["trades"] == 0
    assert result["censored_log"][0]["reason"] == "exit_candle_unobserved"


def test_paper_polling_replay_uses_later_open_for_partial_profit() -> None:
    candles = _verified_trend(40)
    entry = candles[9].open
    candles[17] = replace(candles[17], high=entry * 2.2, close=entry * 2.1)
    candles[20] = replace(candles[20], open=entry * 2, high=entry * 2.05)
    result = _single_entry_replay(candles, exit_model="paper_polling_proxy")

    assert result["trades"] == 1
    assert result["trade_log"][0]["exit_legs"] == 2
    assert result["trade_log"][0]["tp_stages"] == 1
    assert result["trade_log"][0]["exit_reason"] == "trailing_stop"
    assert result["trade_log"][0]["return_pct"] > 0


def _five_minute_trend(first_open: int, *, rising: bool) -> list[Candle]:
    aligned = first_open - first_open % 300
    return [
        Candle(
            opened_at=aligned + index * 300,
            open=1 + (index if rising else 11 - index) * 0.02,
            high=1.04 + (index if rising else 11 - index) * 0.02,
            low=0.99 + (index if rising else 11 - index) * 0.02,
            close=1.01 + (index if rising else 11 - index) * 0.02,
            volume_delta_usd=100 + index * 50,
            liquidity_usd=50_000,
            samples=1,
            source="geckoterminal_pool_trades",
            first_fetched_at=aligned + (index + 1) * 300 + 1,
            first_observed_values_at=aligned + (index + 1) * 300 + 1,
        )
        for index in range(12)
    ]


def test_multi_frame_replay_requires_closed_five_minute_confirmation() -> None:
    candles = _verified_trend(55)

    def replay(five_minute: list[Candle]) -> dict[str, Any]:
        return run_backtest(
            candles,
            confirmation_frames={"5m": five_minute},
            minimum_liquidity_usd=25_000,
            tp_levels_bps=(10_000,),
            tp_sell_bps=(10_000,),
            stop_loss_bps=3_500,
            max_hold_seconds=60,
        )

    rising = replay(_five_minute_trend(candles[0].opened_at, rising=True))
    falling = replay(_five_minute_trend(candles[0].opened_at, rising=False))
    future = replay(_five_minute_trend(candles[-1].opened_at + 3_600, rising=True))
    missing = replay([])
    assert rising["entry_model"] == "multi_timeframe_candle_gate"
    assert rising["trades"] > 0
    assert falling["trades"] == 0
    assert falling["entry_decisions_in_window"] > 0
    assert falling["entry_rejection_counts"]["5m_unconfirmed"] > 0
    bounded = run_backtest(
        candles,
        confirmation_frames={"5m": _five_minute_trend(candles[0].opened_at, rising=False)},
        minimum_liquidity_usd=25_000,
        tp_levels_bps=(10_000,),
        tp_sell_bps=(10_000,),
        stop_loss_bps=3_500,
        entry_windows=((candles[40].opened_at, candles[45].opened_at),),
    )
    assert bounded["entry_decisions_in_window"] == 5
    assert sum(bounded["entry_rejection_counts"].values()) == 5
    assert future["trades"] == 0
    assert missing["status"] == "insufficient_confirmation_history"


def test_replay_rejects_bars_first_fetched_after_entry_decision() -> None:
    candles = _verified_trend(55)
    five_minute = _five_minute_trend(candles[0].opened_at, rising=True)
    final_decision = candles[-2].opened_at + 60

    def replay(one_minute: list[Candle], confirmation: list[Candle]) -> dict[str, Any]:
        return run_backtest(
            one_minute,
            confirmation_frames={"5m": confirmation},
            minimum_liquidity_usd=25_000,
            tp_levels_bps=(10_000,),
            tp_sell_bps=(10_000,),
            stop_loss_bps=3_500,
            max_hold_seconds=60,
        )

    assert replay(candles, five_minute)["trades"] > 0
    impossible_preclose = [
        replace(
            c,
            first_fetched_at=c.opened_at + 59,
            first_observed_values_at=c.opened_at + 59,
        )
        for c in candles
    ]
    assert replay(impossible_preclose, five_minute)["status"] == (
        "insufficient_candle_availability"
    )
    delayed_entry = replay(
        [replace(c, first_fetched_at=final_decision + 1) for c in candles], five_minute
    )
    assert delayed_entry["status"] == "insufficient_candle_availability"
    assert delayed_entry["trades"] == 0
    assert replay([replace(c, first_fetched_at=None) for c in candles], five_minute)["trades"] == 0
    assert (
        replay([replace(c, first_observed_values_at=None) for c in candles], five_minute)["status"]
        == "insufficient_candle_availability"
    )
    delayed_confirmation = replay(
        candles,
        [replace(c, first_fetched_at=final_decision + 1) for c in five_minute],
    )
    assert delayed_confirmation["status"] == "insufficient_confirmation_history"
    assert delayed_confirmation["trades"] == 0


def test_derived_five_minute_bar_waits_for_latest_component_fetch(tmp_path: Path) -> None:
    minute_rows = [
        PoolCandle("mint", "pool", "1m", index * 60, "1", "1", "1", "1", "10") for index in range(5)
    ]
    with TradeJournal(tmp_path / "availability.sqlite3") as journal:
        journal.record_pool_candles(minute_rows[:4], fetched_at=310)
        journal.record_pool_candles(minute_rows[4:], fetched_at=370)
        derived = load_verified_candles(journal, "mint", "pool", "5m", now=600)
    assert len(derived) == 1
    assert derived[0].samples == 5
    assert derived[0].first_fetched_at == 370
    assert derived[0].first_observed_values_at == 370


def test_replay_uses_only_prior_fresh_safe_audit_observations() -> None:
    candles = _verified_trend(55)
    five_minute = _five_minute_trend(candles[0].opened_at, rising=True)
    pool = "recorded-pool"

    def replay(observations: list[dict[str, object]], *, replay_pool: str = pool) -> dict[str, Any]:
        return run_backtest(
            candles,
            minimum_liquidity_usd=25_000,
            tp_levels_bps=(10_000,),
            tp_sell_bps=(10_000,),
            stop_loss_bps=3_500,
            max_hold_seconds=60,
            confirmation_frames={"5m": five_minute},
            audit_observations=observations,
            expected_pool=replay_pool,
        )

    observed_at_us = (candles[38].opened_at + 1) * 1_000_000
    safe: dict[str, object] = {
        "observed_at_us": observed_at_us,
        "audit_status": "safe",
        "pair_address": pool,
    }
    assert replay([safe])["trades"] > 0
    blocked = replay([{**safe, "audit_status": "blocked"}])
    assert blocked["trades"] == 0
    assert blocked["entry_rejection_counts"]["audit_not_safe"] > 0
    wrong_pool = replay([{**safe, "pair_address": "different-pool"}])
    assert wrong_pool["trades"] == 0
    assert wrong_pool["entry_rejection_counts"]["audit_pool_mismatch"] > 0
    assert replay([{**safe, "pair_address": None}])["trades"] == 0
    assert replay([{**safe, "observed_at_us": candles[0].opened_at * 1_000_000}])["trades"] == 0
    future = replay([{**safe, "observed_at_us": (candles[-1].opened_at + 60) * 1_000_000}])
    assert future["status"] == "insufficient_audit_history"
    assert replay([])["status"] == "insufficient_audit_history"


def test_replay_market_hard_gates_use_only_prior_fresh_pool_snapshots() -> None:
    candles = _verified_trend(40)
    decision_at = candles[8].opened_at + 60
    valid: dict[str, object] = {
        "captured_at": decision_at - 20,
        "pool": "pool",
        "liquidity_usd": "50000",
        "volume_24h_usd": "100000",
        "price_change_24h": "25",
    }

    def replay(snapshots: list[dict[str, object]]) -> dict[str, Any]:
        return run_backtest(
            candles,
            minimum_liquidity_usd=25_000,
            minimum_volume_24h_usd=50_000,
            market_snapshots=snapshots,
            expected_pool="pool",
            tp_levels_bps=(10_000,),
            tp_sell_bps=(10_000,),
            stop_loss_bps=3_500,
            max_hold_seconds=600,
            entry_windows=((candles[9].opened_at, candles[10].opened_at),),
            exit_model="paper_polling_proxy",
        )

    accepted = replay([valid])
    assert accepted["trades"] == 1
    assert accepted["historical_market_hard_gates"] is True
    assert accepted["historical_market_gate_parity"] is False
    assert replay([])["status"] == "insufficient_market_history"

    blocked_cases = (
        ({"captured_at": decision_at - 181}, "market_snapshot_stale"),
        ({"pool": "different-pool"}, "market_pool_mismatch"),
        ({"liquidity_usd": "10000"}, "market_liquidity_below_minimum"),
        ({"volume_24h_usd": None}, "market_volume_unavailable"),
        ({"volume_24h_usd": "1000"}, "market_volume_below_minimum"),
        ({"price_change_24h": "251"}, "market_move_exceeds_guard"),
        ({"captured_at": decision_at + 1}, "market_snapshot_missing"),
    )
    for override, reason in blocked_cases:
        result = replay([{**valid, **override}])
        assert result["trades"] == 0
        assert result["entry_rejection_counts"][reason] > 0

    later = {**valid, "captured_at": decision_at + 1, "volume_24h_usd": "1000"}
    assert replay([valid, later])["trades"] == 1
    earlier = {**later, "captured_at": decision_at - 1}
    assert replay([valid, earlier])["entry_rejection_counts"]["market_volume_below_minimum"] > 0


def test_replay_market_score_fails_closed_and_records_eligible_score() -> None:
    candles = _verified_trend(40)
    decision_at = candles[8].opened_at + 60
    snapshot: dict[str, object] = {
        "captured_at": decision_at - 20,
        "pool": "pool",
        "liquidity_usd": "50000",
        "volume_24h_usd": "100000",
        "price_change_24h": "25",
    }

    def replay(verdict: tuple[str, int | None] | None) -> dict[str, Any]:
        return run_backtest(
            candles,
            minimum_liquidity_usd=25_000,
            minimum_volume_24h_usd=50_000,
            market_snapshots=[snapshot],
            audit_observations=[
                {
                    "observed_at_us": (decision_at - 20) * 1_000_000,
                    "audit_status": "safe",
                    "pair_address": "pool",
                }
            ],
            expected_pool="pool",
            market_score_replayer=lambda _snapshot, _now: verdict,
            tp_levels_bps=(10_000,),
            tp_sell_bps=(10_000,),
            stop_loss_bps=3_500,
            max_hold_seconds=600,
            entry_windows=((candles[9].opened_at, candles[10].opened_at),),
            exit_model="paper_polling_proxy",
        )

    assert replay(None)["entry_rejection_counts"]["narrative_history_missing"] > 0
    assert replay(("reject", 90))["entry_rejection_counts"]["market_score_reject"] > 0
    assert replay(("observe", 60))["entry_rejection_counts"]["market_score_observe"] > 0
    for reason in (
        "market_policy_missing",
        "market_policy_stale",
        "market_policy_mismatch",
        "market_policy_invalid",
    ):
        assert replay((reason, None))["entry_rejection_counts"][reason] > 0
    accepted = replay(("eligible", 85))
    assert accepted["trades"] == 1
    assert accepted["historical_market_score_gate_enabled"] is True
    assert accepted["historical_market_score_evaluations"] >= 1
    assert accepted["historical_market_gate_parity"] is False
    assert accepted["trade_log"][0]["market_score"] == 85


def test_replay_enters_after_signal_close_and_respects_position_fraction() -> None:
    candles = _verified_trend(11)
    result = run_backtest(
        candles,
        minimum_liquidity_usd=25_000,
        tp_levels_bps=(10_000,),
        tp_sell_bps=(10_000,),
        stop_loss_bps=3_500,
        max_hold_seconds=60,
    )
    assert result["trades"] == 1
    trade = result["trade_log"][0]
    assert trade["signal_at"] == candles[7].opened_at
    assert trade["decision_at"] == candles[8].opened_at + 60
    assert trade["entry_at"] == candles[9].opened_at
    assert trade["entry_price"] == candles[9].open
    assert result["position_fraction_bps"] == 100


def test_replay_regime_ignores_unobserved_decision_bar() -> None:
    candles = _verified_trend(11)

    def replay(rows: list[Candle]) -> dict[str, Any]:
        return run_backtest(
            rows,
            minimum_liquidity_usd=25_000,
            tp_levels_bps=(100_000,),
            tp_sell_bps=(10_000,),
            stop_loss_bps=3_500,
            max_hold_seconds=60,
        )

    baseline = replay(candles)
    changed = candles.copy()
    changed[8] = replace(
        changed[8],
        high=changed[8].high * 10,
        low=changed[8].low * 0.1,
        close=changed[8].close * 10,
        volume_delta_usd=changed[8].volume_delta_usd * 100,
    )
    mutated = replay(changed)
    assert baseline["trades"] == mutated["trades"] == 1
    assert baseline["trade_log"][0]["signal_at"] == candles[7].opened_at
    assert baseline["trade_log"][0]["regime"] == mutated["trade_log"][0]["regime"]
    assert baseline["trade_log"][0]["signal_score"] == mutated["trade_log"][0]["signal_score"]


def test_replay_uses_only_recorded_research_selection_windows() -> None:
    candles = _verified_trend(11)
    entry_at = candles[9].opened_at
    before = run_backtest(
        candles,
        minimum_liquidity_usd=25_000,
        tp_levels_bps=(10_000,),
        tp_sell_bps=(10_000,),
        stop_loss_bps=3_500,
        max_hold_seconds=60,
        entry_windows=((candles[0].opened_at, entry_at),),
    )
    during = run_backtest(
        candles,
        minimum_liquidity_usd=25_000,
        tp_levels_bps=(10_000,),
        tp_sell_bps=(10_000,),
        stop_loss_bps=3_500,
        max_hold_seconds=60,
        entry_windows=((entry_at, entry_at + 60),),
    )
    assert before["trades"] == 0
    assert during["trades"] == 1
    assert during["trade_log"][0]["entry_at"] == entry_at


def test_replay_excludes_unclosed_position_from_realized_performance() -> None:
    result = run_backtest(
        _verified_trend(10),
        minimum_liquidity_usd=25_000,
        tp_levels_bps=(10_000,),
        tp_sell_bps=(10_000,),
        stop_loss_bps=3_500,
    )
    assert result["trades"] == 0
    assert result["trade_log"] == []
    assert result["censored_trades"] == 1
    assert result["censored_log"][0]["reason"] == "series_end"
    assert result["return_pct"] == 0


def test_replay_censors_unknown_exit_liquidity_and_data_gap() -> None:
    candles = _verified_trend(11)
    for changed, reason in (
        (replace(candles[8], liquidity_usd=0), "liquidity_unknown"),
        (replace(candles[10], opened_at=candles[10].opened_at + 60), "data_gap"),
    ):
        scenario = candles.copy()
        scenario[8 if reason == "liquidity_unknown" else 10] = changed
        result = run_backtest(
            scenario,
            minimum_liquidity_usd=25_000,
            tp_levels_bps=(10_000,),
            tp_sell_bps=(10_000,),
            stop_loss_bps=3_500,
        )
        assert result["trades"] == 0
        assert result["censored_trades"] == 1
        assert result["censored_log"][0]["reason"] == reason


def test_intrabar_replay_ignores_liquidity_observed_after_entry_open() -> None:
    candles = _verified_trend(11)
    candles[9] = replace(candles[9], liquidity_usd=0)
    result = run_backtest(
        candles,
        minimum_liquidity_usd=25_000,
        tp_levels_bps=(10_000,),
        tp_sell_bps=(10_000,),
        stop_loss_bps=3_500,
        max_hold_seconds=60,
    )

    assert result["trades"] == 1
    assert result["trade_log"][0]["exit_reason"] == "max_hold_time"


@pytest.mark.parametrize("field", ["first_fetched_at", "first_observed_values_at"])
def test_replay_censors_exit_bar_observed_too_late(field: str) -> None:
    candles = _verified_trend(11)
    exit_bar = candles[10]
    candles[10] = replace(exit_bar, **{field: exit_bar.opened_at + 60 + 121})
    result = run_backtest(
        candles,
        minimum_liquidity_usd=25_000,
        tp_levels_bps=(100_000,),
        tp_sell_bps=(10_000,),
        stop_loss_bps=3_500,
        max_hold_seconds=60,
    )
    assert result["trades"] == 0
    assert result["censored_trades"] == 1
    assert result["censored_log"][0]["reason"] == "exit_candle_unobserved"
    assert result["paper_exit_model_parity"] is False


def test_replay_never_enters_before_token_discovery() -> None:
    candles = _verified_trend(10)
    result = run_backtest(
        candles,
        minimum_liquidity_usd=25_000,
        tp_levels_bps=(10_000,),
        tp_sell_bps=(10_000,),
        stop_loss_bps=3_500,
        entry_not_before=candles[-1].opened_at + 60,
    )
    assert result["status"] == "complete"
    assert result["trades"] == 0


def test_stale_and_missing_intervals_cannot_confirm_entry() -> None:
    candles = _verified_trend(10)
    stale = analyze_entry(
        candles[:8],
        minimum_liquidity_usd=25_000,
        as_of=candles[7].opened_at + 181,
    )
    assert stale.status == "stale"
    assert stale.confirmed is False
    candles[6] = Candle(
        opened_at=candles[6].opened_at + 60,
        open=candles[6].open,
        high=candles[6].high,
        low=candles[6].low,
        close=candles[6].close,
        volume_delta_usd=candles[6].volume_delta_usd,
        liquidity_usd=candles[6].liquidity_usd,
        samples=1,
        source="geckoterminal_pool_trades",
    )
    missing = analyze_entry(candles[:8], minimum_liquidity_usd=25_000)
    assert missing.status == "gap"
    assert missing.confirmed is False


def test_forward_signal_requires_first_observed_values() -> None:
    candles = _verified_trend(8)
    now = candles[-1].opened_at + 61
    assert analyze_entry(candles, minimum_liquidity_usd=25_000, as_of=now).confirmed
    unknown = [*candles[:-1], replace(candles[-1], first_observed_values_at=None)]
    signal = analyze_entry(unknown, minimum_liquidity_usd=25_000, as_of=now)
    assert signal.status == "unverified_values"
    assert signal.confirmed is False
    impossible = [
        replace(c, first_fetched_at=c.opened_at + 59, first_observed_values_at=c.opened_at + 59)
        for c in candles
    ]
    assert analyze_entry(impossible, minimum_liquidity_usd=25_000, as_of=now).status == (
        "unverified_values"
    )


def test_replay_rejects_legacy_snapshot_candles() -> None:
    candles = _verified_trend(10)
    candles[0] = replace(candles[0], source="legacy_snapshot_proxy")
    result = run_backtest(
        candles,
        minimum_liquidity_usd=25_000,
        tp_levels_bps=(10_000,),
        tp_sell_bps=(10_000,),
        stop_loss_bps=3_500,
    )
    assert result["status"] == "unverified_data"
    assert result["trades"] == 0


def test_replay_trailing_stop_uses_prior_completed_candle_peak() -> None:
    candles = _verified_trend(11)
    entry = candles[9].open
    candles[9] = replace(candles[9], high=entry * 1.5, low=entry * 0.98, close=entry * 1.4)
    candles[10] = replace(
        candles[10],
        open=entry * 1.4,
        high=entry * 1.45,
        low=entry * 1.2,
        close=entry * 1.3,
    )
    result = run_backtest(
        candles,
        minimum_liquidity_usd=25_000,
        tp_levels_bps=(100_000,),
        tp_sell_bps=(10_000,),
        stop_loss_bps=3_500,
    )
    assert result["trade_log"][0]["exit_reason"] == "trailing_stop"
    assert result["trade_log"][0]["exit_at"] == candles[10].opened_at


def test_replay_max_hold_and_costs_reduce_portfolio_return() -> None:
    candles = _verified_trend(11)
    free = run_backtest(
        candles,
        minimum_liquidity_usd=25_000,
        tp_levels_bps=(100_000,),
        tp_sell_bps=(10_000,),
        stop_loss_bps=3_500,
        max_hold_seconds=60,
        execution_cost_bps=0,
    )
    costly = run_backtest(
        candles,
        minimum_liquidity_usd=25_000,
        tp_levels_bps=(100_000,),
        tp_sell_bps=(10_000,),
        stop_loss_bps=3_500,
        max_hold_seconds=60,
        execution_cost_bps=200,
    )
    assert free["trade_log"][0]["exit_reason"] == "max_hold_time"
    assert costly["return_pct"] < free["return_pct"]


def test_replay_charges_fixed_reserve_once_and_keeps_refund_hypothetical() -> None:
    result = run_backtest(
        _verified_trend(22),
        minimum_liquidity_usd=25_000,
        tp_levels_bps=(100_000,),
        tp_sell_bps=(10_000,),
        stop_loss_bps=3_500,
        max_hold_seconds=60,
        max_consecutive_losses=10,
        max_drawdown_bps=9_999,
        daily_loss_bps=9_999,
        execution_cost_bps=3_000,
        fixed_cost_bps=2_900,
        refundable_ata_reserve_bps=2_500,
    )
    assert result["trades"] >= 2
    first, second = result["trade_log"][:2]
    gross_first = 1 + float(first["gross_return_pct"]) / 100
    assert float(first["return_pct"]) == pytest.approx(
        (gross_first * 0.99 - 0.29 - 1) * 100, abs=0.0002
    )
    assert first["rent_reserved_bps"] == 2_500
    assert second["rent_reserved_bps"] == 0
    assert second["fixed_cost_bps"] == 400
    assert first["return_if_full_rent_reclaimed_pct"] == pytest.approx(
        float(first["return_pct"]) + 25, abs=0.0002
    )
    assert result["full_rent_recovery_sensitivity"]["return_pct"] > result["return_pct"]
    already_reserved = run_backtest(
        _verified_trend(11),
        minimum_liquidity_usd=25_000,
        tp_levels_bps=(100_000,),
        tp_sell_bps=(10_000,),
        stop_loss_bps=3_500,
        max_hold_seconds=60,
        execution_cost_bps=3_000,
        fixed_cost_bps=2_900,
        refundable_ata_reserve_bps=2_500,
        ata_already_reserved=True,
    )
    assert already_reserved["trade_log"][0]["rent_reserved_bps"] == 0
    assert already_reserved["trade_log"][0]["fixed_cost_bps"] == 400


def test_replay_charges_each_extra_partial_exit_transaction() -> None:
    candles = _verified_trend(11)
    entry = candles[9].open
    candles[9] = replace(candles[9], high=entry * 1.25, low=entry * 0.99, close=entry * 1.15)
    candles[10] = replace(
        candles[10], open=entry * 1.15, high=entry * 1.2, low=entry * 0.9, close=entry * 1.1
    )
    free = run_backtest(
        candles,
        minimum_liquidity_usd=25_000,
        tp_levels_bps=(2_000, 100_000),
        tp_sell_bps=(5_000, 5_000),
        stop_loss_bps=3_500,
        max_hold_seconds=60,
    )
    charged = run_backtest(
        candles,
        minimum_liquidity_usd=25_000,
        tp_levels_bps=(2_000, 100_000),
        tp_sell_bps=(5_000, 5_000),
        stop_loss_bps=3_500,
        max_hold_seconds=60,
        additional_exit_fee_bps=100,
    )
    single = run_backtest(
        candles,
        minimum_liquidity_usd=25_000,
        tp_levels_bps=(100_000,),
        tp_sell_bps=(10_000,),
        stop_loss_bps=3_500,
        max_hold_seconds=60,
        additional_exit_fee_bps=100,
    )

    assert free["trade_log"][0]["exit_legs"] == 2
    assert charged["trade_log"][0]["additional_exit_cost_bps"] == 100
    assert charged["trade_log"][0]["return_pct"] == free["trade_log"][0]["return_pct"] - 1
    assert single["trade_log"][0]["exit_legs"] == 1
    assert single["trade_log"][0]["additional_exit_cost_bps"] == 0


def test_research_cohort_stays_stable_and_includes_blocked_discoveries(tmp_path: Path) -> None:
    with TradeJournal(tmp_path / "cohort.sqlite3") as journal:
        mints = [str(Pubkey.new_unique()) for _ in range(30)]
        for index, mint in enumerate(mints):
            journal.upsert_discovery_candidate(
                mint=mint,
                source_program="test",
                audit_status="blocked" if index % 2 else "safe",
                audit_summary=None,
                top10_concentration_bps=None,
                decimals=6,
            )
        now = int(time.time())
        first = select_research_cohort(journal, limit=20, now=now)
        assert len(first) == 20
        assert all(journal.first_research_selected_at(mint) == now for mint in first)
        assert set(first) <= set(mints)
        assert any(index % 2 for index, mint in enumerate(mints) if mint in first)
        state = journal.runtime_state(RESEARCH_COHORT_KEY)
        assert state is not None
        assert state["population_count"] == 30
        assert state["selection_method"] == RESEARCH_SELECTION_METHOD
        assert state["focus_size"] == RESEARCH_FOCUS_SIZE
        assert research_focus_size(journal) == RESEARCH_FOCUS_SIZE

        for _ in range(10):
            journal.upsert_discovery_candidate(
                mint=str(Pubkey.new_unique()),
                source_program="test",
                audit_status="safe",
                audit_summary=None,
                top10_concentration_bps=None,
                decimals=6,
            )
        assert select_research_cohort(journal, limit=20, now=now + 1) == first
        assert all(journal.first_research_selected_at(mint) == now for mint in first)
        select_research_cohort(journal, limit=20, now=now + RESEARCH_COHORT_SECONDS + 1)
        rotated = journal.runtime_state(RESEARCH_COHORT_KEY)
        assert rotated is not None
        assert rotated["selected_at"] == now + RESEARCH_COHORT_SECONDS + 1
        assert rotated["population_count"] == 0
        assert rotated["mints"] == []
        assert rotated["focus_size"] == RESEARCH_FOCUS_SIZE


def test_existing_research_cohort_keeps_eight_focus_mints_until_rotation(tmp_path: Path) -> None:
    with TradeJournal(tmp_path / "legacy-cohort.sqlite3") as journal:
        mints = [str(Pubkey.new_unique()) for _ in range(10)]
        for mint in mints:
            journal.upsert_discovery_candidate(
                mint=mint,
                source_program="test",
                audit_status="blocked",
                audit_summary=None,
                top10_concentration_bps=None,
                decimals=6,
            )
        now = int(time.time())
        journal.set_runtime_state(RESEARCH_COHORT_KEY, {"selected_at": now, "mints": mints})
        assert research_focus_size(journal) == 8
        assert select_research_cohort(journal, now=now + 1) == mints
        assert research_focus_size(journal) == 8
        select_research_cohort(journal, now=now + RESEARCH_COHORT_SECONDS + 1)
        assert research_focus_size(journal) == RESEARCH_FOCUS_SIZE


def test_decision_followup_is_prospective_immutable_and_rotates(tmp_path: Path) -> None:
    first, second = (str(Pubkey.new_unique()) for _ in range(2))
    with TradeJournal(tmp_path / "followup.sqlite3") as journal:
        assert select_decision_followup(journal, [], now=1_000) == []
        selected = select_decision_followup(journal, [first, second], now=1_001)
        assert selected[0] in {first, second}
        state = journal.runtime_state(DECISION_FOLLOWUP_KEY)
        assert state is not None
        assert state["population_count"] == 2
        assert state["selection_method"] == DECISION_FOLLOWUP_METHOD
        assert select_decision_followup(journal, [], now=1_002) == selected
        assert select_decision_followup(journal, [first, second], now=2_000) == selected
        records = journal.decision_followup_selections()
        assert records == [
            {
                "selected_at": 1_001,
                "mint": selected[0],
                "expires_at": 1_001 + DECISION_FOLLOWUP_SECONDS,
                "population_count": 2,
                "selection_method": DECISION_FOLLOWUP_METHOD,
            }
        ]
        replacement = second if selected[0] == first else first
        assert select_decision_followup(
            journal, [replacement], now=1_001 + DECISION_FOLLOWUP_SECONDS
        ) == [replacement]
        assert len(journal.decision_followup_selections()) == 2


def test_followup_upgrade_respects_persisted_older_window_expiry(tmp_path: Path) -> None:
    old_mint, new_mint = (str(Pubkey.new_unique()) for _ in range(2))
    with TradeJournal(tmp_path / "followup-upgrade.sqlite3") as journal:
        journal.record_decision_followup_selection(
            old_mint,
            selected_at=1_000,
            expires_at=2_800,
            population_count=2,
            method="sha256_30m_market_eligible_v1",
        )
        journal.set_runtime_state(
            DECISION_FOLLOWUP_KEY,
            {
                "mint": old_mint,
                "selected_at": 1_000,
                "expires_at": 2_800,
                "selection_method": "sha256_30m_market_eligible_v1",
            },
        )
        assert select_decision_followup(journal, [new_mint], now=2_799) == [old_mint]
        assert select_decision_followup(journal, [new_mint], now=2_800) == [new_mint]
        records = journal.decision_followup_selections()
        assert len(records) == 2
        assert records[0]["selection_method"] == DECISION_FOLLOWUP_METHOD
        assert records[1]["expires_at"] == 2_800


def test_research_cohort_records_actual_time_for_later_additions(tmp_path: Path) -> None:
    with TradeJournal(tmp_path / "cohort-additions.sqlite3") as journal:
        first_mint = str(Pubkey.new_unique())
        second_mint = str(Pubkey.new_unique())
        for mint in (first_mint,):
            journal.upsert_discovery_candidate(
                mint=mint,
                source_program="test",
                audit_status="safe",
                audit_summary=None,
                top10_concentration_bps=None,
                decimals=6,
            )
        now = int(time.time())
        assert select_research_cohort(journal, limit=2, now=now) == [first_mint]
        journal.upsert_discovery_candidate(
            mint=second_mint,
            source_program="test",
            audit_status="blocked",
            audit_summary=None,
            top10_concentration_bps=None,
            decimals=6,
        )
        assert select_research_cohort(journal, limit=2, now=now + 500) == [
            first_mint,
            second_mint,
        ]
        assert journal.first_research_selected_at(first_mint) == now
        assert journal.first_research_selected_at(second_mint) == now + 500


def test_new_cohort_excludes_old_discoveries_and_keeps_legacy_history(tmp_path: Path) -> None:
    now = int(time.time())
    stale_mint, fresh_mint = (str(Pubkey.new_unique()) for _ in range(2))
    with TradeJournal(tmp_path / "fresh-cohort.sqlite3") as journal:
        for mint, status in ((stale_mint, "safe"), (fresh_mint, "blocked")):
            journal.upsert_discovery_candidate(
                mint=mint,
                source_program="test",
                audit_status=status,
                audit_summary=None,
                top10_concentration_bps=None,
                decimals=6,
            )
        journal._connection.execute(
            "UPDATE discovery_candidates SET first_seen = ? WHERE mint = ?",
            (datetime.fromtimestamp(now - 7_200, UTC).isoformat(), stale_mint),
        )
        journal._connection.execute(
            "UPDATE discovery_candidates SET first_seen = ? WHERE mint = ?",
            (datetime.fromtimestamp(now, UTC).isoformat(), fresh_mint),
        )
        journal.record_research_selection(
            [stale_mint],
            cohort_started_at=now - 7_200,
            selected_at=now - 7_200,
            expires_at=now + 3_600,
            population_count=2,
            method="sha256_day_mint_from_100_recent_first_seen",
        )
        assert select_research_cohort(journal, limit=2, now=now) == [fresh_mint]
        assert journal.first_research_selected_at(stale_mint) == now - 7_200
        assert (
            journal.first_research_selected_at(
                stale_mint, selection_method=RESEARCH_SELECTION_METHOD
            )
            is None
        )
        assert (
            journal.research_entry_windows(stale_mint, selection_method=RESEARCH_SELECTION_METHOD)
            == ()
        )


def test_empty_research_universe_clears_expired_sample(tmp_path: Path) -> None:
    with TradeJournal(tmp_path / "empty-cohort.sqlite3") as journal:
        journal.set_runtime_state(
            RESEARCH_COHORT_KEY,
            {"mints": [str(Pubkey.new_unique())], "selected_at": 1_000},
        )
        assert select_research_cohort(journal, limit=2, now=87_401) == []
        state = journal.runtime_state(RESEARCH_COHORT_KEY)
        assert state is not None
        assert state["mints"] == []
        assert state["selected_at"] == 87_401


def test_ohlcv_collection_uses_cached_pool_when_dex_provider_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    mint = str(Pubkey.new_unique())
    pool = str(Pubkey.new_unique())
    calls: list[tuple[str, str, str]] = []

    class FailedDex:
        def __enter__(self) -> FailedDex:
            return self

        def __exit__(self, *_: object) -> None:
            return None

        def tokens(self, _mints: list[str]) -> dict[str, TokenMarket]:
            raise MarketDataError("unavailable")

    class FakeOhlcv:
        def __enter__(self) -> FakeOhlcv:
            return self

        def __exit__(self, *_: object) -> None:
            return None

        def candles(self, selected_mint: str, selected_pool: str, frame: str) -> list[PoolCandle]:
            calls.append((selected_mint, selected_pool, frame))
            return [_recent_pool_candle(selected_mint, selected_pool, frame)]

    monkeypatch.setattr("memecoin_bot.strategy_lab.DexScreenerClient", FailedDex)
    monkeypatch.setattr("memecoin_bot.strategy_lab.GeckoTerminalOhlcvClient", FakeOhlcv)
    monkeypatch.setattr(
        "memecoin_bot.strategy_lab.fetch_research_pool_markets",
        lambda requested: (
            [PoolMarketObservation(mint, pool, "1", "0", "5", "-2", None)]
            if requested == {mint: pool}
            else []
        ),
    )
    with TradeJournal(tmp_path / "fallback.sqlite3") as journal:
        journal.upsert_discovery_candidate(
            mint=mint,
            source_program="test",
            audit_status="blocked",
            audit_summary=None,
            top10_concentration_bps=None,
            decimals=6,
            market={"pair_address": pool, "price_usd": "1"},
        )
        result = collect_market_snapshots(journal, limit=1)
        assert result["research_cohort_size"] == 1
        assert result["authentic_candles_refreshed"] == 1
        assert "DEX Screener unavailable" in str(result["ohlcv_errors"])
        assert len(journal.pool_candles(mint, pool, "1m")) == 1
        assert result["research_pool_probe_status"] == "success"
        assert result["research_pool_probe_recorded"] == 1
        snapshot = journal.market_snapshots(mint)[-1]
        assert snapshot["source"] == "geckoterminal_pool"
        assert snapshot["liquidity_usd"] == "0"
        assert snapshot["pool"] == pool
    assert calls == [(mint, pool, "1m")]


def test_research_pool_probe_rotates_bounded_requests_and_backs_off(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    mints = [str(Pubkey.new_unique()) for _ in range(11)]
    pools = {mint: str(Pubkey.new_unique()) for mint in mints}
    requested: list[dict[str, str]] = []

    class EmptyDex:
        def __enter__(self) -> EmptyDex:
            return self

        def __exit__(self, *_: object) -> None:
            return None

        def tokens(self, _mints: list[str]) -> dict[str, TokenMarket]:
            return {}

    class EmptyOhlcv:
        def __enter__(self) -> EmptyOhlcv:
            return self

        def __exit__(self, *_: object) -> None:
            return None

        def candles(self, *_: object) -> list[PoolCandle]:
            return []

    def capture(selected: dict[str, str]) -> list[PoolMarketObservation]:
        requested.append(selected)
        return []

    monkeypatch.setattr("memecoin_bot.strategy_lab.DexScreenerClient", EmptyDex)
    monkeypatch.setattr("memecoin_bot.strategy_lab.GeckoTerminalOhlcvClient", EmptyOhlcv)
    monkeypatch.setattr("memecoin_bot.strategy_lab.fetch_research_pool_markets", capture)
    with TradeJournal(tmp_path / "probe.sqlite3") as journal:
        for mint in mints:
            journal.upsert_discovery_candidate(
                mint=mint,
                source_program="test",
                audit_status="blocked",
                audit_summary=None,
                top10_concentration_bps=None,
                decimals=6,
                market={"pair_address": pools[mint]},
            )
        before_first = int(time.time())
        first = collect_market_snapshots(journal, limit=11)
        assert first["research_pool_probe_status"] == "success"
        assert len(requested[0]) == 10
        state = journal.runtime_state("research_pool_market_probe") or {}
        assert state["next_at"] >= before_first + 300
        journal.set_runtime_state("research_pool_market_probe", {**state, "next_at": 0})
        second = collect_market_snapshots(journal, limit=11)
        assert second["research_pool_probe_status"] == "success"
        assert len(requested[1]) == 10
        assert set(requested[0]) | set(requested[1]) == set(mints)

        def limited(_: dict[str, str]) -> list[PoolMarketObservation]:
            raise OhlcvRateLimited("limited", retry_after_seconds=60)

        monkeypatch.setattr("memecoin_bot.strategy_lab.fetch_research_pool_markets", limited)
        state = journal.runtime_state("research_pool_market_probe") or {}
        journal.set_runtime_state("research_pool_market_probe", {**state, "next_at": 0})
        before = int(time.time())
        limited_cycle = collect_market_snapshots(journal, limit=11)
        assert limited_cycle["research_pool_probe_status"] == "rate_limited"
        assert (journal.runtime_state("research_pool_market_probe") or {})[
            "next_at"
        ] >= before + 600
        assert limited_cycle["ohlcv_higher_budget_retry_after"] >= before + 3_600

        for mint in mints:
            journal.upsert_discovery_candidate(
                mint=mint,
                source_program="test",
                audit_status="safe",
                audit_summary=None,
                top10_concentration_bps=1_000,
                decimals=6,
                market={"pair_address": pools[mint], "liquidity_usd": "50000"},
            )
        state = journal.runtime_state("research_pool_market_probe") or {}
        journal.set_runtime_state("research_pool_market_probe", {**state, "next_at": 0})
        journal.set_runtime_state("ohlcv_request_budget", {"limit": 6})
        deferred = collect_market_snapshots(
            journal, limit=11, active_candidate_priority=lambda _: True
        )
        assert deferred["research_pool_probe_status"] == "deferred_for_candle_capacity"
        assert len(requested) == 2


def test_safe_paper_candidate_receives_candles_outside_research_cohort(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    blocked_mint, safe_mint = (str(Pubkey.new_unique()) for _ in range(2))
    pools = {mint: str(Pubkey.new_unique()) for mint in (blocked_mint, safe_mint)}
    requested: list[list[str]] = []
    fetched: list[str] = []

    class FakeDex:
        def __enter__(self) -> FakeDex:
            return self

        def __exit__(self, *_: object) -> None:
            return None

        def tokens(self, mints: list[str]) -> dict[str, TokenMarket]:
            requested.append(mints)
            return {}

    class FakeOhlcv:
        def __enter__(self) -> FakeOhlcv:
            return self

        def __exit__(self, *_: object) -> None:
            return None

        def candles(self, mint: str, pool: str, frame: str) -> list[PoolCandle]:
            fetched.append(mint)
            return [_recent_pool_candle(mint, pool, frame)]

    monkeypatch.setattr("memecoin_bot.strategy_lab.DexScreenerClient", FakeDex)
    monkeypatch.setattr("memecoin_bot.strategy_lab.GeckoTerminalOhlcvClient", FakeOhlcv)
    with TradeJournal(tmp_path / "safe-watchlist.sqlite3") as journal:
        for mint, status in ((blocked_mint, "blocked"), (safe_mint, "safe")):
            journal.upsert_discovery_candidate(
                mint=mint,
                source_program="test",
                audit_status=status,
                audit_summary=None,
                top10_concentration_bps=None,
                decimals=6,
                market={"pair_address": pools[mint], "price_usd": "1", "liquidity_usd": "50000"},
            )
        journal.set_runtime_state(
            RESEARCH_COHORT_KEY,
            {"selected_at": int(time.time()), "mints": [blocked_mint]},
        )
        result = collect_market_snapshots(journal, limit=1)
        assert result["research_cohort_size"] == 1
        assert result["safe_watchlist_size"] == 1
        assert result["safe_watchlist_tracked"] == 1
        assert research_cohort_mints(journal) == [blocked_mint]
        assert journal.first_research_selected_at(safe_mint) is None
        assert result["collection_lane"] == "active_research_1m"
        assert len(journal.pool_candles(safe_mint, pools[safe_mint], "1m")) == 1
        assert fetched == [safe_mint, blocked_mint]
        lanes = [result["collection_lane"]]
        for _ in range(7):
            before = len(fetched)
            cycle = collect_market_snapshots(journal, limit=1)
            lanes.append(str(cycle["collection_lane"]))
            assert len(fetched) - before <= 4
        assert lanes == [
            "active_research_1m",
            "active_research_1m",
            "active_research_1m",
            "active_deep",
            "active_research_1m",
            "active_research_1m",
            "active_research_1m",
            "research_1m",
        ]
        assert len(journal.pool_candles(blocked_mint, pools[blocked_mint], "1m")) == 1
        assert len(journal.pool_candles(safe_mint, pools[safe_mint], "1h")) == 1
    assert requested == [[blocked_mint, safe_mint]] * 8
    assert fetched.count(safe_mint) == 9
    assert fetched.count(blocked_mint) == 8


def test_research_cycle_samples_both_focus_and_remaining_cohort(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    mints = [str(Pubkey.new_unique()) for _ in range(20)]
    pools = {mint: str(Pubkey.new_unique()) for mint in mints}
    calls: list[str] = []

    class FakeDex:
        def __enter__(self) -> FakeDex:
            return self

        def __exit__(self, *_: object) -> None:
            return None

        def tokens(self, _mints: list[str]) -> dict[str, TokenMarket]:
            return {}

    class FakeOhlcv:
        def __enter__(self) -> FakeOhlcv:
            return self

        def __exit__(self, *_: object) -> None:
            return None

        def candles(self, mint: str, _pool: str, _frame: str) -> list[PoolCandle]:
            calls.append(mint)
            return []

    monkeypatch.setattr("memecoin_bot.strategy_lab.DexScreenerClient", FakeDex)
    monkeypatch.setattr("memecoin_bot.strategy_lab.GeckoTerminalOhlcvClient", FakeOhlcv)
    with TradeJournal(tmp_path / "research-lanes.sqlite3") as journal:
        for mint in mints:
            journal.upsert_discovery_candidate(
                mint=mint,
                source_program="test",
                audit_status="blocked",
                audit_summary=None,
                top10_concentration_bps=None,
                decimals=6,
                market={"pair_address": pools[mint]},
            )
        journal.set_runtime_state(
            RESEARCH_COHORT_KEY,
            {"selected_at": int(time.time()), "mints": mints},
        )
        journal.set_runtime_state("ohlcv_schedule", {"cycle": 7})
        result = collect_market_snapshots(journal, limit=20)

    assert result["collection_lane"] == "research_1m"
    assert len(calls) == 4
    assert calls[0] in mints[:8]
    assert calls[1] in mints[8:]
    assert len(set(calls) & set(mints[:8])) == 2
    assert len(set(calls) & set(mints[8:])) == 2
    assert result["research_secondary_attempted"] == 2
    assert result["research_secondary_succeeded"] == 2


def test_active_cohort_mints_get_timely_slots_without_changing_membership(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    mints = [str(Pubkey.new_unique()) for _ in range(20)]
    pools = {mint: str(Pubkey.new_unique()) for mint in mints}
    active_focus, active_secondary = mints[3], mints[11]
    calls: list[str] = []

    class FakeDex:
        def __enter__(self) -> FakeDex:
            return self

        def __exit__(self, *_: object) -> None:
            return None

        def tokens(self, requested: list[str]) -> dict[str, TokenMarket]:
            return {
                mint: TokenMarket(
                    mint,
                    None,
                    None,
                    None,
                    pools[mint],
                    "1",
                    "50000",
                    "100000",
                    "1",
                    int(time.time() * 1000),
                    txns_5m_buys=1,
                )
                for mint in (active_focus, active_secondary)
                if mint in requested
            }

    class FakeOhlcv:
        def __enter__(self) -> FakeOhlcv:
            return self

        def __exit__(self, *_: object) -> None:
            return None

        def candles(self, mint: str, _pool: str, _frame: str) -> list[PoolCandle]:
            calls.append(mint)
            return []

    monkeypatch.setattr("memecoin_bot.strategy_lab.DexScreenerClient", FakeDex)
    monkeypatch.setattr("memecoin_bot.strategy_lab.GeckoTerminalOhlcvClient", FakeOhlcv)
    with TradeJournal(tmp_path / "activity-scheduling.sqlite3") as journal:
        for mint in mints:
            journal.upsert_discovery_candidate(
                mint=mint,
                source_program="test",
                audit_status="blocked",
                audit_summary=None,
                top10_concentration_bps=None,
                decimals=6,
                market={"pair_address": pools[mint]},
            )
        journal.set_runtime_state(
            RESEARCH_COHORT_KEY,
            {"selected_at": int(time.time()), "mints": mints},
        )
        result = collect_market_snapshots(journal, limit=20)
        assert research_cohort_mints(journal) == mints
        journal.set_runtime_state("ohlcv_retry_cooldown", {"entries": {}})
        journal.set_runtime_state("ohlcv_schedule", {"cycle": 1})
        collect_market_snapshots(journal, limit=20)

    assert len(calls) == 8
    assert set(calls[:2]) == {active_focus, active_secondary}
    assert result["research_active_5m_size"] == 2
    assert result["research_active_5m_scheduled"] == 2
    assert result["research_secondary_attempted"] == 1
    assert result["research_active_focus_size"] == 1
    assert result["research_active_focus_scheduled"] == 1
    assert calls[4] == active_focus


def test_research_active_focus_records_conditioned_selection_and_replacement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    blocked, safe, market_ready = [str(Pubkey.new_unique()) for _ in range(3)]
    cohort = [blocked, safe, market_ready]
    pools = {mint: str(Pubkey.new_unique()) for mint in cohort}
    active = set(cohort)

    class FakeDex:
        def __enter__(self) -> FakeDex:
            return self

        def __exit__(self, *_: object) -> None:
            return None

        def tokens(self, requested: list[str]) -> dict[str, TokenMarket]:
            return {
                mint: TokenMarket(
                    mint,
                    None,
                    None,
                    None,
                    pools[mint],
                    "1",
                    "50000",
                    "100000",
                    "5",
                    int(time.time() * 1000),
                    volume_5m_usd="100" if mint in active else "0",
                    txns_5m_buys=1 if mint in active else 0,
                )
                for mint in requested
            }

    class FakeOhlcv:
        def __enter__(self) -> FakeOhlcv:
            return self

        def __exit__(self, *_: object) -> None:
            return None

        def candles(self, mint: str, pool: str, frame: str) -> list[PoolCandle]:
            return [_recent_pool_candle(mint, pool, frame)]

    monkeypatch.setattr("memecoin_bot.strategy_lab.DexScreenerClient", FakeDex)
    monkeypatch.setattr("memecoin_bot.strategy_lab.GeckoTerminalOhlcvClient", FakeOhlcv)
    with TradeJournal(tmp_path / "research-pin.sqlite3") as journal:
        for mint in cohort:
            journal.upsert_discovery_candidate(
                mint=mint,
                source_program="test",
                audit_status="blocked" if mint == blocked else "safe",
                audit_summary=None,
                top10_concentration_bps=None,
                decimals=6,
                market={"pair_address": pools[mint]},
            )
        cohort_selected_at = int(time.time())
        journal.set_runtime_state(
            RESEARCH_COHORT_KEY,
            {"selected_at": cohort_selected_at, "mints": cohort},
        )
        first = collect_market_snapshots(
            journal,
            active_candidate_priority=lambda item: item["mint"] == market_ready,
        )
        selected = journal.runtime_state("ohlcv_research_active_focus") or {}
        assert selected["mint"] == market_ready
        assert selected["selection_tier"] == "market_eligible"
        assert first["research_active_focus_scheduled"] == 1

        active.remove(market_ready)
        second = collect_market_snapshots(
            journal,
            active_candidate_priority=lambda item: item["mint"] == market_ready,
        )
        replaced = journal.runtime_state("ohlcv_research_active_focus") or {}
        assert replaced["mint"] == safe
        assert replaced["selection_tier"] == "audit_safe"
        assert replaced["history"][-1]["mint"] == market_ready
        assert replaced["history"][-1]["cohort_selected_at"] == cohort_selected_at
        assert second["research_active_focus_scheduled"] == 1
        assert research_cohort_mints(journal) == cohort


def test_ohlcv_rate_limit_cools_down_and_preserves_interrupted_mint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first, second = (str(Pubkey.new_unique()) for _ in range(2))
    pools = {mint: str(Pubkey.new_unique()) for mint in (first, second)}
    calls: list[tuple[str, str]] = []
    limited = True

    class FakeDex:
        def __enter__(self) -> FakeDex:
            return self

        def __exit__(self, *_: object) -> None:
            return None

        def tokens(self, _mints: list[str]) -> dict[str, TokenMarket]:
            return {}

    class FakeOhlcv:
        def __enter__(self) -> FakeOhlcv:
            return self

        def __exit__(self, *_: object) -> None:
            return None

        def candles(self, mint: str, pool: str, frame: str) -> list[PoolCandle]:
            nonlocal limited
            calls.append((mint, frame))
            if limited:
                limited = False
                raise OhlcvRateLimited("quota", retry_after_seconds=90)
            return [_recent_pool_candle(mint, pool, frame)]

    monkeypatch.setattr("memecoin_bot.strategy_lab.DexScreenerClient", FakeDex)
    monkeypatch.setattr("memecoin_bot.strategy_lab.GeckoTerminalOhlcvClient", FakeOhlcv)
    with TradeJournal(tmp_path / "rate-limit.sqlite3") as journal:
        for mint in (first, second):
            journal.upsert_discovery_candidate(
                mint=mint,
                source_program="test",
                audit_status="blocked",
                audit_summary=None,
                top10_concentration_bps=None,
                decimals=6,
                market={"pair_address": pools[mint], "price_usd": "1"},
            )
        journal.set_runtime_state(
            RESEARCH_COHORT_KEY,
            {"selected_at": int(time.time()), "mints": [first, second]},
        )
        journal.set_runtime_state("ohlcv_schedule", {"cycle": 0})
        first_result = collect_market_snapshots(journal, limit=2)
        assert first_result["ohlcv_rate_limited"] is True
        assert first_result["ohlcv_error_codes"] == {"rate_limit": 1}
        assert (journal.runtime_state("ohlcv_schedule") or {})["cycle"] == 0
        assert calls == [(first, "1m")]

        cooldown_result = collect_market_snapshots(journal, limit=2)
        assert cooldown_result["ohlcv_rate_limited"] is True
        assert calls == [(first, "1m")]

        journal.set_runtime_state("ohlcv_backoff", {"until": 0})
        recovered = collect_market_snapshots(journal, limit=2)
        assert recovered["ohlcv_rate_limited"] is False
        assert recovered["authentic_candles_refreshed"] == 2
        assert calls[1] == (first, "1m")
        assert (journal.runtime_state("ohlcv_schedule") or {})["cycle"] == 1
        cycles = (journal.runtime_state("ohlcv_request_budget") or {})["recent_cycles"]
        assert [(row["attempted"], row["succeeded"], row["rate_limited"]) for row in cycles] == [
            (1, 0, True),
            (0, 0, True),
            (2, 2, False),
        ]


def test_research_lane_is_retried_after_quota_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    research_mint, safe_mint = (str(Pubkey.new_unique()) for _ in range(2))
    pools = {mint: str(Pubkey.new_unique()) for mint in (research_mint, safe_mint)}
    calls: list[str] = []

    class FakeDex:
        def __enter__(self) -> FakeDex:
            return self

        def __exit__(self, *_: object) -> None:
            return None

        def tokens(self, _mints: list[str]) -> dict[str, TokenMarket]:
            return {}

    class FakeOhlcv:
        def __enter__(self) -> FakeOhlcv:
            return self

        def __exit__(self, *_: object) -> None:
            return None

        def candles(self, mint: str, pool: str, frame: str) -> list[PoolCandle]:
            calls.append(mint)
            if len(calls) == 1:
                raise OhlcvRateLimited("quota", retry_after_seconds=90)
            return [_recent_pool_candle(mint, pool, frame)]

    monkeypatch.setattr("memecoin_bot.strategy_lab.DexScreenerClient", FakeDex)
    monkeypatch.setattr("memecoin_bot.strategy_lab.GeckoTerminalOhlcvClient", FakeOhlcv)
    with TradeJournal(tmp_path / "research-retry.sqlite3") as journal:
        for mint, status in ((research_mint, "blocked"), (safe_mint, "safe")):
            journal.upsert_discovery_candidate(
                mint=mint,
                source_program="test",
                audit_status=status,
                audit_summary=None,
                top10_concentration_bps=None,
                decimals=6,
                market={"pair_address": pools[mint], "price_usd": "1"},
            )
        journal.set_runtime_state(
            RESEARCH_COHORT_KEY,
            {"selected_at": int(time.time()), "mints": [research_mint]},
        )
        journal.set_runtime_state("ohlcv_schedule", {"cycle": 7})
        first = collect_market_snapshots(journal, limit=1)
        assert first["collection_lane"] == "research_1m"
        assert first["ohlcv_rate_limited"] is True
        assert (journal.runtime_state("ohlcv_schedule") or {})["cycle"] == 7
        journal.set_runtime_state("ohlcv_backoff", {"until": 0})
        recovered = collect_market_snapshots(journal, limit=1)
        assert recovered["collection_lane"] == "research_1m"
        assert (journal.runtime_state("ohlcv_schedule") or {})["cycle"] == 8
        assert calls == [research_mint, research_mint]


def test_short_collection_refreshes_all_four_safe_candidates_without_duplicate_five_minute_calls(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    mints = [str(Pubkey.new_unique()) for _ in range(4)]
    pools = {mint: str(Pubkey.new_unique()) for mint in mints}
    calls: list[tuple[str, str]] = []

    class FakeDex:
        def __enter__(self) -> FakeDex:
            return self

        def __exit__(self, *_: object) -> None:
            return None

        def tokens(self, _mints: list[str]) -> dict[str, TokenMarket]:
            return {}

    class FakeOhlcv:
        def __enter__(self) -> FakeOhlcv:
            return self

        def __exit__(self, *_: object) -> None:
            return None

        def candles(self, mint: str, pool: str, frame: str) -> list[PoolCandle]:
            calls.append((mint, frame))
            return [_recent_pool_candle(mint, pool, frame)]

    monkeypatch.setattr("memecoin_bot.strategy_lab.DexScreenerClient", FakeDex)
    monkeypatch.setattr("memecoin_bot.strategy_lab.GeckoTerminalOhlcvClient", FakeOhlcv)
    with TradeJournal(tmp_path / "rotate.sqlite3") as journal:
        for mint in mints:
            journal.upsert_discovery_candidate(
                mint=mint,
                source_program="test",
                audit_status="safe",
                audit_summary=None,
                top10_concentration_bps=None,
                decimals=6,
                market={"pair_address": pools[mint], "price_usd": "1"},
            )
        first = collect_market_snapshots(journal, limit=1)
        first_mints = {mint for mint, _ in calls}
        assert first["active_watchlist_size"] == 4
        assert len(first_mints) == 4
        assert {frame for _, frame in calls} == {"1m"}

        calls.clear()
        second = collect_market_snapshots(journal, limit=1)
        second_mints = {mint for mint, _ in calls}
        assert second["collection_lane"] == "active_research_1m"
        assert len(second_mints) == 4
        assert first_mints == second_mints == set(mints)


def test_deep_collection_keeps_another_active_candidate_fresh(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    mints = [str(Pubkey.new_unique()) for _ in range(2)]
    pools = {mint: str(Pubkey.new_unique()) for mint in mints}
    calls: list[tuple[str, str]] = []

    class FakeDex:
        def __enter__(self) -> FakeDex:
            return self

        def __exit__(self, *_: object) -> None:
            return None

        def tokens(self, _mints: list[str]) -> dict[str, TokenMarket]:
            return {}

    class FakeOhlcv:
        def __enter__(self) -> FakeOhlcv:
            return self

        def __exit__(self, *_: object) -> None:
            return None

        def candles(self, mint: str, pool: str, frame: str) -> list[PoolCandle]:
            calls.append((mint, frame))
            return [_recent_pool_candle(mint, pool, frame)]

    monkeypatch.setattr("memecoin_bot.strategy_lab.DexScreenerClient", FakeDex)
    monkeypatch.setattr("memecoin_bot.strategy_lab.GeckoTerminalOhlcvClient", FakeOhlcv)
    with TradeJournal(tmp_path / "deep-active.sqlite3") as journal:
        for mint in mints:
            journal.upsert_discovery_candidate(
                mint=mint,
                source_program="test",
                audit_status="safe",
                audit_summary=None,
                top10_concentration_bps=None,
                decimals=6,
                market={"pair_address": pools[mint], "price_usd": "1"},
            )
        journal.set_runtime_state("ohlcv_schedule", {"cycle": 3})
        result = collect_market_snapshots(journal, limit=1)

    assert result["collection_lane"] == "active_deep"
    assert len(calls) == 4
    assert {mint for mint, _ in calls} == set(mints)
    assert {frame for _, frame in calls} == {"1m", "15m", "1h"}
    assert len({mint for mint, selected in calls if selected == "1m"}) == 2
    assert all(
        len({mint for mint, selected in calls if selected == frame}) == 1 for frame in ("15m", "1h")
    )


def test_free_collection_repairs_five_minute_gap_without_losing_research_sample(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    active = [str(Pubkey.new_unique()) for _ in range(2)]
    research = [str(Pubkey.new_unique()) for _ in range(4)]
    pools = {mint: str(Pubkey.new_unique()) for mint in [*active, *research]}
    calls: list[tuple[str, str]] = []
    minute_end = int(time.time()) // 60 * 60
    five_end = int(time.time()) // 300 * 300

    def bar(opened_at: int, frame: str) -> Candle:
        period = TIMEFRAME_SECONDS[frame]
        return Candle(
            opened_at=opened_at,
            open=1,
            high=1,
            low=1,
            close=1,
            volume_delta_usd=100,
            liquidity_usd=50_000,
            samples=1,
            source="geckoterminal_pool_trades",
            first_fetched_at=opened_at + period,
            first_observed_values_at=opened_at + period,
        )

    minutes = [bar(minute_end - (8 - index) * 60, "1m") for index in range(8)]
    five = [bar(five_end - (9 - index) * 300, "5m") for index in range(9) if index != 4]

    class FakeDex:
        def __enter__(self) -> FakeDex:
            return self

        def __exit__(self, *_: object) -> None:
            return None

        def tokens(self, _mints: list[str]) -> dict[str, TokenMarket]:
            return {}

    class FakeOhlcv:
        def __enter__(self) -> FakeOhlcv:
            return self

        def __exit__(self, *_: object) -> None:
            return None

        def candles(self, mint: str, pool: str, frame: str) -> list[PoolCandle]:
            calls.append((mint, frame))
            return [_recent_pool_candle(mint, pool, frame)]

    def saved_candles(
        _journal: TradeJournal,
        _mint: str,
        _pool: str,
        frame: str,
        *,
        now: int | None = None,
    ) -> list[Candle]:
        return minutes if frame == "1m" else five if frame == "5m" else []

    monkeypatch.setattr("memecoin_bot.strategy_lab.DexScreenerClient", FakeDex)
    monkeypatch.setattr("memecoin_bot.strategy_lab.GeckoTerminalOhlcvClient", FakeOhlcv)
    monkeypatch.setattr("memecoin_bot.strategy_lab.load_verified_candles", saved_candles)
    with TradeJournal(tmp_path / "repair.sqlite3") as journal:
        for mint in [*active, *research]:
            journal.upsert_discovery_candidate(
                mint=mint,
                source_program="test",
                audit_status="safe" if mint in active else "blocked",
                audit_summary=None,
                top10_concentration_bps=None,
                decimals=6,
                market={"pair_address": pools[mint]},
            )
        journal.set_runtime_state(
            RESEARCH_COHORT_KEY,
            {"selected_at": int(time.time()), "mints": research},
        )
        first = collect_market_snapshots(
            journal, active_candidate_priority=lambda candidate: candidate["mint"] in active
        )
        assert first["direct_5m_repair_scheduled"] is True
        assert len(calls) == 4
        assert len([mint for mint, frame in calls if frame == "5m" and mint in active]) == 1
        assert {mint for mint, frame in calls if frame == "1m" and mint in active} == set(active)
        assert any(mint in research and frame == "1m" for mint, frame in calls)
        repaired_first = next(mint for mint, frame in calls if frame == "5m")

        calls.clear()
        second = collect_market_snapshots(
            journal, active_candidate_priority=lambda candidate: candidate["mint"] in active
        )
        assert second["direct_5m_repair_scheduled"] is False
        assert all(frame != "5m" for _, frame in calls)

        history = (journal.runtime_state("ohlcv_direct_5m_repair_history") or {})["history"]
        history[-1]["selected_at"] = int(time.time()) - 301
        journal.set_runtime_state("ohlcv_direct_5m_repair_history", {"history": history})
        calls.clear()
        third = collect_market_snapshots(
            journal, active_candidate_priority=lambda candidate: candidate["mint"] in active
        )
        assert third["direct_5m_repair_scheduled"] is True
        repaired_second = next(mint for mint, frame in calls if frame == "5m")
        assert repaired_second in active and repaired_second != repaired_first

        calls.clear()
        fourth = collect_market_snapshots(
            journal, active_candidate_priority=lambda candidate: candidate["mint"] in active
        )
        assert fourth["direct_5m_repair_scheduled"] is False
        assert all(frame != "5m" for _, frame in calls)
        history = (journal.runtime_state("ohlcv_direct_5m_repair_history") or {})["history"]
        assert [row["mint"] for row in history] == [repaired_first, repaired_second]
        assert all(row["reason"] == "gap" and row["request_succeeded"] for row in history)
        assert all(row["bars_returned"] == 1 for row in history)

    followup = str(Pubkey.new_unique())
    pools[followup] = str(Pubkey.new_unique())
    monkeypatch.setattr(
        "memecoin_bot.strategy_lab.select_decision_followup", lambda *_args: [followup]
    )
    calls.clear()
    with TradeJournal(tmp_path / "followup-repair.sqlite3") as journal:
        for mint in [*active, followup, research[0]]:
            journal.upsert_discovery_candidate(
                mint=mint,
                source_program="test",
                audit_status="safe" if mint != research[0] else "blocked",
                audit_summary=None,
                top10_concentration_bps=None,
                decimals=6,
                market={"pair_address": pools[mint]},
            )
        current = int(time.time())
        journal.set_runtime_state(
            RESEARCH_COHORT_KEY, {"selected_at": current, "mints": [research[0]]}
        )
        journal.set_runtime_state(
            "ohlcv_active_focus",
            {
                "selected_at": current,
                "mints": active,
                "last_selected_at": {mint: current for mint in active},
            },
        )
        result = collect_market_snapshots(
            journal,
            limit=1,
            active_candidate_priority=lambda candidate: candidate["mint"] in [*active, followup],
        )
        assert result["direct_5m_repair_scheduled"] is True
        assert result["decision_followup_attempted"] == 0
        assert len(calls) == 4
        assert all((mint, "1m") in calls for mint in [*active, research[0]])
        assert (followup, "1m") not in calls
        repair = (journal.runtime_state("ohlcv_direct_5m_repair_history") or {})["history"][-1]
        assert repair["displaced_1m_mint"] == followup
        assert repair["displaced_decision_followup"] is True

    five[-1] = replace(
        five[-1], ohlc_disagreement_pct=1.5, overlap_observed_at=int(time.time()) - 1
    )
    monkeypatch.setattr("memecoin_bot.strategy_lab.select_decision_followup", lambda *_args: [])
    calls.clear()
    with TradeJournal(tmp_path / "known-conflict.sqlite3") as journal:
        for mint in [*active, research[0]]:
            journal.upsert_discovery_candidate(
                mint=mint,
                source_program="test",
                audit_status="safe" if mint in active else "blocked",
                audit_summary=None,
                top10_concentration_bps=None,
                decimals=6,
                market={"pair_address": pools[mint]},
            )
        journal.set_runtime_state(
            RESEARCH_COHORT_KEY,
            {"selected_at": int(time.time()), "mints": [research[0]]},
        )
        no_repair = collect_market_snapshots(
            journal,
            limit=1,
            active_candidate_priority=lambda candidate: candidate["mint"] in active,
        )
        assert no_repair["direct_5m_repair_scheduled"] is False
        assert all(frame != "5m" for _, frame in calls)


def test_fixed_research_focus_gets_point_in_time_cadence_with_active_candidates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    focus = [str(Pubkey.new_unique()) for _ in range(8)]
    active = [str(Pubkey.new_unique()) for _ in range(4)]
    pools = {mint: str(Pubkey.new_unique()) for mint in [*focus, *active]}
    calls: list[tuple[str, str]] = []

    class FakeDex:
        def __enter__(self) -> FakeDex:
            return self

        def __exit__(self, *_: object) -> None:
            return None

        def tokens(self, _mints: list[str]) -> dict[str, TokenMarket]:
            return {}

    class FakeOhlcv:
        def __enter__(self) -> FakeOhlcv:
            return self

        def __exit__(self, *_: object) -> None:
            return None

        def candles(self, mint: str, pool: str, frame: str) -> list[PoolCandle]:
            calls.append((mint, frame))
            return [_recent_pool_candle(mint, pool, frame)]

    monkeypatch.setattr("memecoin_bot.strategy_lab.DexScreenerClient", FakeDex)
    monkeypatch.setattr("memecoin_bot.strategy_lab.GeckoTerminalOhlcvClient", FakeOhlcv)
    with TradeJournal(tmp_path / "focus-cadence.sqlite3") as journal:
        for mint in [*focus, *active]:
            journal.upsert_discovery_candidate(
                mint=mint,
                source_program="test",
                audit_status="blocked" if mint in focus else "safe",
                audit_summary=None,
                top10_concentration_bps=None,
                decimals=6,
                market={"pair_address": pools[mint], "price_usd": "1"},
            )
        journal.set_runtime_state(
            RESEARCH_COHORT_KEY, {"selected_at": int(time.time()), "mints": focus}
        )
        per_cycle: list[list[tuple[str, str]]] = []
        for _ in range(5):
            start = len(calls)
            result = collect_market_snapshots(journal, limit=8)
            per_cycle.append(calls[start:])
            assert result["research_focus_size"] == 8
            assert len(per_cycle[-1]) <= 4
            assert result["ohlcv_requests_attempted"] == len(per_cycle[-1])
            assert result["ohlcv_requests_succeeded"] == len(per_cycle[-1])

        journal.set_runtime_state(
            RESEARCH_COHORT_KEY,
            {"selected_at": int(time.time()), "mints": focus, "focus_size": RESEARCH_FOCUS_SIZE},
        )
        start = len(calls)
        concentrated = collect_market_snapshots(journal, limit=8)
        assert concentrated["research_focus_size"] == RESEARCH_FOCUS_SIZE
        assert {mint for mint, frame in calls[start:] if mint in focus and frame == "1m"} == set(
            focus[:RESEARCH_FOCUS_SIZE]
        )
        assert len(calls[start:]) == 4

    fetched_focus = {mint for mint, frame in calls if mint in focus and frame == "1m"}
    fetched_active = {mint for mint, frame in calls if mint in active and frame == "1m"}
    assert fetched_focus == set(focus)
    assert fetched_active == set(active)
    assert sum(len(cycle) for cycle in per_cycle) == 20
    assert per_cycle[0][0][0] in active
    assert per_cycle[0][1][0] in focus


def test_stale_nonempty_pool_cools_down_without_changing_research_membership(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    stale_mint, fresh_mint = (str(Pubkey.new_unique()) for _ in range(2))
    pools = {mint: str(Pubkey.new_unique()) for mint in (stale_mint, fresh_mint)}
    calls: list[str] = []

    class FakeDex:
        def __enter__(self) -> FakeDex:
            return self

        def __exit__(self, *_: object) -> None:
            return None

        def tokens(self, _mints: list[str]) -> dict[str, TokenMarket]:
            return {}

    class FakeOhlcv:
        def __enter__(self) -> FakeOhlcv:
            return self

        def __exit__(self, *_: object) -> None:
            return None

        def candles(self, mint: str, pool: str, frame: str) -> list[PoolCandle]:
            calls.append(mint)
            if mint == stale_mint:
                return [PoolCandle(mint, pool, frame, 60, "1", "1", "1", "1", "100")]
            return [_recent_pool_candle(mint, pool, frame)]

    monkeypatch.setattr("memecoin_bot.strategy_lab.DexScreenerClient", FakeDex)
    monkeypatch.setattr("memecoin_bot.strategy_lab.GeckoTerminalOhlcvClient", FakeOhlcv)
    with TradeJournal(tmp_path / "stale-focus.sqlite3") as journal:
        for mint in (stale_mint, fresh_mint):
            journal.upsert_discovery_candidate(
                mint=mint,
                source_program="test",
                audit_status="blocked",
                audit_summary=None,
                top10_concentration_bps=None,
                decimals=6,
                market={"pair_address": pools[mint], "price_usd": "1"},
            )
        journal.set_runtime_state(
            RESEARCH_COHORT_KEY,
            {"selected_at": int(time.time()), "mints": [stale_mint, fresh_mint]},
        )
        first = collect_market_snapshots(journal, limit=2)
        second = collect_market_snapshots(journal, limit=2)
        assert research_cohort_mints(journal) == [stale_mint, fresh_mint]
        assert len(journal.pool_candles(stale_mint, pools[stale_mint], "1m")) == 1

    assert first["ohlcv_stale_responses"] == 1
    assert first["ohlcv_retry_cooldown_size"] == 1
    assert second["ohlcv_stale_responses"] == 0
    assert calls.count(stale_mint) == 1
    assert calls.count(fresh_mint) == 2


@pytest.mark.parametrize("failure_mode", ["empty", "invalid"])
def test_unavailable_research_pool_cools_down_without_removing_sample(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure_mode: str
) -> None:
    empty_mint, trading_mint = (str(Pubkey.new_unique()) for _ in range(2))
    pools = {mint: str(Pubkey.new_unique()) for mint in (empty_mint, trading_mint)}
    calls: list[str] = []

    class FakeDex:
        def __enter__(self) -> FakeDex:
            return self

        def __exit__(self, *_: object) -> None:
            return None

        def tokens(self, _mints: list[str]) -> dict[str, TokenMarket]:
            return {}

    class FakeOhlcv:
        def __enter__(self) -> FakeOhlcv:
            return self

        def __exit__(self, *_: object) -> None:
            return None

        def candles(self, mint: str, pool: str, frame: str) -> list[PoolCandle]:
            calls.append(mint)
            if mint == empty_mint and failure_mode == "invalid":
                raise OhlcvError("Provider omitted OHLCV candles")
            return [] if mint == empty_mint else [_recent_pool_candle(mint, pool, frame)]

    monkeypatch.setattr("memecoin_bot.strategy_lab.DexScreenerClient", FakeDex)
    monkeypatch.setattr("memecoin_bot.strategy_lab.GeckoTerminalOhlcvClient", FakeOhlcv)
    with TradeJournal(tmp_path / "empty-focus.sqlite3") as journal:
        for mint in (empty_mint, trading_mint):
            journal.upsert_discovery_candidate(
                mint=mint,
                source_program="test",
                audit_status="blocked",
                audit_summary=None,
                top10_concentration_bps=None,
                decimals=6,
                market={"pair_address": pools[mint], "price_usd": "1"},
            )
        journal.set_runtime_state(
            RESEARCH_COHORT_KEY,
            {"selected_at": int(time.time()), "mints": [empty_mint, trading_mint]},
        )
        first = collect_market_snapshots(journal, limit=2)
        second = collect_market_snapshots(journal, limit=2)
        assert research_cohort_mints(journal) == [empty_mint, trading_mint]
        assert journal.pool_candles(empty_mint, pools[empty_mint], "1m") == []
        assert len(journal.pool_candles(trading_mint, pools[trading_mint], "1m")) == 1
    assert first["ohlcv_empty_responses"] == (1 if failure_mode == "empty" else 0)
    assert first["ohlcv_error_codes"] == ({} if failure_mode == "empty" else {"invalid_ohlcv": 1})
    assert first["ohlcv_retry_cooldown_size"] == 1
    assert second["ohlcv_empty_responses"] == 0
    assert calls.count(empty_mint) == 1
    assert calls.count(trading_mint) == 2


@pytest.mark.parametrize("fail_second_batch", [False, True])
def test_market_eligible_candidate_outside_first_thirty_receives_liquidity_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fail_second_batch: bool
) -> None:
    target = str(Pubkey.new_unique())
    safe_others = [str(Pubkey.new_unique()) for _ in range(29)]
    eligible = {target, *safe_others[:14]}
    cohort = [str(Pubkey.new_unique()) for _ in range(20)]
    pools = {mint: str(Pubkey.new_unique()) for mint in [target, *safe_others, *cohort]}
    requested: list[str] = []
    batches: list[list[str]] = []

    class FakeDex:
        def __enter__(self) -> FakeDex:
            return self

        def __exit__(self, *_: object) -> None:
            return None

        def tokens(self, mints: list[str]) -> dict[str, TokenMarket]:
            batches.append(mints)
            requested.extend(mints)
            if fail_second_batch and len(batches) == 2:
                raise MarketDataError("provider unavailable")
            if target not in mints:
                return {}
            return {
                target: TokenMarket(
                    target,
                    None,
                    None,
                    None,
                    pools[target],
                    "1",
                    "50000",
                    "100000",
                    "5",
                    1_700_000_000_000,
                )
            }

    class FakeOhlcv:
        def __enter__(self) -> FakeOhlcv:
            return self

        def __exit__(self, *_: object) -> None:
            return None

        def candles(self, _mint: str, _pool: str, _frame: str) -> list[PoolCandle]:
            return []

    monkeypatch.setattr("memecoin_bot.strategy_lab.DexScreenerClient", FakeDex)
    monkeypatch.setattr("memecoin_bot.strategy_lab.GeckoTerminalOhlcvClient", FakeOhlcv)
    with TradeJournal(tmp_path / "priority-snapshot.sqlite3") as journal:
        for mint in [target, *safe_others]:
            journal.upsert_discovery_candidate(
                mint=mint,
                source_program="test",
                audit_status="safe",
                audit_summary=None,
                top10_concentration_bps=None,
                decimals=6,
                market={"pair_address": pools[mint]},
            )
        for mint in cohort:
            journal.upsert_discovery_candidate(
                mint=mint,
                source_program="test",
                audit_status="blocked",
                audit_summary=None,
                top10_concentration_bps=None,
                decimals=6,
                market={"pair_address": pools[mint]},
            )
        safe_order = [
            str(item["mint"])
            for item in journal.discovery_candidates(limit=100)
            if item.get("audit_status") == "safe"
        ]
        assert safe_order.index(target) >= 29
        journal.set_runtime_state(
            RESEARCH_COHORT_KEY,
            {"selected_at": int(time.time()), "mints": cohort},
        )
        result = collect_market_snapshots(
            journal,
            limit=20,
            active_candidate_priority=lambda item: item["mint"] in eligible,
        )
        snapshots = journal.market_snapshots(target)

    assert len(requested) == 50
    assert [len(batch) for batch in batches] == [30, 20]
    assert target in requested
    assert set(cohort).issubset(requested)
    assert result["priority_safe_tracked"] == 15
    assert result["research_cohort_tracked"] == 20
    assert result["dex_requests_attempted"] == 2
    assert result["dex_requests_succeeded"] == (1 if fail_second_batch else 2)
    assert snapshots[-1]["liquidity_usd"] == "50000"
    assert snapshots[-1]["pool"] == pools[target]
    assert snapshots[-1]["price_change_24h"] == "5"
    assert snapshots[-1]["pair_created_at"] == 1_700_000_000_000


def test_collection_prioritizes_market_ready_paper_candidate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ready_mint = str(Pubkey.new_unique())
    other_mints = [str(Pubkey.new_unique()) for _ in range(4)]
    pools = {mint: str(Pubkey.new_unique()) for mint in [ready_mint, *other_mints]}
    calls: list[str] = []

    class FakeDex:
        def __enter__(self) -> FakeDex:
            return self

        def __exit__(self, *_: object) -> None:
            return None

        def tokens(self, _mints: list[str]) -> dict[str, TokenMarket]:
            return {}

    class FakeOhlcv:
        def __enter__(self) -> FakeOhlcv:
            return self

        def __exit__(self, *_: object) -> None:
            return None

        def candles(self, mint: str, pool: str, frame: str) -> list[PoolCandle]:
            calls.append(mint)
            return [_recent_pool_candle(mint, pool, frame)]

    monkeypatch.setattr("memecoin_bot.strategy_lab.DexScreenerClient", FakeDex)
    monkeypatch.setattr("memecoin_bot.strategy_lab.GeckoTerminalOhlcvClient", FakeOhlcv)
    with TradeJournal(tmp_path / "priority.sqlite3") as journal:
        # The market-ready mint is older than the four safe mints that fail the
        # market-only liquidity gate, so recency alone would omit it.
        for mint in [ready_mint, *other_mints]:
            journal.upsert_discovery_candidate(
                mint=mint,
                source_program="test",
                audit_status="safe",
                audit_summary=None,
                top10_concentration_bps=None,
                decimals=6,
                market={
                    "pair_address": pools[mint],
                    "price_usd": "1",
                    "liquidity_usd": "50000" if mint == ready_mint else "100",
                    "volume_24h_usd": "20000",
                },
            )
        result = collect_market_snapshots(
            journal,
            limit=1,
            active_candidate_priority=lambda candidate: (
                float(candidate.get("liquidity_usd") or 0) >= 25_000
            ),
        )
        assert result["research_cohort_size"] == 1
        assert result["active_market_ready_size"] == 1
        assert result["active_watchlist_size"] == 4
        assert len(calls) == 4
        assert ready_mint in calls


def test_collection_replaces_revision_blocked_focus_without_changing_cohort(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ready = [str(Pubkey.new_unique()) for _ in range(3)]
    research = [str(Pubkey.new_unique()) for _ in range(20)]
    pools = {mint: str(Pubkey.new_unique()) for mint in [*ready, *research]}
    now = int(time.time())

    class FakeDex:
        def __enter__(self) -> FakeDex:
            return self

        def __exit__(self, *_: object) -> None:
            return None

        def tokens(self, requested: list[str]) -> dict[str, TokenMarket]:
            return {
                mint: TokenMarket(
                    mint,
                    None,
                    None,
                    None,
                    pools[mint],
                    "1",
                    "50000",
                    "100000",
                    "1",
                    now * 1000,
                    volume_5m_usd="100",
                    txns_5m_buys=1,
                )
                for mint in requested
            }

    class FakeOhlcv:
        def __enter__(self) -> FakeOhlcv:
            return self

        def __exit__(self, *_: object) -> None:
            return None

        def candles(self, _mint: str, _pool: str, _frame: str) -> list[PoolCandle]:
            return []

    monkeypatch.setattr("memecoin_bot.strategy_lab.DexScreenerClient", FakeDex)
    monkeypatch.setattr("memecoin_bot.strategy_lab.GeckoTerminalOhlcvClient", FakeOhlcv)
    with TradeJournal(tmp_path / "revision-focus.sqlite3") as journal:
        for mint in [*ready, *research]:
            journal.upsert_discovery_candidate(
                mint=mint,
                source_program="test",
                audit_status="safe" if mint in ready else "blocked",
                audit_summary=None,
                top10_concentration_bps=None,
                decimals=6,
                market={"pair_address": pools[mint]},
            )
        journal.set_runtime_state(RESEARCH_COHORT_KEY, {"selected_at": now, "mints": research})
        journal.set_runtime_state(
            "ohlcv_active_focus",
            {
                "selected_at": now,
                "mints": ready[:2],
                "last_selected_at": {mint: now for mint in ready[:2]},
            },
        )
        opened_at = (now // 60 - 1) * 60
        journal.record_pool_candles(
            [
                PoolCandle(
                    ready[0],
                    pools[ready[0]],
                    "1m",
                    opened_at - index * 60,
                    "1",
                    "1",
                    "1",
                    "1",
                    "100",
                )
                for index in range(8)
            ],
            fetched_at=now - 20,
        )
        journal.record_pool_candles(
            [PoolCandle(ready[0], pools[ready[0]], "1m", opened_at, "1", "1", "1", "1.1", "100")],
            fetched_at=now - 10,
        )
        older_hour = (now // 3600 - 1) * 3600
        journal.record_pool_candles(
            [PoolCandle(ready[1], pools[ready[1]], "1h", older_hour, "1", "1", "1", "1", "100")],
            fetched_at=now - 20,
        )
        journal.record_pool_candles(
            [PoolCandle(ready[1], pools[ready[1]], "1h", older_hour, "1", "1", "1", "1.1", "100")],
            fetched_at=now - 10,
        )
        assert journal.pool_has_revision_since(
            ready[1], pools[ready[1]], opened_since=older_hour, observed_by=now
        )
        assert journal.pool_has_revision_since(
            ready[0], pools[ready[0]], opened_since=opened_at, observed_by=now
        )
        assert not journal.pool_has_revision_since(
            ready[0], pools[ready[0]], opened_since=opened_at + 60, observed_by=now
        )
        assert not journal.pool_has_revision_since(
            ready[0], pools[ready[0]], opened_since=opened_at, observed_by=now - 11
        )
        result = collect_market_snapshots(
            journal, active_candidate_priority=lambda candidate: candidate["mint"] in ready
        )
        focus = journal.runtime_state("ohlcv_active_focus") or {}
        assert set(focus["mints"]) == set(ready[1:])
        assert focus["revision_excluded"] == [ready[0]]
        assert result["strategy_focus_revision_excluded"] == 1
        assert research_cohort_mints(journal) == research


def test_collection_pins_market_ready_candidates_then_rotates_after_hour(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ready = [str(Pubkey.new_unique()) for _ in range(6)]
    cohort = [str(Pubkey.new_unique()) for _ in range(20)]
    pools = {mint: str(Pubkey.new_unique()) for mint in [*ready, *cohort]}
    calls: list[str] = []
    deep_calls: list[str] = []

    class FakeDex:
        def __enter__(self) -> FakeDex:
            return self

        def __exit__(self, *_: object) -> None:
            return None

        def tokens(self, _mints: list[str]) -> dict[str, TokenMarket]:
            return {}

    class FakeOhlcv:
        def __enter__(self) -> FakeOhlcv:
            return self

        def __exit__(self, *_: object) -> None:
            return None

        def candles(self, mint: str, pool: str, frame: str) -> list[PoolCandle]:
            calls.append(mint)
            if frame in ("15m", "1h"):
                deep_calls.append(mint)
            return [_recent_pool_candle(mint, pool, frame)]

    monkeypatch.setattr("memecoin_bot.strategy_lab.DexScreenerClient", FakeDex)
    monkeypatch.setattr("memecoin_bot.strategy_lab.GeckoTerminalOhlcvClient", FakeOhlcv)
    with TradeJournal(tmp_path / "rotate-ready.sqlite3") as journal:
        for mint in [*ready, *cohort]:
            journal.upsert_discovery_candidate(
                mint=mint,
                source_program="test",
                audit_status="safe" if mint in ready else "blocked",
                audit_summary=None,
                top10_concentration_bps=None,
                decimals=6,
                market={"pair_address": pools[mint]},
            )
        journal.set_runtime_state(
            RESEARCH_COHORT_KEY,
            {"selected_at": int(time.time()), "mints": cohort},
        )
        pinned: set[str] | None = None
        for _ in range(3):
            before = len(calls)
            result = collect_market_snapshots(
                journal,
                active_candidate_priority=lambda item: item["mint"] in ready,
            )
            assert result["active_market_ready_size"] == len(ready)
            assert result["strategy_focus_size"] == 2
            assert result["strategy_focus_scheduled"] == 2
            focus = journal.runtime_state("ohlcv_active_focus") or {}
            current = set(focus["mints"])
            assert current.issubset(calls[before:])
            if pinned is None:
                pinned = current
            else:
                assert current == pinned

        assert pinned is not None
        original_focus_state = journal.runtime_state("ohlcv_active_focus") or {}
        survivor, unavailable = sorted(pinned)
        journal.set_runtime_state(
            "ohlcv_retry_cooldown",
            {"entries": {f"{unavailable}:{pools[unavailable]}:1m": int(time.time()) + 300}},
        )
        collect_market_snapshots(
            journal, active_candidate_priority=lambda item: item["mint"] in ready
        )
        replaced_focus_state = journal.runtime_state("ohlcv_active_focus") or {}
        assert survivor in replaced_focus_state["mints"]
        assert unavailable not in replaced_focus_state["mints"]
        assert len(deep_calls) == 2
        assert set(deep_calls).issubset(replaced_focus_state["mints"])
        assert (
            replaced_focus_state["last_selected_at"][survivor]
            == original_focus_state["last_selected_at"][survivor]
        )
        journal.set_runtime_state("ohlcv_retry_cooldown", {"entries": {}})

        selected_at = int(time.time())
        history = {mint: selected_at - 3_601 for mint in pinned}
        journal.set_runtime_state(
            "ohlcv_active_focus",
            {
                "mints": sorted(pinned),
                "selected_at": selected_at - 3_601,
                "last_selected_at": history,
            },
        )
        collect_market_snapshots(
            journal, active_candidate_priority=lambda item: item["mint"] in ready
        )
        next_focus = set((journal.runtime_state("ohlcv_active_focus") or {})["mints"])
        assert len(next_focus) == 2
        assert not next_focus & pinned

        history.update({mint: int(time.time()) - 3_601 for mint in next_focus})
        journal.set_runtime_state(
            "ohlcv_active_focus",
            {
                "mints": sorted(next_focus),
                "selected_at": int(time.time()) - 3_601,
                "last_selected_at": history,
            },
        )
        collect_market_snapshots(
            journal, active_candidate_priority=lambda item: item["mint"] in ready
        )
        final_focus = set((journal.runtime_state("ohlcv_active_focus") or {})["mints"])
        assert len(final_focus) == 2
        assert not final_focus & (pinned | next_focus)

    assert set(ready).issubset(calls)


def test_collection_retires_pins_that_cannot_fill_trade_backed_minutes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ready = [str(Pubkey.new_unique()) for _ in range(4)]
    cohort = [str(Pubkey.new_unique()) for _ in range(20)]
    pools = {mint: str(Pubkey.new_unique()) for mint in [*ready, *cohort]}
    activity = {ready[0]: 2, ready[1]: 3, ready[2]: 45, ready[3]: 35}

    class FakeDex:
        def __enter__(self) -> FakeDex:
            return self

        def __exit__(self, *_: object) -> None:
            return None

        def tokens(self, requested: list[str]) -> dict[str, TokenMarket]:
            return {
                mint: TokenMarket(
                    mint=mint,
                    name=None,
                    symbol=None,
                    dex_id="raydium",
                    pair_address=pools[mint],
                    price_usd="1",
                    liquidity_usd="50000",
                    volume_24h_usd="100000",
                    price_change_24h="1",
                    pair_created_at=None,
                    volume_5m_usd="1000",
                    txns_5m_buys=activity[mint],
                )
                for mint in requested
                if mint in activity
            }

    class FakeOhlcv:
        def __enter__(self) -> FakeOhlcv:
            return self

        def __exit__(self, *_: object) -> None:
            return None

        def candles(self, mint: str, pool: str, frame: str) -> list[PoolCandle]:
            return [_recent_pool_candle(mint, pool, frame)]

    monkeypatch.setattr("memecoin_bot.strategy_lab.DexScreenerClient", FakeDex)
    monkeypatch.setattr("memecoin_bot.strategy_lab.GeckoTerminalOhlcvClient", FakeOhlcv)
    with TradeJournal(tmp_path / "quiet-pins.sqlite3") as journal:
        for mint in [*ready, *cohort]:
            journal.upsert_discovery_candidate(
                mint=mint,
                source_program="test",
                audit_status="safe" if mint in ready else "blocked",
                audit_summary=None,
                top10_concentration_bps=None,
                decimals=6,
                market={"pair_address": pools[mint]},
            )
        journal.set_runtime_state(
            RESEARCH_COHORT_KEY, {"selected_at": int(time.time()), "mints": cohort}
        )
        now = int(time.time())
        journal.set_runtime_state(
            "ohlcv_active_focus",
            {
                "selected_at": now,
                "mints": ready[:2],
                "last_selected_at": {mint: now for mint in ready[:2]},
            },
        )
        for cycle in range(3):
            result = collect_market_snapshots(
                journal, active_candidate_priority=lambda candidate: candidate["mint"] in ready
            )
            focus = journal.runtime_state("ohlcv_active_focus") or {}
            assert set(focus["mints"]).issubset(set(result["ohlcv_refreshed_mints"]))
            if cycle < 2:
                assert focus["mints"] == ready[:2]
                assert result["strategy_focus_low_activity_excluded"] == 0
            else:
                assert set(focus["mints"]) == set(ready[2:])
                assert result["strategy_focus_low_activity_excluded"] == 2
        assert research_cohort_mints(journal) == cohort


def test_new_candle_focus_requires_time_left_on_passing_audit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ready = [str(Pubkey.new_unique()) for _ in range(3)]
    pools = {mint: str(Pubkey.new_unique()) for mint in ready}

    class FakeDex:
        def __enter__(self) -> FakeDex:
            return self

        def __exit__(self, *_: object) -> None:
            return None

        def tokens(self, requested: list[str]) -> dict[str, TokenMarket]:
            return {
                mint: TokenMarket(
                    mint=mint,
                    name=None,
                    symbol=None,
                    dex_id="raydium",
                    pair_address=pools[mint],
                    price_usd="1",
                    liquidity_usd="50000",
                    volume_24h_usd="100000",
                    price_change_24h="1",
                    pair_created_at=None,
                    volume_5m_usd="1000",
                    txns_5m_buys=100 if mint == ready[0] else 20,
                )
                for mint in requested
                if mint in pools
            }

    class FakeOhlcv:
        def __enter__(self) -> FakeOhlcv:
            return self

        def __exit__(self, *_: object) -> None:
            return None

        def candles(self, mint: str, pool: str, frame: str) -> list[PoolCandle]:
            return [_recent_pool_candle(mint, pool, frame)]

    monkeypatch.setattr("memecoin_bot.strategy_lab.DexScreenerClient", FakeDex)
    monkeypatch.setattr("memecoin_bot.strategy_lab.GeckoTerminalOhlcvClient", FakeOhlcv)
    with TradeJournal(tmp_path / "focus-audit-age.sqlite3") as journal:
        for mint in ready:
            journal.upsert_discovery_candidate(
                mint=mint,
                source_program="test",
                audit_status="safe",
                audit_summary=None,
                top10_concentration_bps=None,
                decimals=6,
                market={"pair_address": pools[mint]},
            )
        journal._connection.execute(
            "UPDATE discovery_candidates SET last_seen=? WHERE mint=?",
            (datetime.fromtimestamp(time.time() - 900, UTC).isoformat(), ready[0]),
        )
        result = collect_market_snapshots(
            journal, active_candidate_priority=lambda candidate: candidate["mint"] in ready
        )
        focus = journal.runtime_state("ohlcv_active_focus") or {}
        assert set(focus["mints"]) == set(ready[1:])
        assert result["strategy_focus_stale_audit_excluded"] == 1


def test_collection_defers_explicitly_inactive_pools_without_dropping_cohort(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    inactive, active, lagging = [str(Pubkey.new_unique()) for _ in range(3)]
    cohort = [inactive, active, lagging]
    pools = {mint: str(Pubkey.new_unique()) for mint in cohort}
    calls: list[str] = []
    reactivated = False

    class FakeDex:
        def __enter__(self) -> FakeDex:
            return self

        def __exit__(self, *_: object) -> None:
            return None

        def tokens(self, mints: list[str]) -> dict[str, TokenMarket]:
            return {
                mint: TokenMarket(
                    mint,
                    None,
                    None,
                    None,
                    pools[mint],
                    "1",
                    "50000",
                    "100000",
                    "5",
                    1_700_000_000_000,
                    volume_5m_usd="100"
                    if mint == active or (mint == inactive and reactivated)
                    else "0",
                    txns_5m_buys=1 if mint == active or (mint == inactive and reactivated) else 0,
                )
                for mint in mints
            }

    class FakeOhlcv:
        def __enter__(self) -> FakeOhlcv:
            return self

        def __exit__(self, *_: object) -> None:
            return None

        def candles(self, mint: str, pool: str, frame: str) -> list[PoolCandle]:
            calls.append(mint)
            return [_recent_pool_candle(mint, pool, frame)]

    monkeypatch.setattr("memecoin_bot.strategy_lab.DexScreenerClient", FakeDex)
    monkeypatch.setattr("memecoin_bot.strategy_lab.GeckoTerminalOhlcvClient", FakeOhlcv)
    with TradeJournal(tmp_path / "inactive-research.sqlite3") as journal:
        for mint in cohort:
            journal.upsert_discovery_candidate(
                mint=mint,
                source_program="test",
                audit_status="blocked",
                audit_summary=None,
                top10_concentration_bps=None,
                decimals=6,
                market={"pair_address": pools[mint]},
            )
        journal.set_runtime_state(
            RESEARCH_COHORT_KEY,
            {"selected_at": int(time.time()), "mints": cohort, "focus_size": RESEARCH_FOCUS_SIZE},
        )
        journal.record_pool_candles(
            [_recent_pool_candle(lagging, pools[lagging], "1m")], fetched_at=int(time.time())
        )
        first = collect_market_snapshots(journal)
        assert inactive not in calls
        assert {active, lagging}.issubset(calls)
        assert first["inactive_5m_mints_deferred"] == 1
        assert first["research_cohort_size"] == 3
        assert research_cohort_mints(journal) == cohort

        reactivated = True
        calls.clear()
        second = collect_market_snapshots(journal)
        assert inactive in calls
        assert second["inactive_5m_mints_deferred"] == 0


def test_active_secondary_research_keeps_a_slot_when_focus_is_inactive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cohort = [str(Pubkey.new_unique()) for _ in range(20)]
    safe = [str(Pubkey.new_unique()) for _ in range(4)]
    active_secondary = cohort[5]
    pools = {mint: str(Pubkey.new_unique()) for mint in [*cohort, *safe]}
    calls: list[str] = []

    class FakeDex:
        def __enter__(self) -> FakeDex:
            return self

        def __exit__(self, *_: object) -> None:
            return None

        def tokens(self, requested: list[str]) -> dict[str, TokenMarket]:
            return {
                mint: TokenMarket(
                    mint,
                    None,
                    None,
                    None,
                    pools[mint],
                    "1",
                    "50000",
                    "100000",
                    "5",
                    int(time.time() * 1000),
                    volume_5m_usd="0" if mint in cohort[:2] else "100",
                    txns_5m_buys=0 if mint in cohort[:2] else 1,
                )
                for mint in requested
                if mint in {*cohort[:2], active_secondary, *safe}
            }

    class FakeOhlcv:
        def __enter__(self) -> FakeOhlcv:
            return self

        def __exit__(self, *_: object) -> None:
            return None

        def candles(self, mint: str, _pool: str, _frame: str) -> list[PoolCandle]:
            calls.append(mint)
            return []

    monkeypatch.setattr("memecoin_bot.strategy_lab.DexScreenerClient", FakeDex)
    monkeypatch.setattr("memecoin_bot.strategy_lab.GeckoTerminalOhlcvClient", FakeOhlcv)
    with TradeJournal(tmp_path / "inactive-focus.sqlite3") as journal:
        for mint in cohort:
            journal.upsert_discovery_candidate(
                mint=mint,
                source_program="test",
                audit_status="blocked",
                audit_summary=None,
                top10_concentration_bps=None,
                decimals=6,
                market={"pair_address": pools[mint]},
            )
        for mint in safe:
            journal.upsert_discovery_candidate(
                mint=mint,
                source_program="test",
                audit_status="safe",
                audit_summary=None,
                top10_concentration_bps=None,
                decimals=6,
                market={"pair_address": pools[mint]},
            )
        journal.set_runtime_state(
            RESEARCH_COHORT_KEY,
            {"selected_at": int(time.time()), "mints": cohort, "focus_size": 2},
        )
        result = collect_market_snapshots(journal)
        assert research_cohort_mints(journal) == cohort

    assert result["inactive_5m_mints_deferred"] == 2
    assert result["research_active_5m_eligible"] == 1
    assert result["research_active_5m_scheduled"] == 1
    assert active_secondary in calls[:2]


def test_collection_budget_ramps_only_for_sustained_ready_backlog_and_reverts_on_quota(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The measured four-call baseline may cautiously try six after clean cycles.
    ready = [str(Pubkey.new_unique()) for _ in range(10)]
    cohort = [str(Pubkey.new_unique()) for _ in range(20)]
    pools = {mint: str(Pubkey.new_unique()) for mint in [*ready, *cohort]}
    calls: list[tuple[str, str]] = []
    paces: list[float] = []
    limit_next = False
    empty_next = False
    stale_next = False
    cache_next = False

    class FakeDex:
        def __enter__(self) -> FakeDex:
            return self

        def __exit__(self, *_: object) -> None:
            return None

        def tokens(self, _mints: list[str]) -> dict[str, TokenMarket]:
            return {}

    class FakeOhlcv:
        def __enter__(self) -> FakeOhlcv:
            return self

        def __exit__(self, *_: object) -> None:
            return None

        def candles(self, mint: str, pool: str, frame: str) -> list[PoolCandle]:
            nonlocal limit_next, empty_next, stale_next, cache_next
            calls.append((mint, frame))
            paces.append(self.request_interval_seconds)
            self.last_call_cache_hit = cache_next
            cache_next = False
            if limit_next:
                limit_next = False
                raise OhlcvRateLimited("quota", retry_after_seconds=60)
            if empty_next:
                empty_next = False
                return []
            if stale_next:
                stale_next = False
                period = TIMEFRAME_SECONDS[frame]
                return [
                    replace(
                        _recent_pool_candle(mint, pool, frame),
                        opened_at=(int(time.time()) // period - 200) * period,
                    )
                ]
            return [_recent_pool_candle(mint, pool, frame)]

    monkeypatch.setattr("memecoin_bot.strategy_lab.DexScreenerClient", FakeDex)
    monkeypatch.setattr("memecoin_bot.strategy_lab.GeckoTerminalOhlcvClient", FakeOhlcv)
    with TradeJournal(tmp_path / "adaptive-budget.sqlite3") as journal:
        for mint in [*ready, *cohort]:
            journal.upsert_discovery_candidate(
                mint=mint,
                source_program="test",
                audit_status="safe" if mint in ready else "blocked",
                audit_summary=None,
                top10_concentration_bps=None,
                decimals=6,
                market={"pair_address": pools[mint]},
            )
        journal.set_runtime_state(
            RESEARCH_COHORT_KEY,
            {"selected_at": int(time.time()), "mints": cohort},
        )
        for cycle_number in range(3):
            before = len(calls)
            result = collect_market_snapshots(
                journal,
                active_candidate_priority=lambda item: item["mint"] in ready,
            )
            assert len(calls) - before == 4
            assert result["ohlcv_request_budget_next"] == (6 if cycle_number == 2 else 4)
            assert result["ohlcv_requests_succeeded"] == 4
            assert set(paces[before:]) == {15.0}

        before = len(calls)
        expanded = collect_market_snapshots(
            journal,
            active_candidate_priority=lambda item: item["mint"] in ready,
        )
        assert expanded["collection_lane"] == "active_deep"
        assert len(calls) - before == 6
        assert expanded["ohlcv_requests_succeeded"] == 6
        assert expanded["ohlcv_request_budget_next"] == 6
        assert set(paces[before:]) == {10.0}
        assert any(mint in cohort for mint, _ in calls[before:])

        journal.set_runtime_state("ohlcv_request_budget", {"limit": 8})
        before = len(calls)
        at_ceiling = collect_market_snapshots(
            journal,
            active_candidate_priority=lambda item: item["mint"] in ready,
        )
        assert len(calls) - before == 6
        assert at_ceiling["ohlcv_request_budget_next"] == 6
        assert len({mint for mint, _ in calls[before:] if mint in cohort}) >= 2

        limit_next = True
        limited = collect_market_snapshots(
            journal,
            active_candidate_priority=lambda item: item["mint"] in ready,
        )
        assert limited["ohlcv_rate_limited"] is True
        assert limited["ohlcv_error_codes"] == {"rate_limit": 1}
        assert limited["ohlcv_request_budget_next"] == 4
        budget_after_limit = journal.runtime_state("ohlcv_request_budget") or {}
        assert budget_after_limit["limit"] == 4
        assert int(budget_after_limit["higher_budget_retry_after"]) >= int(time.time()) + 86_390

        journal.set_runtime_state("ohlcv_backoff", {"until": 0})
        # A restart can load a cooldown written by an older hourly policy.
        # The recent quota event must still prevent a fresh six-call trial.
        journal.set_runtime_state(
            "ohlcv_request_budget",
            {
                **budget_after_limit,
                "limit": 6,
                "higher_budget_retry_after": int(time.time()) - 1,
            },
        )
        held = collect_market_snapshots(
            journal,
            active_candidate_priority=lambda item: item["mint"] in ready,
        )
        assert held["ohlcv_requests_attempted"] <= 4
        assert held["ohlcv_request_budget_next"] == 4
        assert held["ohlcv_higher_budget_retry_after"] >= int(time.time()) + 86_390

        journal.set_runtime_state("ohlcv_request_budget", {"limit": 8})
        before = len(calls)
        no_backlog = collect_market_snapshots(
            journal,
            active_candidate_priority=lambda _item: False,
        )
        assert len(calls) - before <= 4
        assert no_backlog["active_market_ready_size"] == 0
        assert no_backlog["ohlcv_request_budget_next"] == 4

        journal.set_runtime_state(
            "ohlcv_request_budget",
            {
                "limit": 4,
                "fresh_network_successes": 8,
                "fresh_window_started_at": int(time.time()),
            },
        )
        empty_next = True
        empty = collect_market_snapshots(
            journal,
            active_candidate_priority=lambda item: item["mint"] in ready,
        )
        assert empty["ohlcv_empty_responses"] == 1
        assert empty["ohlcv_request_budget_next"] == 4
        assert (journal.runtime_state("ohlcv_request_budget") or {})["fresh_network_successes"] == 0

        journal.set_runtime_state(
            "ohlcv_request_budget",
            {
                "limit": 4,
                "fresh_network_successes": 8,
                "fresh_window_started_at": int(time.time()),
            },
        )
        cache_next = True
        cached = collect_market_snapshots(
            journal,
            active_candidate_priority=lambda item: item["mint"] in ready,
        )
        assert cached["ohlcv_cache_hits"] == 1
        assert cached["ohlcv_request_budget_next"] == 4
        assert (journal.runtime_state("ohlcv_request_budget") or {})[
            "fresh_network_successes"
        ] == 11
        next_fresh = collect_market_snapshots(
            journal,
            active_candidate_priority=lambda item: item["mint"] in ready,
        )
        assert next_fresh["ohlcv_cache_hits"] == 0
        assert next_fresh["ohlcv_request_budget_next"] == 6

        journal.set_runtime_state(
            "ohlcv_request_budget",
            {
                "limit": 4,
                "fresh_network_successes": 8,
                "fresh_window_started_at": int(time.time()),
            },
        )
        stale_next = True
        stale = collect_market_snapshots(
            journal,
            active_candidate_priority=lambda item: item["mint"] in ready,
        )
        assert stale["ohlcv_stale_responses"] == 1
        assert stale["ohlcv_request_budget_next"] == 4
        assert (journal.runtime_state("ohlcv_request_budget") or {})["fresh_network_successes"] == 0

        journal.set_runtime_state(
            "ohlcv_request_budget",
            {
                "limit": 4,
                "fresh_network_successes": 11,
                "fresh_window_started_at": int(time.time()) - 601,
            },
        )
        expired = collect_market_snapshots(
            journal,
            active_candidate_priority=lambda item: item["mint"] in ready,
        )
        assert expired["ohlcv_request_budget_next"] == 4
        assert (journal.runtime_state("ohlcv_request_budget") or {})["fresh_network_successes"] <= 4

        # An old eight-call persisted value cannot exceed the six-call trial.
        journal.set_runtime_state("ohlcv_request_budget", {"limit": 8})
        paced = collect_market_snapshots(
            journal,
            active_candidate_priority=lambda item: item["mint"] in ready,
        )
        assert paced["ohlcv_requests_attempted"] <= 6
        assert paced["ohlcv_request_budget_next"] == 6
