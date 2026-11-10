from __future__ import annotations

import time
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest
from solders.pubkey import Pubkey

from memecoin_bot.config import Settings
from memecoin_bot.execution import PaperExecutionError
from memecoin_bot.forward_performance import has_signal_evidence
from memecoin_bot.journal import TradeJournal
from memecoin_bot.ohlcv import PoolCandle
from memecoin_bot.paper import PaperTradingService, StalePaperDecisionError
from memecoin_bot.strategy import (
    StrategyDecision,
    attempt_paper_entry,
    confirm_candidate_candles,
    evaluate_candidate,
    paper_entry_breakers,
    primary_decision_reason,
    refreshed_paper_entry_opportunities,
    replay_market_verdict,
    run_paper_decision_cycle,
)


def test_primary_reason_prefers_blocking_market_gate() -> None:
    assert (
        primary_decision_reason(
            "reject",
            ["on-chain audit did not pass", "no token-linked narrative evidence (+0)"],
            {"market_gate_reasons": ["on-chain audit did not pass", "liquidity is unavailable"]},
        )
        == "on-chain audit did not pass"
    )
    assert (
        primary_decision_reason(
            "observe",
            ["1m/5m trend, momentum, volume, and liquidity did not align"],
            {"market_gate_reasons": []},
        )
        == "1m/5m trend, momentum, volume, and liquidity did not align"
    )


def test_forward_paper_entries_require_current_exit_and_watchdog_workers(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    with TradeJournal(settings.state_path) as journal:
        journal.set_runtime_state(
            "paper_campaign", {"mode": "forward_paper_only", "status": "active"}
        )
        assert "exit_monitor_unavailable" in paper_entry_breakers(settings, journal)
        assert "paper_watchdog_unavailable" in paper_entry_breakers(settings, journal)

        completed_at = datetime.now(UTC).isoformat()
        shared: dict[str, object] = {
            "state": "running",
            "running": True,
            "run_id": "current-run",
            "last_completed_run_id": "current-run",
            "last_completed_at": completed_at,
            "last_error": None,
        }
        journal.set_runtime_state(
            "paper_exit_monitor",
            {**shared, "last_result": {"quote_failures": 0, "exit_failures": 0}},
        )
        assert "exit_monitor_unavailable" not in paper_entry_breakers(settings, journal)
        assert "paper_watchdog_unavailable" in paper_entry_breakers(settings, journal)

        journal.set_runtime_state("paper_watchdog", shared)
        reasons = paper_entry_breakers(settings, journal)
        assert "exit_monitor_unavailable" not in reasons
        assert "paper_watchdog_unavailable" not in reasons


def test_replay_market_score_uses_narrative_refresh_available_at_decision(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    mint = str(Pubkey.new_unique())
    snapshot: dict[str, object] = {
        "liquidity_usd": "100000",
        "volume_24h_usd": "250000",
        "price_change_24h": "25",
        "pair_created_at": int(time.time() * 1_000) - 1_200_000,
    }
    with TradeJournal(settings.state_path) as journal:
        journal.record_strategy_decision(
            evaluate_candidate({**snapshot, "mint": mint, "audit_status": "safe"}, settings),
            action="observed",
        )
        journal.replace_narrative_signals(
            [
                {
                    "mint": mint,
                    "confidence": "high",
                    "explicit_contract_mentions": 2,
                    "unique_sources": 2,
                    "risk_flags": ["security_claim_present"],
                }
            ]
        )
        first_us = journal._connection.execute(
            "SELECT captured_at_us FROM narrative_signal_observations ORDER BY refresh_id LIMIT 1"
        ).fetchone()[0]
        assert (
            replay_market_verdict(
                journal,
                settings,
                mint=mint,
                snapshot=snapshot,
                decision_at_ms=(first_us - 1) // 1_000,
            )
            is None
        )
        first_ms = (first_us + 999) // 1_000
        blocked = replay_market_verdict(
            journal, settings, mint=mint, snapshot=snapshot, decision_at_ms=first_ms
        )
        assert blocked is not None and blocked[0] == "reject"

        time.sleep(0.003)
        journal.replace_narrative_signals([])
        second_us = journal._connection.execute(
            "SELECT captured_at_us FROM narrative_signal_observations "
            "ORDER BY refresh_id DESC LIMIT 1"
        ).fetchone()[0]
        assert (
            replay_market_verdict(
                journal, settings, mint=mint, snapshot=snapshot, decision_at_ms=first_ms
            )
            == blocked
        )
        after_clear = replay_market_verdict(
            journal,
            settings,
            mint=mint,
            snapshot=snapshot,
            decision_at_ms=(second_us + 999) // 1_000,
        )
        assert after_clear is not None and after_clear[0] == "eligible"


def test_replay_market_score_requires_prior_recent_matching_policy(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    mint = str(Pubkey.new_unique())
    snapshot: dict[str, object] = {
        "liquidity_usd": "100000",
        "volume_24h_usd": "250000",
        "price_change_24h": "25",
        "pair_created_at": int(time.time() * 1_000) - 1_200_000,
    }
    with TradeJournal(settings.state_path) as journal:
        journal.replace_narrative_signals([])
        narrative_us = int(
            journal._connection.execute(
                "SELECT captured_at_us FROM narrative_signal_observations "
                "ORDER BY refresh_id DESC LIMIT 1"
            ).fetchone()[0]
        )
        before_policy_ms = (narrative_us + 999) // 1_000
        assert replay_market_verdict(
            journal,
            settings,
            mint=mint,
            snapshot=snapshot,
            decision_at_ms=before_policy_ms,
        ) == ("market_policy_missing", None)

        journal.record_strategy_decision(
            evaluate_candidate({**snapshot, "mint": mint, "audit_status": "safe"}, settings),
            action="observed",
        )
        recorded = journal.market_policy_before(mint, decision_at_us=4_102_444_800_000_000)
        assert recorded is not None
        observed_us = int(recorded["observed_at_us"])
        assert journal.market_policy_before(mint, decision_at_us=observed_us - 1) is None
        decision_at_ms = (observed_us + 999) // 1_000 + 1
        assert (
            replay_market_verdict(
                journal,
                settings,
                mint=mint,
                snapshot=snapshot,
                decision_at_ms=decision_at_ms,
            )[0]
            == "eligible"
        )
        assert replay_market_verdict(
            journal,
            replace(settings, strategy_min_score=95),
            mint=mint,
            snapshot=snapshot,
            decision_at_ms=decision_at_ms,
        ) == ("market_policy_mismatch", None)
        assert replay_market_verdict(
            journal,
            settings,
            mint=mint,
            snapshot=snapshot,
            decision_at_ms=decision_at_ms
            + 2 * settings.paper_automation_interval_seconds * 1_000
            + 1_000,
        ) == ("market_policy_stale", None)


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        rpc_url="https://rpc.example.test",
        keypair_path=None,
        jupiter_api_key=None,
        live_trading_enabled=False,
        max_trade_lamports=50_000_000,
        max_daily_lamports=100_000_000,
        max_slippage_bps=300,
        max_price_impact_bps=500,
        max_top10_concentration_bps=7_000,
        max_priority_fee_lamports=1_000_000,
        confirmation_timeout_seconds=45,
        allow_token_2022=False,
        state_path=tmp_path / "state.sqlite3",
    )


def test_strategy_selects_strong_audited_candidate(tmp_path: Path) -> None:
    candidate: dict[str, object] = {
        "mint": str(Pubkey.new_unique()),
        "audit_status": "safe",
        "liquidity_usd": "100000",
        "volume_24h_usd": "250000",
        "price_change_24h": "25",
        "pair_created_at": 1_700_000_000_000,
    }
    decision = evaluate_candidate(candidate, _settings(tmp_path), now_ms=1_700_003_600_000)
    assert decision.verdict == "eligible"
    assert decision.score >= 65
    assert any("audit passed" in reason for reason in decision.reasons)
    assert decision.evidence["market_gate_verdict"] == "eligible"
    assert decision.evidence["market_gate_score"] == decision.score
    assert decision.evidence["market_evaluated_at_ms"] == 1_700_003_600_000
    assert decision.evidence["market_policy"] == {
        "minimum_liquidity_usd": 25_000,
        "minimum_volume_24h_usd": 10_000,
        "maximum_absolute_24h_change_pct": 250,
        "minimum_score": 65,
        "maximum_narrative_age_seconds": 900,
        "maximum_narrative_mention_age_seconds": 3600,
    }


def test_strategy_rejects_low_liquidity_even_with_momentum(tmp_path: Path) -> None:
    candidate: dict[str, object] = {
        "mint": str(Pubkey.new_unique()),
        "audit_status": "safe",
        "liquidity_usd": "5000",
        "volume_24h_usd": "900000",
        "price_change_24h": "40",
    }
    decision = evaluate_candidate(candidate, _settings(tmp_path))
    assert decision.verdict == "reject"
    assert any("below" in reason and "liquidity" in reason for reason in decision.reasons)


def test_strategy_rejects_extreme_move_as_manipulation_guard(tmp_path: Path) -> None:
    candidate: dict[str, object] = {
        "mint": str(Pubkey.new_unique()),
        "audit_status": "safe",
        "liquidity_usd": "100000",
        "volume_24h_usd": "250000",
        "price_change_24h": "500",
    }
    decision = evaluate_candidate(candidate, _settings(tmp_path))
    assert decision.verdict == "reject"
    assert any("manipulation guard" in reason for reason in decision.reasons)
    assert decision.evidence["market_gate_verdict"] == "reject"
    assert any(
        "manipulation guard" in reason for reason in decision.evidence["market_gate_reasons"]
    )


def test_strategy_only_rewards_contract_linked_corroborated_narrative(tmp_path: Path) -> None:
    base: dict[str, object] = {
        "mint": str(Pubkey.new_unique()),
        "audit_status": "safe",
        "liquidity_usd": "100000",
        "volume_24h_usd": "250000",
        "price_change_24h": "25",
    }
    evaluated_at_ms = 1_700_000_000_000
    without_narrative = evaluate_candidate(base, _settings(tmp_path), now_ms=evaluated_at_ms)
    with_narrative = evaluate_candidate(
        {
            **base,
            "narrative_signal": {
                "updated_at": "2023-11-14T22:13:20+00:00",
                "latest_mention_at": "2023-11-14T22:13:20+00:00",
                "confidence": "high",
                "explicit_contract_mentions": 2,
                "unique_sources": 3,
                "risk_flags": [],
            },
        },
        _settings(tmp_path),
        now_ms=evaluated_at_ms,
    )

    assert with_narrative.score == min(100, without_narrative.score + 5)
    assert any(
        "contract-linked narrative confirmation" in reason for reason in with_narrative.reasons
    )


@pytest.mark.parametrize(
    "updated_at",
    [None, "invalid", "2023-11-14T21:58:19+00:00", "2023-11-14T22:13:21+00:00"],
)
def test_missing_stale_or_future_narrative_cannot_raise_entry_score(
    tmp_path: Path, updated_at: str | None
) -> None:
    candidate: dict[str, object] = {
        "mint": str(Pubkey.new_unique()),
        "audit_status": "safe",
        "liquidity_usd": "100000",
        "volume_24h_usd": "250000",
        "price_change_24h": "25",
    }
    settings = _settings(tmp_path)
    baseline = evaluate_candidate(candidate, settings, now_ms=1_700_000_000_000)
    with_narrative = evaluate_candidate(
        {
            **candidate,
            "narrative_signal": {
                "updated_at": updated_at,
                "latest_mention_at": "2023-11-14T22:13:20+00:00",
                "confidence": "high",
                "explicit_contract_mentions": 2,
                "unique_sources": 3,
                "risk_flags": [],
            },
        },
        settings,
        now_ms=1_700_000_000_000,
    )
    assert with_narrative.score == baseline.score
    assert any(
        "narrative observation is missing or older" in reason for reason in with_narrative.reasons
    )


def test_recalculated_old_mention_cannot_raise_entry_score(tmp_path: Path) -> None:
    candidate: dict[str, object] = {
        "mint": str(Pubkey.new_unique()),
        "audit_status": "safe",
        "liquidity_usd": "100000",
        "volume_24h_usd": "250000",
        "price_change_24h": "25",
    }
    settings = _settings(tmp_path)
    baseline = evaluate_candidate(candidate, settings, now_ms=1_700_000_000_000)
    with_narrative = evaluate_candidate(
        {
            **candidate,
            "narrative_signal": {
                "updated_at": "2023-11-14T22:13:20+00:00",
                "latest_mention_at": "2023-11-14T21:13:19+00:00",
                "confidence": "high",
                "explicit_contract_mentions": 2,
                "unique_sources": 3,
                "risk_flags": [],
            },
        },
        settings,
        now_ms=1_700_000_000_000,
    )
    assert with_narrative.score == baseline.score
    assert any("latest underlying mention" in reason for reason in with_narrative.reasons)


def test_current_market_evidence_can_complete_forward_entry_record(tmp_path: Path) -> None:
    mint = str(Pubkey.new_unique())
    candidate: dict[str, object] = {
        "mint": mint,
        "audit_status": "safe",
        "liquidity_usd": "100000",
        "volume_24h_usd": "250000",
        "price_change_24h": "25",
    }
    market = evaluate_candidate(candidate, _settings(tmp_path), now_ms=1_700_000_000_000)
    assert market.verdict == "eligible"
    evidence = {
        **market.evidence,
        "decision_as_of": 1_700_000_000,
        "audit_last_seen": "2023-11-14T22:13:20+00:00",
        "market_captured_at": 1_700_000_000,
        "candle_policy": {"signal_threshold": 75, "minimum_liquidity_usd": "10000"},
        "candle_signals": {
            frame: {"status": "ready", "confirmed": True, "score": 80}
            for frame in ("1m", "5m", "15m", "1h")
        },
    }
    assert has_signal_evidence(evidence)


@pytest.mark.parametrize(
    "confidence,flags",
    [
        ("medium", ["promotion_dominant"]),
        ("medium", ["social_only_evidence"]),
        ("high", ["social_only_evidence"]),
    ],
)
def test_promotional_or_social_only_narrative_adds_no_entry_points(
    tmp_path: Path, confidence: str, flags: list[str]
) -> None:
    base: dict[str, object] = {
        "mint": str(Pubkey.new_unique()),
        "audit_status": "safe",
        "liquidity_usd": "100000",
        "volume_24h_usd": "250000",
        "price_change_24h": "25",
    }
    original = evaluate_candidate(base, _settings(tmp_path))
    with_narrative = evaluate_candidate(
        {
            **base,
            "narrative_signal": {
                "confidence": confidence,
                "explicit_contract_mentions": 1,
                "unique_sources": 2,
                "risk_flags": flags,
            },
        },
        _settings(tmp_path),
    )
    assert with_narrative.score == original.score


def test_strategy_rejects_corroborated_contract_security_claim(tmp_path: Path) -> None:
    candidate: dict[str, object] = {
        "mint": str(Pubkey.new_unique()),
        "audit_status": "safe",
        "liquidity_usd": "100000",
        "volume_24h_usd": "250000",
        "price_change_24h": "25",
        "narrative_signal": {
            "confidence": "high",
            "explicit_contract_mentions": 2,
            "unique_sources": 2,
            "risk_flags": ["security_claim_present"],
        },
    }

    decision = evaluate_candidate(candidate, _settings(tmp_path))

    assert decision.verdict == "reject"
    assert any("security claim" in reason for reason in decision.reasons)


def test_autonomous_paper_entry_waits_for_fresh_real_candles(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    mint = str(Pubkey.new_unique())
    pool = str(Pubkey.new_unique())
    calls: list[str] = []

    def open_paper(
        *,
        mint: str,
        lamports: int,
        slippage_bps: int,
        decision: StrategyDecision,
        entry_source: str,
    ) -> dict[str, object]:
        calls.append(mint)
        assert entry_source == "autonomous"
        journal.record_strategy_decision(decision, action="paper_opened", position_id="paper-test")
        return {"position": {"position_id": "paper-test"}}

    paper = cast(PaperTradingService, SimpleNamespace(open=open_paper))
    now = int(datetime.now(UTC).timestamp())
    with TradeJournal(settings.state_path) as journal:
        journal.upsert_discovery_candidate(
            mint=mint,
            source_program="test",
            audit_status="safe",
            audit_summary=None,
            top10_concentration_bps=1_000,
            decimals=6,
            market={
                "pair_address": pool,
                "price_usd": "1",
                "liquidity_usd": "100000",
                "volume_24h_usd": "250000",
                "price_change_24h": "25",
                "pair_created_at": (now - 3_600) * 1_000,
            },
        )
        journal.upsert_discovery_candidate(
            mint=str(Pubkey.new_unique()),
            source_program="test",
            audit_status="blocked",
            audit_summary="mint authority active",
            top10_concentration_bps=9_000,
            decimals=6,
        )
        missing = run_paper_decision_cycle(settings=settings, paper=paper, journal=journal)
        assert missing["opened"] is None
        assert calls == []
        decision_count = len(journal.strategy_decisions())
        assert refreshed_paper_entry_opportunities(settings, journal, [mint]) == []
        assert len(journal.strategy_decisions()) == decision_count
        assert journal.collector_signal_observations()[0]["verdict"] == "observe"

        first_five_minute = (now // 300 - 8) * 300
        for frame, seconds in (("1m", 60), ("5m", 300)):
            latest = (now // seconds - 1) * seconds
            candles = []
            for index in range(8):
                opened = latest - (7 - index) * seconds
                price = (
                    1 + index * 0.02
                    if frame == "5m"
                    else 1
                    + ((opened - first_five_minute) // 300) * 0.02
                    + ((opened % 300) // 60 - 4) * 0.003
                )
                opened_price = price - 0.012 if frame == "5m" else price
                candles.append(
                    PoolCandle(
                        mint=mint,
                        pool=pool,
                        timeframe=frame,
                        opened_at=opened,
                        open=str(opened_price),
                        high=str(price * 1.02),
                        low=str(opened_price * 0.99),
                        close=str(price * 1.01),
                        volume_usd=str(100 + index * 50),
                    )
                )
                journal.record_market_snapshot(
                    mint=mint,
                    pool=pool,
                    price_usd=str(price),
                    liquidity_usd="100000",
                    volume_24h_usd="250000",
                    observed_at=opened + seconds - 1,
                )
            journal.record_pool_candles(candles, fetched_at=now)
        assert refreshed_paper_entry_opportunities(settings, journal, [mint]) == [mint]
        assert refreshed_paper_entry_opportunities(settings, journal, []) == []
        assert [row["verdict"] for row in journal.collector_signal_observations()] == [
            "eligible",
            "observe",
        ]
        cycle_result = run_paper_decision_cycle(settings=settings, paper=paper, journal=journal)
        decisions = journal.strategy_decisions()
        blocked_result = run_paper_decision_cycle(
            settings=settings,
            paper=paper,
            journal=journal,
            forced_breaker_reasons=("position_limit",),
        )
        blocked_decisions = journal.strategy_decisions()
    assert cycle_result["opened"] is not None
    assert calls == [mint]
    assert len(decisions) == 4
    first_cycle = [
        item
        for item in decisions
        if item["evidence"]["decision_cycle_id"] == cycle_result["cycle_id"]
    ]
    assert len(first_cycle) == 2
    assert {item["evidence"]["decision_cycle_rank"] for item in first_cycle} == {1, 2}
    assert all(item["evidence"]["decision_cycle_size"] == 2 for item in first_cycle)
    assert all(item["evidence"]["decision_cycle_breakers"] == [] for item in first_cycle)
    assert all(
        isinstance(item["evidence"]["decision_cycle_started_at"], str) for item in first_cycle
    )
    assert blocked_result["entry_paused"] is True
    assert blocked_result["cycle_id"] != cycle_result["cycle_id"]
    assert blocked_result["evaluated"] == 2
    assert blocked_result["opened"] is None
    assert calls == [mint]
    assert len(blocked_decisions) == 6
    assert any(item["action"] == "circuit_breaker:position_limit" for item in blocked_decisions)
    assert all(
        item["evidence"]["decision_cycle_breakers"] == ["position_limit"]
        for item in blocked_decisions
        if item["evidence"]["decision_cycle_id"] == blocked_result["cycle_id"]
    )
    opened_decision = next(item for item in decisions if item["action"] == "paper_opened")
    evidence = opened_decision["evidence"]
    assert isinstance(evidence, dict)
    assert evidence["strategy"] == "baseline_four_factor"
    assert evidence["market_gate_verdict"] == "eligible"
    assert evidence["market_gate_score"] >= settings.strategy_min_score
    assert evidence["market_policy"]["minimum_score"] == settings.strategy_min_score
    assert evidence["market_policy"]["maximum_absolute_24h_change_pct"] == 250
    assert evidence["market_gate_reasons"] == []
    assert evidence["candle_signals"]["1m"]["confirmed"] is True
    assert evidence["candle_policy"]["signal_threshold"] == 75
    assert float(evidence["candle_policy"]["minimum_liquidity_usd"]) == float(
        settings.strategy_min_liquidity_usd
    )


def test_stale_audit_or_market_snapshot_blocks_paper_entry(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    mint = str(Pubkey.new_unique())
    pool = str(Pubkey.new_unique())
    with TradeJournal(settings.state_path) as journal:
        journal.upsert_discovery_candidate(
            mint=mint,
            source_program="test",
            audit_status="safe",
            audit_summary=None,
            top10_concentration_bps=1_000,
            decimals=6,
            market={
                "pair_address": pool,
                "price_usd": "1",
                "liquidity_usd": "100000",
                "volume_24h_usd": "250000",
                "price_change_24h": "25",
            },
        )
        candidate = journal.discovery_candidates()[0]
        initial = evaluate_candidate(candidate, settings)
        assert initial.verdict == "eligible"

        candidate["last_seen"] = (datetime.now(UTC) - timedelta(minutes=11)).isoformat()
        stale_audit = confirm_candidate_candles(candidate, initial, settings, journal)
        assert stale_audit.verdict == "observe"
        assert any("audit" in reason for reason in stale_audit.reasons)

        candidate["last_seen"] = datetime.now(UTC).isoformat()
        journal._connection.execute(
            "UPDATE market_snapshots SET captured_at=? WHERE mint=?",
            (int(datetime.now(UTC).timestamp()) - 181, mint),
        )
        stale_market = confirm_candidate_candles(candidate, initial, settings, journal)
        assert stale_market.verdict == "observe"
        assert any("snapshot" in reason for reason in stale_market.reasons)


def test_paper_entry_records_stable_execution_rejection_reason(tmp_path: Path) -> None:
    settings = _settings(tmp_path)

    def reject_entry(**_: object) -> None:
        raise PaperExecutionError(
            "Paper entry requires a positive route USD value and visible pool liquidity"
        )

    paper = cast(PaperTradingService, SimpleNamespace(open=reject_entry))
    decision = StrategyDecision(str(Pubkey.new_unique()), 90, "eligible", ("entry confirmed",))
    with TradeJournal(settings.state_path) as journal:
        action, opened = attempt_paper_entry(
            settings=settings, paper=paper, journal=journal, decision=decision
        )
    assert action == "simulation_failed:PaperExecutionError:entry_liquidity_unavailable"
    assert opened is None

    def changed_signal(**_: object) -> None:
        raise StalePaperDecisionError("Paper entry signal is no longer eligible")

    paper = cast(PaperTradingService, SimpleNamespace(open=changed_signal))
    with TradeJournal(settings.state_path) as journal:
        action, opened = attempt_paper_entry(
            settings=settings, paper=paper, journal=journal, decision=decision
        )
    assert action == "decision_changed_before_entry"
    assert opened is None


def test_paper_entry_risk_budget_includes_modeled_entry_costs(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    requested: list[int] = []

    def open_paper(*, lamports: int, **_: object) -> dict[str, object]:
        requested.append(lamports)
        return {"position": {"position_id": "sized-paper-entry"}}

    paper = cast(PaperTradingService, SimpleNamespace(open=open_paper))
    decision = StrategyDecision(str(Pubkey.new_unique()), 90, "eligible", ("entry confirmed",))
    with TradeJournal(settings.state_path) as journal:
        action, opened = attempt_paper_entry(
            settings=settings, paper=paper, journal=journal, decision=decision
        )
    cash_budget = settings.paper_starting_lamports * settings.paper_risk_per_trade_bps // 10_000
    entry_costs = (
        settings.paper_execution_network_fee_lamports
        + settings.paper_execution_priority_fee_lamports
        + settings.paper_execution_ata_rent_lamports
    )
    assert action == "paper_opened"
    assert opened is not None
    assert requested == [min(cash_budget - entry_costs, settings.max_trade_lamports)]
    assert requested[0] + entry_costs <= cash_budget


def test_paper_entry_rejects_budget_too_small_for_costs_and_minimum_swap(
    tmp_path: Path,
) -> None:
    settings = replace(_settings(tmp_path), paper_risk_per_trade_bps=1)
    paper = cast(PaperTradingService, SimpleNamespace(open=lambda **_: pytest.fail("unexpected")))
    decision = StrategyDecision(str(Pubkey.new_unique()), 90, "eligible", ("entry confirmed",))
    with TradeJournal(settings.state_path) as journal:
        action, opened = attempt_paper_entry(
            settings=settings, paper=paper, journal=journal, decision=decision
        )
    assert action == "insufficient_paper_risk_budget"
    assert opened is None


def test_all_paper_entry_paths_share_drawdown_and_liquidity_breakers(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    held_mint = str(Pubkey.new_unique())
    next_mint = str(Pubkey.new_unique())
    pool = str(Pubkey.new_unique())
    calls: list[str] = []

    def open_paper(*, mint: str, lamports: int, slippage_bps: int) -> dict[str, object]:
        calls.append(mint)
        return {"position": {"position_id": "unexpected"}}

    paper = cast(PaperTradingService, SimpleNamespace(open=open_paper))
    eligible = StrategyDecision(next_mint, 90, "eligible", ("entry confirmed",))
    with TradeJournal(settings.state_path) as journal:
        journal.upsert_discovery_candidate(
            mint=held_mint,
            source_program="test",
            audit_status="safe",
            audit_summary=None,
            top10_concentration_bps=1_000,
            decimals=6,
            market={
                "pair_address": pool,
                "price_usd": "1",
                "liquidity_usd": "100000",
                "volume_24h_usd": "250000",
            },
        )
        journal.record_market_snapshot(
            mint=held_mint,
            pool=pool,
            price_usd="1",
            liquidity_usd="100000",
            volume_24h_usd="250000",
        )
        journal.open_paper_position(
            position_id="held",
            mint=held_mint,
            entry_lamports=200_000_000,
            token_amount=1_000_000,
            simulated_units=0,
            starting_lamports=settings.paper_starting_lamports,
        )
        assert "open_position_mark_stale" in paper_entry_breakers(settings, journal)
        assert run_paper_decision_cycle(settings=settings, paper=paper, journal=journal)[
            "entry_paused"
        ]
        journal.mark_paper_position("held", 90_000_000)
        assert "portfolio_drawdown_limit" in paper_entry_breakers(settings, journal)
        action, opened = attempt_paper_entry(
            settings=settings, paper=paper, journal=journal, decision=eligible
        )
        assert action == "circuit_breaker:portfolio_drawdown_limit"
        assert opened is None
        journal.record_market_snapshot(
            mint=held_mint,
            pool=pool,
            price_usd="1",
            liquidity_usd="10000",
            volume_24h_usd="250000",
            observed_at=int(datetime.now(UTC).timestamp()) + 1,
        )
        assert "open_position_liquidity_limit" in paper_entry_breakers(settings, journal)
    assert calls == []
