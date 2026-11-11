from __future__ import annotations

import json
import time
from dataclasses import replace
from pathlib import Path
from typing import Any, cast

import pytest
from solders.pubkey import Pubkey

from memecoin_bot.config import Settings
from memecoin_bot.journal import TradeJournal
from memecoin_bot.ohlcv import PoolCandle
from memecoin_bot.operations import summarize_ohlcv_collection_capacity
from memecoin_bot.strategy import StrategyDecision, evaluate_candidate
from memecoin_bot.strategy_lab import (
    DECISION_FOLLOWUP_METHOD,
    RESEARCH_SELECTION_METHOD,
    Candle,
    Timeframe,
    analyze_entry,
)
from memecoin_bot.verification import (
    report,
    summarize_candle_revisions,
    summarize_collector_signal_outcomes,
    summarize_current_entry_readiness,
    summarize_current_research_capture,
    summarize_current_research_lanes,
    summarize_decision_cycle_integrity,
    summarize_market_eligible_followup_replay,
    summarize_recent_decisions,
    summarize_saved_market_gate_reproducibility,
    verify_paper_entry_market_gate,
    verify_paper_entry_signal,
)


def test_collector_rejection_outcomes_keep_missing_paths_in_denominator() -> None:
    paths: dict[str, object] = {
        "horizons": {
            "5": {
                "rows": [
                    {
                        "verdict": "observe",
                        "final_reason": "1m unavailable",
                        "outcome": {
                            "status": "observed_timely",
                            "horizon_close_pct": 12.0,
                            "max_favorable_pct": 20.0,
                            "max_adverse_pct": -7.0,
                        },
                    },
                    {
                        "verdict": "observe",
                        "final_reason": "1m unavailable",
                        "outcome": {"status": "future_bar_gap"},
                    },
                    {
                        "verdict": "observe",
                        "final_reason": "1m unavailable",
                        "outcome": {"status": "observed_backfilled"},
                    },
                    {
                        "verdict": "reject",
                        "final_reason": "audit stale",
                        "outcome": {
                            "status": "observed_timely",
                            "horizon_close_pct": -5.0,
                            "max_favorable_pct": 2.0,
                            "max_adverse_pct": -10.0,
                        },
                    },
                ]
            },
            "15": {"rows": []},
        }
    }
    summary = summarize_collector_signal_outcomes(paths)
    five = cast(dict[str, Any], summary["horizons"])["5"]
    assert five["sample_size"] == 4
    observed = five["reason_groups"][0]
    assert observed["observations"] == 3
    assert observed["outcome_statuses"] == {
        "observed_timely": 1,
        "future_bar_gap": 1,
        "observed_backfilled": 1,
    }
    assert observed["timely_outcomes"] == 1
    assert observed["median_horizon_close_pct"] == 12.0
    assert observed["interpretation_status"] == "insufficient_timely_sample"
    assert summary["performance_inference"] == "none_without_executable_costed_forward_trades"


def test_collector_rejection_summary_requires_distinct_tokens() -> None:
    outcome = {
        "status": "observed_timely",
        "horizon_close_pct": 3.0,
        "max_favorable_pct": 8.0,
        "max_adverse_pct": -4.0,
    }

    def summarize(mints: list[str]) -> dict[str, object]:
        return summarize_collector_signal_outcomes(
            {
                "horizons": {
                    "5": {
                        "rows": [
                            {
                                "mint": mint,
                                "verdict": "observe",
                                "final_reason": "1m unavailable",
                                "outcome": outcome,
                            }
                            for mint in mints
                        ]
                    }
                }
            }
        )

    repeated = cast(dict[str, Any], summarize(["same-mint"] * 30)["horizons"])["5"][
        "reason_groups"
    ][0]
    assert repeated["timely_outcomes"] == 30
    assert repeated["timely_distinct_mints"] == 1
    assert repeated["interpretation_status"] == "insufficient_timely_sample"
    independent = cast(
        dict[str, Any], summarize([f"mint-{index}" for index in range(30)])["horizons"]
    )["5"]["reason_groups"][0]
    assert independent["timely_distinct_mints"] == 30
    assert independent["interpretation_status"] == "descriptive_only"


def test_candle_revision_summary_counts_only_observed_corrections(tmp_path: Path) -> None:
    mint, pool = str(Pubkey.new_unique()), str(Pubkey.new_unique())
    with TradeJournal(tmp_path / "revision-summary.sqlite3") as journal:
        assert summarize_candle_revisions(journal)["revised_candles"] == 0
        journal.record_pool_candles(
            [
                PoolCandle(mint, pool, "1m", 600, "1", "1", "1", "1", "100"),
                PoolCandle(mint, pool, "1m", 660, "1", "1", "1", "1", "100"),
            ],
            fetched_at=730,
        )
        journal.record_pool_candles(
            [PoolCandle(mint, pool, "1m", 600, "1", "1", "1", "1.01", "100")],
            fetched_at=850,
        )
        summary = summarize_candle_revisions(journal)
    assert summary["revised_candles"] == 1
    assert summary["affected_pools"] == 1
    assert summary["by_timeframe"] == {"1m": 1}
    assert summary["median_seconds_after_first_observation"] == 120
    assert summary["median_seconds_after_close"] == 190
    assert summary["revised_candles_with_measured_magnitude"] == 1
    assert summary["measured_price_changes"] == 1
    assert summary["median_max_price_revision_bps"] == pytest.approx(100)
    assert summary["largest_price_revision_bps"] == pytest.approx(100)
    assert summary["measured_volume_changes"] == 0
    assert "not an error rate" in summary["coverage_note"]


def test_followup_replay_does_not_finalize_an_open_selection_window(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = Settings(
        rpc_url="https://rpc.example.test",
        keypair_path=None,
        jupiter_api_key=None,
        live_trading_enabled=False,
        max_trade_lamports=100_000_000,
        max_daily_lamports=200_000_000,
        max_slippage_bps=300,
        max_price_impact_bps=500,
        max_top10_concentration_bps=7_000,
        max_priority_fee_lamports=1_000_000,
        confirmation_timeout_seconds=45,
        allow_token_2022=False,
        state_path=tmp_path / "followup-window.sqlite3",
    )
    now = int(time.time())
    mint, pool = str(Pubkey.new_unique()), str(Pubkey.new_unique())
    with TradeJournal(settings.state_path) as journal:
        journal.record_decision_followup_selection(
            mint,
            selected_at=now - 300,
            expires_at=now + 300,
            population_count=1,
            method=DECISION_FOLLOWUP_METHOD,
        )
        journal.record_market_snapshot(
            mint=mint,
            pool=pool,
            price_usd="1",
            liquidity_usd="50000",
            volume_24h_usd="100000",
            observed_at=now - 310,
            source="dexscreener",
        )
        journal.record_pool_candles(
            [PoolCandle(mint, pool, "1m", (now // 60) * 60 - 60, "1", "1", "1", "1", "100")],
            fetched_at=now,
        )
        monkeypatch.setattr(
            "memecoin_bot.verification.run_backtest",
            lambda *args, **kwargs: {"status": "complete", "trades": 0},
        )
        collecting = summarize_market_eligible_followup_replay(journal, settings, as_of=now)
        expired = summarize_market_eligible_followup_replay(journal, settings, as_of=now + 301)

    assert collecting["statuses"] == {"collecting": 1}
    assert collecting["selection_windows_collecting"] == 1
    assert collecting["results"][0]["selection_window_complete"] is False
    assert expired["statuses"] == {"complete": 1}
    assert expired["selection_windows_complete"] == 1
    assert expired["results"][0]["selection_window_complete"] is True


def test_followup_replay_uses_only_pool_known_at_selection(tmp_path: Path) -> None:
    settings = Settings(
        rpc_url="https://rpc.example.test",
        keypair_path=None,
        jupiter_api_key=None,
        live_trading_enabled=False,
        max_trade_lamports=100_000_000,
        max_daily_lamports=200_000_000,
        max_slippage_bps=300,
        max_price_impact_bps=500,
        max_top10_concentration_bps=7_000,
        max_priority_fee_lamports=1_000_000,
        confirmation_timeout_seconds=45,
        allow_token_2022=False,
        state_path=tmp_path / "followup.sqlite3",
    )
    now = int(time.time())
    known_mint, future_mint = str(Pubkey.new_unique()), str(Pubkey.new_unique())
    pool = str(Pubkey.new_unique())
    with TradeJournal(settings.state_path) as journal:
        for mint in (known_mint, future_mint):
            journal.record_decision_followup_selection(
                mint,
                selected_at=now - 600,
                expires_at=now + 600,
                population_count=2,
                method=DECISION_FOLLOWUP_METHOD,
            )
        for mint, observed_at in ((known_mint, now - 630), (future_mint, now - 570)):
            journal.record_market_snapshot(
                mint=mint,
                pool=pool,
                price_usd="1",
                liquidity_usd="50000",
                volume_24h_usd="100000",
                observed_at=observed_at,
                source="dexscreener",
            )
        first_bar = (now // 60) * 60 - 12 * 60
        journal.record_pool_candles(
            [
                PoolCandle(
                    known_mint,
                    pool,
                    "1m",
                    first_bar + minute * 60,
                    "1",
                    "1",
                    "1",
                    "1",
                    "100",
                )
                for minute in range(12)
            ],
            fetched_at=now,
        )
        summary = summarize_market_eligible_followup_replay(journal, settings)

    statuses = {row["mint"]: row["status"] for row in summary["results"]}
    assert statuses == {
        known_mint: "insufficient_audit_history",
        future_mint: "missing_selection_market",
    }
    assert summary["closed_trades"] == 0
    assert summary["censored_trades"] == 0


def test_ohlcv_capacity_reports_observed_throughput_and_entry_demand(tmp_path: Path) -> None:
    now = int(time.time())
    with TradeJournal(tmp_path / "capacity.sqlite3") as journal:
        assert (
            summarize_ohlcv_collection_capacity(journal)["observed_successful_requests_per_minute"]
            is None
        )
        journal.set_runtime_state(
            "ohlcv_request_budget",
            {
                "recent_cycles": [
                    {
                        "started_at": now - 240,
                        "at": now - 220,
                        "attempted": 4,
                        "succeeded": 4,
                        "rate_limited": False,
                        "market_ready": 14,
                    },
                    {
                        "started_at": now - 210,
                        "at": now - 180,
                        "attempted": 4,
                        "succeeded": 4,
                        "cache_hits": 1,
                        "rate_limited": False,
                        "market_ready": 14,
                    },
                    {
                        "started_at": now - 150,
                        "at": now - 120,
                        "attempted": 0,
                        "succeeded": 0,
                        "cache_hits": 0,
                        "rate_limited": True,
                        "market_ready": 14,
                    },
                    {
                        "started_at": now - 90,
                        "at": now - 60,
                        "attempted": 4,
                        "succeeded": 3,
                        "cache_hits": 0,
                        "rate_limited": True,
                        "market_ready": 14,
                    },
                    {
                        "started_at": now - 30,
                        "at": now,
                        "attempted": 4,
                        "succeeded": 4,
                        "cache_hits": 0,
                        "rate_limited": False,
                        "market_ready": 14,
                    },
                    {
                        "started_at": now,
                        "at": now,
                        "attempted": 1,
                        "succeeded": 2,
                        "cache_hits": 0,
                        "rate_limited": False,
                        "market_ready": 14,
                    },
                ]
            },
        )
        result = summarize_ohlcv_collection_capacity(journal)
    assert result["cycles"] == 4
    assert result["requests_attempted"] == 12
    assert result["requests_succeeded"] == 10
    assert result["cache_hits"] == 1
    assert result["rate_limited_cycles"] == 2
    assert result["zero_request_cycles"] == 1
    assert result["window_observed_seconds"] == 210
    assert result["observed_successful_requests_per_minute"] == 2.86
    assert result["minimum_calls_per_minute_to_refresh_each_ready_mint_every_120s"] == 7


def test_current_research_capture_counts_elapsed_slots_and_selected_pool(tmp_path: Path) -> None:
    focus_mint = str(Pubkey.new_unique())
    secondary_mint = str(Pubkey.new_unique())
    pool = str(Pubkey.new_unique())
    other_pool = str(Pubkey.new_unique())
    with TradeJournal(tmp_path / "capture.sqlite3") as journal:
        for mint, market in (
            (focus_mint, {"pair_address": pool}),
            (secondary_mint, None),
        ):
            journal.upsert_discovery_candidate(
                mint=mint,
                source_program="test",
                audit_status="blocked",
                audit_summary=None,
                top10_concentration_bps=None,
                decimals=6,
                market=cast(dict[str, object] | None, market),
            )
        journal.record_research_selection(
            [focus_mint, secondary_mint],
            cohort_started_at=300,
            selected_at=300,
            expires_at=900,
            population_count=2,
            method=RESEARCH_SELECTION_METHOD,
        )
        journal.record_pool_candles(
            [PoolCandle(focus_mint, pool, "1m", 300, "1", "1", "1", "1", "100")],
            fetched_at=370,
        )
        journal.record_pool_candles(
            [PoolCandle(focus_mint, pool, "1m", 360, "1", "1", "1", "1", "100")],
            fetched_at=550,
        )
        journal.record_pool_candles(
            [PoolCandle(focus_mint, other_pool, "1m", 420, "1", "1", "1", "1", "100")],
            fetched_at=490,
        )
        result = summarize_current_research_capture(
            journal,
            [focus_mint, secondary_mint],
            cohort_started_at=300,
            focus_size=1,
            as_of=600,
        )

    assert result["focus"] == {
        "selected_mints": 1,
        "eligible_minute_slots": 5,
        "observed_closed_bars": 2,
        "trusted_timely_bars": 1,
        "mints_without_current_pool": 0,
    }
    assert result["secondary"] == {
        "selected_mints": 1,
        "eligible_minute_slots": 5,
        "observed_closed_bars": 0,
        "trusted_timely_bars": 0,
        "mints_without_current_pool": 1,
    }


def test_decision_cycle_integrity_distinguishes_complete_partial_and_corrupt() -> None:
    def row(cycle: str, rank: int, size: int, action: str = "observed") -> dict[str, object]:
        return {
            "action": action,
            "evidence": {
                "decision_cycle_id": cycle,
                "decision_cycle_rank": rank,
                "decision_cycle_size": size,
                "decision_cycle_started_at": "2026-11-03T00:00:00+00:00",
                "decision_cycle_breakers": [],
            },
        }

    result = summarize_decision_cycle_integrity(
        [
            row("complete", 1, 2, "paper_opened"),
            row("complete", 2, 2),
            row("partial", 2, 3),
            row("corrupt", 1, 2),
            row("corrupt", 1, 2),
            {"action": "observed", "evidence": {}},
        ]
    )
    assert result["legacy_without_cycle_id"] == 1
    assert result["statuses"] == {
        "complete": 1,
        "partial_recent_window": 1,
        "inconsistent": 1,
    }
    assert cast(list[dict[str, object]], result["cycles"])[0]["paper_opened"] == 1


def test_saved_market_gate_replays_with_its_policy_and_narrative(tmp_path: Path) -> None:
    settings = Settings(
        rpc_url="https://rpc.example.test",
        keypair_path=None,
        jupiter_api_key=None,
        live_trading_enabled=False,
        max_trade_lamports=100_000_000,
        max_daily_lamports=200_000_000,
        max_slippage_bps=300,
        max_price_impact_bps=500,
        max_top10_concentration_bps=7_000,
        max_priority_fee_lamports=1_000_000,
        confirmation_timeout_seconds=45,
        allow_token_2022=False,
        state_path=tmp_path / "market-gate.sqlite3",
    )
    mint = str(Pubkey.new_unique())
    candidate: dict[str, object] = {
        "mint": mint,
        "audit_status": "safe",
        "liquidity_usd": "75000",
        "volume_24h_usd": "100000",
        "price_change_24h": "10",
        "pair_created_at": 1_700_000_000_000,
        "narrative_signal": {
            "updated_at": "2023-11-14T23:13:20+00:00",
            "latest_mention_at": "2023-11-14T23:13:20+00:00",
            "confidence": "high",
            "explicit_contract_mentions": 2,
            "unique_sources": 3,
            "risk_flags": [],
        },
    }
    original = evaluate_candidate(candidate, settings, now_ms=1_700_003_600_000)
    saved_evidence = json.loads(json.dumps(original.evidence))
    saved = {"mint": mint, "evidence": saved_evidence}
    assert verify_paper_entry_market_gate(saved, settings)["status"] == "matched"

    saved_evidence["market_gate_inputs"]["narrative_signal"]["risk_flags"] = [
        "security_claim_present"
    ]
    assert verify_paper_entry_market_gate(saved, settings)["status"] == "mismatch"
    saved_evidence["market_gate_inputs"]["narrative_signal"]["risk_flags"] = []

    saved_evidence["market_gate_score"] = original.score - 5
    assert verify_paper_entry_market_gate(saved, settings)["status"] == "mismatch"

    saved_evidence.pop("market_policy_version")
    assert verify_paper_entry_market_gate(saved, settings) == {
        "status": "unverifiable",
        "reason": "market_policy_version_missing_or_unknown",
    }

    mismatched_evidence = json.loads(json.dumps(original.evidence))
    mismatched_evidence["market_gate_score"] = original.score - 5
    with TradeJournal(settings.state_path) as journal:
        journal.record_strategy_decision(original, action="observed")
        journal.record_strategy_decision(
            replace(original, evidence=saved_evidence), action="paper_opened"
        )
        journal.record_strategy_decision(
            replace(original, evidence=mismatched_evidence), action="observed"
        )
        summary = summarize_saved_market_gate_reproducibility(journal, settings)
        assert summary["checked"] == 3
        assert summary["truncated"] is False
        assert summary["statuses"] == {"mismatch": 1, "unverifiable": 1, "matched": 1}
        assert summary["paper_entry_statuses"] == {"unverifiable": 1}
        assert summary["unverifiable_reasons"] == {"market_policy_version_missing_or_unknown": 1}
        assert summary["historical_market_data_parity"] is False
        assert (
            summarize_saved_market_gate_reproducibility(journal, settings, limit=1)["truncated"]
            is True
        )


def test_current_entry_readiness_uses_real_entry_gates(tmp_path: Path) -> None:
    settings = Settings(
        rpc_url="https://rpc.example.test",
        keypair_path=None,
        jupiter_api_key=None,
        live_trading_enabled=False,
        max_trade_lamports=100_000_000,
        max_daily_lamports=200_000_000,
        max_slippage_bps=300,
        max_price_impact_bps=500,
        max_top10_concentration_bps=7_000,
        max_priority_fee_lamports=1_000_000,
        confirmation_timeout_seconds=45,
        allow_token_2022=False,
        state_path=tmp_path / "entry-readiness.sqlite3",
    )
    mint = str(Pubkey.new_unique())
    pool = str(Pubkey.new_unique())
    with TradeJournal(settings.state_path) as journal:
        journal.upsert_discovery_candidate(
            mint=mint,
            source_program="test",
            audit_status="safe",
            audit_summary=None,
            top10_concentration_bps=100,
            decimals=6,
            market={
                "pair_address": pool,
                "liquidity_usd": "75000",
                "volume_24h_usd": "100000",
                "price_change_24h": "10",
            },
        )
        missing_pool = summarize_current_entry_readiness(journal, settings)
        assert missing_pool["market_gate_eligible"] == 1
        assert missing_pool["pre_candle_gate_blocked"] == 1
        assert missing_pool["candle_evaluated"] == 0
        assert missing_pool["entry_eligible"] == 0

        journal.record_market_snapshot(
            mint=mint,
            pool=pool,
            price_usd="1",
            liquidity_usd="75000",
            volume_24h_usd="100000",
        )
        missing_bars = summarize_current_entry_readiness(journal, settings)
        assert missing_bars["pre_candle_gate_blocked"] == 0
        assert missing_bars["candle_evaluated"] == 1
        assert missing_bars["entry_eligible"] == 0
        assert missing_bars["candle_data_limited"] == 1
        assert missing_bars["signal_misaligned"] == 0
        frame_statuses = cast(dict[str, dict[str, int]], missing_bars["frame_statuses"])
        assert frame_statuses["1m"] == {"collecting": 1}
        assert frame_statuses["5m"] == {"collecting": 1}


def test_research_lane_diagnostic_keeps_secondary_censoring_visible() -> None:
    cohort = [f"mint-{index}" for index in range(10)]
    bars: list[tuple[str, int, object, object]] = [
        ("mint-0", 100, 220, 220),
        ("mint-1", 100, 500, 500),
        ("mint-8", 100, 200, 400),
        ("mint-9", 100, 200, 200),
    ]
    replay: list[dict[str, Any]] = [
        {"mint": "mint-0", "status": "censored_unclosed", "censored_reasons": {"series_end": 1}},
        {
            "mint": "mint-8",
            "status": "censored_unclosed",
            "censored_reasons": {"exit_candle_unobserved": 1},
        },
        {"mint": "old-mint", "status": "no_eligible_trades"},
    ]
    result = summarize_current_research_lanes(cohort, bars, replay)
    assert result["scope"] == "current_runtime_cohort_only"
    assert result["unclassified_historical_replay_mints"] == 1
    assert result["focus"] == {
        "selected_mints": 8,
        "membership_1m_candles": 2,
        "fetched_within_120s_of_close": 1,
        "trusted_and_fetched_within_120s_of_close": 1,
        "replay_statuses": {"censored_unclosed": 1},
        "censored_reasons": {"series_end": 1},
    }
    assert result["secondary"] == {
        "selected_mints": 2,
        "membership_1m_candles": 2,
        "fetched_within_120s_of_close": 2,
        "trusted_and_fetched_within_120s_of_close": 1,
        "replay_statuses": {"censored_unclosed": 1},
        "censored_reasons": {"exit_candle_unobserved": 1},
    }
    focused = summarize_current_research_lanes(cohort, bars, replay, focus_size=2)
    assert cast(dict[str, object], focused["focus"])["selected_mints"] == 2
    assert cast(dict[str, object], focused["secondary"])["selected_mints"] == 8
    windowed = summarize_current_research_lanes(
        cohort,
        bars,
        replay,
        focus_size=2,
        current_windows={mint: ((300, 500) if mint == "mint-0" else (0, 1_000)) for mint in cohort},
    )
    assert cast(dict[str, object], windowed["focus"])["membership_1m_candles"] == 1
    assert windowed["replay_status_scope"] == "all_recorded_windows_for_current_mints"


def test_recent_decision_funnel_counts_saved_gates_without_claiming_trades() -> None:
    rows: list[dict[str, object]] = [
        {
            "created_at": "2026-11-03T00:00:03+00:00",
            "action": "circuit_breaker:position_limit",
            "verdict": "eligible",
            "evidence": {
                "audit_status": "safe",
                "candle_signals": {
                    "1m": {"status": "ready", "confirmed": True},
                    "5m": {"status": "ready", "confirmed": True},
                },
            },
        },
        {
            "created_at": "2026-11-03T00:00:02+00:00",
            "action": "observed",
            "verdict": "observe",
            "evidence": {
                "audit_status": "safe",
                "candle_signals": {
                    "1m": {"status": "ready", "confirmed": True},
                    "5m": {"status": "gap", "confirmed": False},
                },
            },
        },
        {
            "created_at": "2026-11-03T00:00:01+00:00",
            "action": "rejected",
            "verdict": "reject",
            "evidence": {"audit_status": "blocked"},
        },
    ]
    result = summarize_recent_decisions(rows)
    assert result["sample_size"] == 3
    assert result["audit_safe"] == 2
    assert result["market_gate_passed"] == 2
    assert result["required_frames_ready"] == 1
    assert result["required_frames_confirmed"] == 1
    assert result["candle_statuses"] == {"1m": {"ready": 2}, "5m": {"ready": 1, "gap": 1}}
    assert result["actions"] == {
        "circuit_breaker:position_limit": 1,
        "observed": 1,
        "rejected": 1,
    }


def test_performance_report_uses_research_population_and_counts_missing_candles(
    tmp_path: Path,
) -> None:
    sampled_with_candle = str(Pubkey.new_unique())
    sampled_without_candle = str(Pubkey.new_unique())
    sampled_with_only_preselection_candle = str(Pubkey.new_unique())
    safe_watchlist_only = str(Pubkey.new_unique())
    pool = str(Pubkey.new_unique())
    now = int(time.time())
    opened_at = (now // 60) * 60 - 600
    recent_opened_at = (now // 60) * 60 - 60
    untrusted_opened_at = (now // 60) * 60 - 120
    late_observed_opened_at = (now // 60) * 60 - 180
    settings = Settings(
        rpc_url="https://rpc.example.test",
        keypair_path=None,
        jupiter_api_key=None,
        live_trading_enabled=False,
        max_trade_lamports=100_000_000,
        max_daily_lamports=200_000_000,
        max_slippage_bps=300,
        max_price_impact_bps=500,
        max_top10_concentration_bps=7_000,
        max_priority_fee_lamports=1_000_000,
        confirmation_timeout_seconds=45,
        allow_token_2022=False,
        state_path=tmp_path / "report.sqlite3",
    )
    with TradeJournal(settings.state_path) as journal:
        for mint in (
            sampled_with_candle,
            sampled_without_candle,
            sampled_with_only_preselection_candle,
            safe_watchlist_only,
        ):
            journal.upsert_discovery_candidate(
                mint=mint,
                source_program="test",
                audit_status="safe",
                audit_summary=None,
                top10_concentration_bps=1_000,
                decimals=6,
                market={"pair_address": pool},
            )
        journal.record_research_selection(
            [sampled_with_candle, sampled_without_candle, sampled_with_only_preselection_candle],
            cohort_started_at=now - 1_800,
            selected_at=now - 1_800,
            expires_at=now + 3_600,
            population_count=4,
            method=RESEARCH_SELECTION_METHOD,
        )
        journal.record_research_selection(
            [safe_watchlist_only],
            cohort_started_at=now - 2_000,
            selected_at=now - 2_000,
            expires_at=now + 3_600,
            population_count=3,
            method="sha256_day_mint_from_100_recent_first_seen",
        )
        journal.record_pool_candles(
            [
                PoolCandle(mint, pool, "1m", opened_at, "1", "1", "1", "1", "100")
                for mint in (sampled_with_candle, safe_watchlist_only)
            ]
            + [
                PoolCandle(sampled_with_candle, pool, "1m", stamp, "1", "1", "1", "1", "100")
                for stamp in (recent_opened_at, untrusted_opened_at)
            ]
            + [
                PoolCandle(
                    sampled_with_only_preselection_candle,
                    pool,
                    "1m",
                    (now // 60) * 60 - 3_600,
                    "1",
                    "1",
                    "1",
                    "1",
                    "100",
                )
            ],
            fetched_at=now,
        )
        journal.record_pool_candles(
            [
                PoolCandle(
                    sampled_with_candle,
                    pool,
                    "1m",
                    late_observed_opened_at,
                    "1",
                    "1",
                    "1",
                    "1",
                    "100",
                )
            ],
            fetched_at=late_observed_opened_at + 90,
        )
        complete_hour = (now // 3_600 - 1) * 3_600
        journal.record_pool_candles(
            [
                PoolCandle(
                    safe_watchlist_only,
                    pool,
                    "1m",
                    complete_hour + minute * 60,
                    "1",
                    "1",
                    "1",
                    "1",
                    "100",
                )
                for minute in range(60)
            ],
            fetched_at=now,
        )
        journal._connection.execute(
            "UPDATE pool_candles SET first_observed_values_at=NULL WHERE mint=? AND opened_at=?",
            (sampled_with_candle, untrusted_opened_at),
        )
        journal._connection.execute(
            "UPDATE pool_candles SET first_observed_values_at=? WHERE mint=? AND opened_at=?",
            (late_observed_opened_at + 181, sampled_with_candle, late_observed_opened_at),
        )
        journal.record_collector_signal_observation(
            StrategyDecision(
                sampled_with_candle,
                74,
                "observe",
                ("5m confirmation is missing",),
                {"pool": pool, "candle_signals": {"1m": {"status": "ready"}}},
            )
        )

    evidence = report(settings)
    assert evidence["live_execution_locked"] is True
    assert evidence["environment_live_flag"] is False
    assert evidence["counts"]["collector_signal_observations"] == 1
    assert evidence["collector_signal_funnel"]["verdicts"] == {"observe": 1}
    assert evidence["collector_signal_funnel"]["frame_statuses"]["1m"] == {"ready": 1}
    assert evidence["collector_signal_funnel"]["frame_statuses"]["5m"] == {"not_evaluated": 1}
    assert evidence["collector_signal_price_paths"]["population"] == (
        "prospective_focused_collector_observations_only"
    )
    assert evidence["estimated_paper_swap_fraction_bps"] == 79
    assert evidence["paper_total_debit_budget_bps"] == settings.paper_risk_per_trade_bps
    replay = evidence["baseline_candlestick_replay"]
    assert {row["mint"] for row in replay} == {
        sampled_with_candle,
        sampled_without_candle,
        sampled_with_only_preselection_candle,
    }
    assert next(row for row in replay if row["mint"] == sampled_without_candle)["status"] == (
        "missing_1m_history"
    )
    assert (
        next(row for row in replay if row["mint"] == sampled_with_only_preselection_candle)[
            "status"
        ]
        == "missing_1m_history"
    )
    assert all(row["historical_market_gate_parity"] is False for row in replay)
    assert all(row["paper_entry_schedule_parity"] is False for row in replay)
    assert evidence["strategy_comparison"]["status"] == "unavailable"
    assert evidence["strategy_comparison"]["baseline_comparable_closed_trades"] == 0
    assert evidence["strategy_comparison"]["upgraded_momentum_strategy"] == (
        "not_implemented_indicators_deferred"
    )
    assert evidence["replay_population"] == {
        "scope": "recorded_v2_recent_discovery_memberships_only",
        "history_scope": "saved_1m_bar_inside_recorded_membership_window",
        "selection_method": RESEARCH_SELECTION_METHOD,
        "selected_mints": 3,
        "legacy_membership_mints_excluded": 1,
        "mints_with_1m_history": 1,
        "mints_without_1m_history": 2,
    }
    assert evidence["research_active_observation"] == {
        "mint": None,
        "selected_at": None,
        "cohort_selected_at": None,
        "selection_tier": None,
        "ended_windows_recorded": 0,
    }
    assert evidence["research_1m_fetch_availability"] == {
        "membership_candles": 4,
        "first_fetch_known": 4,
        "first_fetch_unknown": 0,
        "first_observed_values_trusted": 3,
        "first_observed_values_unknown": 1,
        "fetched_within_120s_of_close": 3,
        "trusted_and_fetched_within_120s_of_close": 1,
    }
    assert any(row["mint"] == safe_watchlist_only for row in evidence["ohlcv_coverage"])
    derived_rows = {
        row["timeframe"]: row
        for row in evidence["ohlcv_coverage"]
        if row["mint"] == safe_watchlist_only
    }
    assert derived_rows["15m"]["stored_count"] == 0
    assert derived_rows["15m"]["derived_from_1m_count"] >= 4
    assert derived_rows["1h"]["stored_count"] == 0
    assert derived_rows["1h"]["derived_from_1m_count"] == 1


def test_paper_entry_parity_excludes_bars_first_seen_after_decision(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    decision_at = 7_201

    def bars(start: int, period: int, count: int, *, fetched_at: int) -> list[Candle]:
        return [
            Candle(
                opened_at=start + index * period,
                open=1 + index * 0.02,
                high=1.02 + index * 0.02,
                low=0.99 + index * 0.02,
                close=1.01 + index * 0.02,
                volume_delta_usd=100 + index * 50,
                liquidity_usd=50_000,
                samples=1,
                source="geckoterminal_pool_trades",
                first_fetched_at=fetched_at,
                first_observed_values_at=fetched_at,
            )
            for index in range(count)
        ]

    available = {
        "1m": bars(6_720, 60, 8, fetched_at=decision_at),
        "5m": bars(4_800, 300, 8, fetched_at=decision_at),
        "15m": bars(0, 900, 8, fetched_at=decision_at + 100),
        "1h": bars(0, 3_600, 2, fetched_at=decision_at + 100),
    }
    monkeypatch.setattr(
        "memecoin_bot.verification.load_verified_candles",
        lambda _journal, _mint, _pool, frame, *, now: available[frame],
    )
    original = {
        frame: analyze_entry(
            available[frame] if frame in {"1m", "5m"} else [],
            minimum_liquidity_usd=25_000,
            timeframe=frame,
            as_of=decision_at,
        )
        for frame in ("1m", "5m", "15m", "1h")
    }
    evidence: dict[str, Any] = {
        "decision_as_of": decision_at,
        "pool": "pool",
        "candle_policy": {"minimum_liquidity_usd": "25000", "signal_threshold": 75},
        "candle_signals": {
            frame: json.loads(json.dumps(signal.as_dict())) for frame, signal in original.items()
        },
        "latest_closed_candle_at": {
            "1m": available["1m"][-1].opened_at,
            "5m": available["5m"][-1].opened_at,
            "15m": None,
            "1h": None,
        },
    }
    decision: dict[str, object] = {"mint": "mint", "evidence": evidence}
    matched = verify_paper_entry_signal(
        cast(TradeJournal, object()), decision, default_minimum_liquidity_usd=30_000
    )
    assert matched["status"] == "matched"
    assert matched["policy_source"] == "recorded"
    assert cast(dict[str, object], matched["known_candles"])["15m"] == 0
    assert all(cast(dict[str, bool], matched["frame_matches"]).values())

    evidence["candle_signals"]["1m"]["score"] = 0
    mismatched = verify_paper_entry_signal(
        cast(TradeJournal, object()), decision, default_minimum_liquidity_usd=30_000
    )
    assert mismatched["status"] == "mismatch"
    assert cast(dict[str, bool], mismatched["frame_matches"])["1m"] is False

    evidence["candle_policy"]["minimum_liquidity_usd"] = "1000000"
    evidence["candle_signals"] = {
        frame: json.loads(
            json.dumps(
                analyze_entry(
                    available[frame] if frame in {"1m", "5m"} else [],
                    minimum_liquidity_usd=1_000_000,
                    timeframe=frame,
                    as_of=decision_at,
                ).as_dict()
            )
        )
        for frame in ("1m", "5m", "15m", "1h")
    }
    decision["verdict"] = "observe"
    observed = verify_paper_entry_signal(
        cast(TradeJournal, object()), decision, default_minimum_liquidity_usd=30_000
    )
    assert observed["status"] == "matched"
    assert observed["expected_verdict"] == "observe"
    assert observed["replayed_entry_eligible"] is False


def test_signal_parity_marks_legacy_stale_history_unverifiable(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    decision_at = 50_000
    all_hourly = [
        Candle(
            opened_at=index * 3_600,
            open=1,
            high=1.1,
            low=0.9,
            close=1,
            volume_delta_usd=100,
            liquidity_usd=50_000,
            samples=1,
            source="geckoterminal_pool_trades",
            first_fetched_at=decision_at if index < 3 else decision_at + 100,
            first_observed_values_at=decision_at if index < 3 else decision_at + 100,
        )
        for index in range(8)
    ]
    monkeypatch.setattr(
        "memecoin_bot.verification.load_verified_candles",
        lambda _journal, _mint, _pool, frame, *, now: all_hourly if frame == "1h" else [],
    )
    frames: tuple[Timeframe, ...] = ("1m", "5m", "15m", "1h")
    recorded = {
        frame: json.loads(
            json.dumps(
                analyze_entry(
                    all_hourly if frame == "1h" else [],
                    minimum_liquidity_usd=25_000,
                    timeframe=frame,
                    as_of=decision_at,
                ).as_dict()
            )
        )
        for frame in frames
    }
    decision: dict[str, object] = {
        "mint": "mint",
        "verdict": "observe",
        "evidence": {
            "decision_as_of": decision_at,
            "pool": "pool",
            "candle_policy": {"minimum_liquidity_usd": "25000", "signal_threshold": 75},
            "candle_signals": recorded,
            "latest_closed_candle_at": {
                frame: all_hourly[-1].opened_at if frame == "1h" else None for frame in frames
            },
        },
    }
    with TradeJournal(tmp_path / "signal-parity.sqlite3") as journal:
        result = verify_paper_entry_signal(journal, decision, default_minimum_liquidity_usd=30_000)
    assert result["status"] == "unverifiable"
    assert result["reason"] == "historical_candle_provenance_incomplete"
    assert result["provenance_limited_frames"] == ["1h"]
