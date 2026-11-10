from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from solders.pubkey import Pubkey

from memecoin_bot.constants import MARKET_POLICY_VERSION
from memecoin_bot.forward_performance import (
    _mark_path_covered,
    _position_mark_values,
    summarize_forward_paper,
)
from memecoin_bot.journal import TradeJournal
from memecoin_bot.strategy import StrategyDecision


def _execution(side: str, amount: int) -> dict[str, object]:
    return {
        "side": side,
        "cash_debit_lamports": amount if side == "buy" else 0,
        "cash_credit_lamports": amount if side in ("sell", "mark") else 0,
        "network_fee_lamports": 1,
        "priority_fee_lamports": 1,
        "ata_rent_lamports": 0,
        "modeled_slippage_lamports": 2,
        "explicit_cost_lamports": 2,
        "quote_age_ms": 10,
    }


def _revalidation() -> dict[str, object]:
    now = datetime.now(UTC)
    decision_at = int(now.timestamp())
    return {
        "score": 80,
        "verdict": "eligible",
        "reasons": ["final entry confirmed"],
        "market_policy_version": MARKET_POLICY_VERSION,
        "market_evaluated_at_ms": int(now.timestamp() * 1_000),
        "market_gate_verdict": "eligible",
        "market_gate_score": 80,
        "market_policy": {
            "minimum_score": 70,
            "minimum_liquidity_usd": 10_000,
            "minimum_volume_24h_usd": 50_000,
            "maximum_absolute_24h_change_pct": 250,
        },
        "market_gate_inputs": {
            "mint": None,
            "audit_status": "safe",
            "liquidity_usd": "20000",
            "volume_24h_usd": "100000",
            "price_change_24h": "10",
            "pair_created_at": None,
            "narrative_signal": None,
        },
        "decision_as_of": decision_at,
        "market_captured_at": decision_at,
        "audit_last_seen": now.isoformat(),
        "candle_policy": {"minimum_liquidity_usd": "10000", "signal_threshold": 75},
        "latest_closed_candle_at": {
            "1m": decision_at - 60,
            "5m": decision_at - 300,
            "15m": None,
            "1h": None,
        },
        "candle_signals": {
            frame: {"status": "ready", "confirmed": True, "score": 80, "trend": "up"}
            for frame in ("1m", "5m", "15m", "1h")
        },
    }


def _decision(mint: str, *, complete: bool = True) -> StrategyDecision:
    evidence: dict[str, object] = {}
    if complete:
        evidence = {
            "market_policy_version": MARKET_POLICY_VERSION,
            "market_evaluated_at_ms": 1_700_000_000_000,
            "market_gate_verdict": "eligible",
            "market_gate_score": 80,
            "market_policy": {
                "minimum_score": 70,
                "minimum_liquidity_usd": 10_000,
                "minimum_volume_24h_usd": 50_000,
                "maximum_absolute_24h_change_pct": 250,
            },
            "market_gate_inputs": {
                "mint": mint,
                "audit_status": "safe",
                "liquidity_usd": "20000",
                "volume_24h_usd": "100000",
                "price_change_24h": "10",
                "pair_created_at": None,
                "narrative_signal": None,
            },
            "decision_as_of": 1_700_000_000,
            "market_captured_at": 1_700_000_000,
            "audit_last_seen": "2026-11-03T00:00:00+00:00",
            "candle_policy": {"signal_threshold": 75, "minimum_liquidity_usd": "10000"},
            "candle_signals": {
                frame: {"status": "ready", "confirmed": True, "score": 80}
                for frame in ("1m", "5m", "15m", "1h")
            },
        }
    return StrategyDecision(mint, 80, "eligible", ("confirmed entry",), evidence)


def _closed(
    journal: TradeJournal,
    position_id: str,
    *,
    exit_lamports: int,
    action: str | None,
    complete_signal: bool = True,
    complete_execution: bool = True,
    costed_exit: bool = True,
    mark_values: tuple[int, ...] = (),
    ata_rent_lamports: int = 0,
    quote_source: str | None = None,
    revalidation: dict[str, object] | None = None,
) -> None:
    mint = str(Pubkey.new_unique())
    entry_execution = _execution("buy", 100_000) if complete_execution else None
    if entry_execution is not None:
        entry_execution["ata_rent_lamports"] = ata_rent_lamports
        entry_execution["explicit_cost_lamports"] = ata_rent_lamports + 2
        if quote_source is not None:
            entry_execution["quote_source"] = quote_source
        if revalidation is not None:
            inputs = revalidation["market_gate_inputs"]
            assert isinstance(inputs, dict)
            inputs["mint"] = mint
            entry_execution["entry_revalidation"] = revalidation
    journal.open_paper_position(
        position_id=position_id,
        mint=mint,
        entry_lamports=100_000,
        token_amount=1_000_000,
        simulated_units=100,
        starting_lamports=2_000_000,
        execution=entry_execution,
    )
    if action:
        journal.record_strategy_decision(
            _decision(mint, complete=complete_signal), action=action, position_id=position_id
        )
    for value in mark_values:
        journal.mark_paper_position(position_id, value, execution=_execution("mark", value))
    exit_execution = (
        _execution("sell", exit_lamports) if complete_execution and costed_exit else None
    )
    if exit_execution is not None and quote_source is not None:
        exit_execution["quote_source"] = quote_source
    journal.close_paper_position(
        position_id,
        exit_lamports,
        exit_reason="take_profit" if exit_lamports > 100_000 else "stop_loss",
        execution=exit_execution,
    )


def test_forward_paper_performance_excludes_manual_and_incomplete_history(tmp_path: Path) -> None:
    with TradeJournal(tmp_path / "forward.sqlite3") as journal:
        _closed(
            journal,
            "auto-win",
            exit_lamports=120_000,
            action="paper_opened",
            mark_values=(120_000, 110_000),
        )
        _closed(
            journal,
            "auto-loss",
            exit_lamports=80_000,
            action="paper_opened",
            mark_values=(90_000, 80_000),
        )
        _closed(journal, "called", exit_lamports=130_000, action="call_paper_opened")
        _closed(journal, "manual", exit_lamports=110_000, action="manual_paper_opened")
        _closed(journal, "legacy", exit_lamports=200_000, action=None, complete_execution=False)
        _closed(
            journal,
            "missing-signals",
            exit_lamports=200_000,
            action="paper_opened",
            complete_signal=False,
        )
        result = summarize_forward_paper(journal, 2_000_000)

    assert result["closed_positions_total"] == 6
    assert result["manual_closed_excluded"] == 1
    assert result["incomplete_closed_excluded"] == 2
    assert result["exclusion_reasons"] == {
        "missing_or_ambiguous_entry_decision": 1,
        "entry_signal_evidence_missing": 1,
    }
    assert result["autonomous"]["trades"] == 2
    assert result["autonomous"]["status"] == "limited_sample"
    assert result["autonomous"]["win_rate_pct"] == 50.0
    assert result["autonomous"]["profit_factor"] == 1.0
    assert result["autonomous"]["net_pnl_lamports"] == 0
    assert result["autonomous"]["loss_reasons"] == {"stop_loss": 1}
    assert result["autonomous"]["trades_with_bounded_mark_gaps"] == 2
    assert result["autonomous"]["max_observed_mark_drawdown_pct"] == 20.0
    assert result["call_inbox"]["trades"] == 1
    assert result["call_inbox"]["net_pnl_lamports"] == 30_000
    assert result["call_inbox"]["max_observed_mark_drawdown_pct"] is None
    assert result["promotion_ready"] is False


def test_forward_campaign_excludes_legacy_fee_bearing_route(tmp_path: Path) -> None:
    with TradeJournal(tmp_path / "forward.sqlite3") as journal:
        journal.set_runtime_state(
            "paper_campaign",
            {"mode": "forward_paper_only", "started_at": datetime.now(UTC).isoformat()},
        )
        _closed(journal, "older-order", exit_lamports=120_000, action="paper_opened")
        _closed(
            journal,
            "fee-free-build",
            exit_lamports=120_000,
            action="paper_opened",
            mark_values=(110_000,),
            quote_source="v2-build",
            revalidation=_revalidation(),
        )
        result = summarize_forward_paper(journal, 2_000_000)
    assert result["autonomous"]["trades"] == 1
    assert result["incomplete_closed_excluded"] == 1
    assert result["exclusion_reasons"]["entry_route_parity_missing"] == 1


def test_forward_campaign_excludes_complete_trade_from_before_start(tmp_path: Path) -> None:
    with TradeJournal(tmp_path / "forward.sqlite3") as journal:
        _closed(
            journal,
            "before-campaign",
            exit_lamports=120_000,
            action="paper_opened",
            quote_source="v2-build",
        )
        started_at = datetime.now(UTC).isoformat()
        journal.set_runtime_state(
            "paper_campaign", {"mode": "forward_paper_only", "started_at": started_at}
        )
        _closed(
            journal,
            "during-campaign",
            exit_lamports=80_000,
            action="paper_opened",
            mark_values=(90_000,),
            quote_source="v2-build",
            revalidation=_revalidation(),
        )
        result = summarize_forward_paper(journal, 2_000_000)

    assert result["closed_positions_total"] == 2
    assert result["campaign_started_at"] == started_at
    assert result["campaign_closed_positions_total"] == 1
    assert result["exclusion_reasons"] == {"pre_campaign_position": 1}
    assert result["autonomous"]["trades"] == 1
    assert result["autonomous"]["net_pnl_lamports"] == -20_000


def test_forward_campaign_excludes_unmonitored_closed_trade(tmp_path: Path) -> None:
    with TradeJournal(tmp_path / "forward.sqlite3") as journal:
        journal.set_runtime_state(
            "paper_campaign",
            {"mode": "forward_paper_only", "started_at": datetime.now(UTC).isoformat()},
        )
        _closed(
            journal,
            "covered",
            exit_lamports=80_000,
            action="paper_opened",
            mark_values=(90_000,),
            quote_source="v2-build",
            revalidation=_revalidation(),
        )
        _closed(
            journal,
            "unmonitored",
            exit_lamports=200_000,
            action="paper_opened",
            quote_source="v2-build",
            revalidation=_revalidation(),
        )
        result = summarize_forward_paper(journal, 2_000_000)

    assert result["closed_positions_total"] == 2
    assert result["quote_mark_history"]["closed_positions_with_bounded_mark_gaps"] == 1
    assert result["exclusion_reasons"] == {"unbounded_quote_mark_gap": 1}
    assert result["autonomous"]["trades"] == 1
    assert result["autonomous"]["net_pnl_lamports"] == -20_000


def test_forward_campaign_requires_current_final_entry_evidence(tmp_path: Path) -> None:
    with TradeJournal(tmp_path / "forward.sqlite3") as journal:
        journal.set_runtime_state(
            "paper_campaign",
            {"mode": "forward_paper_only", "started_at": datetime.now(UTC).isoformat()},
        )
        _closed(
            journal,
            "confirmed",
            exit_lamports=120_000,
            action="paper_opened",
            mark_values=(110_000,),
            quote_source="v2-build",
            revalidation=_revalidation(),
        )
        _closed(
            journal,
            "missing-final-check",
            exit_lamports=120_000,
            action="paper_opened",
            quote_source="v2-build",
        )
        stale = _revalidation()
        stale["decision_as_of"] = int(stale["decision_as_of"]) - 121
        _closed(
            journal,
            "stale-final-check",
            exit_lamports=120_000,
            action="paper_opened",
            quote_source="v2-build",
            revalidation=stale,
        )
        unconfirmed = _revalidation()
        unconfirmed["candle_signals"]["1m"]["confirmed"] = False  # type: ignore[index]
        _closed(
            journal,
            "reversed-final-check",
            exit_lamports=120_000,
            action="paper_opened",
            quote_source="v2-build",
            revalidation=unconfirmed,
        )
        unsafe = _revalidation()
        unsafe_inputs = unsafe["market_gate_inputs"]
        assert isinstance(unsafe_inputs, dict)
        unsafe_inputs["audit_status"] = "blocked"
        _closed(
            journal,
            "unsafe-final-check",
            exit_lamports=120_000,
            action="paper_opened",
            quote_source="v2-build",
            revalidation=unsafe,
        )
        result = summarize_forward_paper(journal, 2_000_000)

    assert result["autonomous"]["trades"] == 1
    assert result["exclusion_reasons"] == {"entry_revalidation_missing_or_invalid": 4}


def test_forward_campaign_flags_open_position_without_final_entry_evidence(tmp_path: Path) -> None:
    with TradeJournal(tmp_path / "forward.sqlite3") as journal:
        journal.set_runtime_state(
            "paper_campaign",
            {"mode": "forward_paper_only", "started_at": datetime.now(UTC).isoformat()},
        )
        for position_id, revalidation in (
            ("current", _revalidation()),
            ("missing-final-check", None),
        ):
            mint = str(Pubkey.new_unique())
            execution = _execution("buy", 100_000)
            execution["quote_source"] = "v2-build"
            if revalidation is not None:
                inputs = revalidation["market_gate_inputs"]
                assert isinstance(inputs, dict)
                inputs["mint"] = mint
                execution["entry_revalidation"] = revalidation
            journal.open_paper_position(
                position_id=position_id,
                mint=mint,
                entry_lamports=100_000,
                token_amount=1_000_000,
                simulated_units=100,
                starting_lamports=2_000_000,
                execution=execution,
            )
            journal.record_strategy_decision(
                _decision(mint), action="paper_opened", position_id=position_id
            )
        result = summarize_forward_paper(journal, 2_000_000)

    assert result["open_positions"] == 2
    assert result["open_positions_without_signal_evidence"] == 1


def test_forward_campaign_without_start_time_cannot_claim_trades(tmp_path: Path) -> None:
    with TradeJournal(tmp_path / "forward.sqlite3") as journal:
        journal.set_runtime_state("paper_campaign", {"mode": "forward_paper_only"})
        _closed(
            journal,
            "unknown-campaign-start",
            exit_lamports=120_000,
            action="paper_opened",
            quote_source="v2-build",
        )
        result = summarize_forward_paper(journal, 2_000_000)
    assert result["campaign_started_at"] is None
    assert result["campaign_closed_positions_total"] == 0
    assert result["exclusion_reasons"] == {"campaign_start_unverified": 1}
    assert result["autonomous"]["trades"] == 0


def test_forward_paper_requires_costed_exit(tmp_path: Path) -> None:
    with TradeJournal(tmp_path / "forward.sqlite3") as journal:
        _closed(
            journal,
            "uncosted",
            exit_lamports=150_000,
            action="paper_opened",
            costed_exit=False,
        )
        result = summarize_forward_paper(journal, 2_000_000)
    assert result["autonomous"]["trades"] == 0
    assert result["autonomous"]["win_rate_pct"] is None
    assert result["incomplete_closed_excluded"] == 1
    assert result["exclusion_reasons"] == {"exit_cost_or_reason_missing": 1}


def test_market_policy_evidence_is_required_for_forward_sample(tmp_path: Path) -> None:
    with TradeJournal(tmp_path / "forward.sqlite3") as journal:
        mint = str(Pubkey.new_unique())
        journal.open_paper_position(
            position_id="legacy-market",
            mint=mint,
            entry_lamports=100_000,
            token_amount=1_000_000,
            simulated_units=100,
            starting_lamports=2_000_000,
            execution=_execution("buy", 100_000),
        )
        decision = _decision(mint)
        decision.evidence.pop("market_policy_version")
        journal.record_strategy_decision(
            decision, action="paper_opened", position_id="legacy-market"
        )
        journal.close_paper_position(
            "legacy-market",
            120_000,
            exit_reason="take_profit",
            execution=_execution("sell", 120_000),
        )
        result = summarize_forward_paper(journal, 2_000_000)

    assert result["autonomous"]["trades"] == 0
    assert result["incomplete_closed_excluded"] == 1
    assert result["exclusion_reasons"] == {"entry_signal_evidence_missing": 1}


def test_rent_recovery_is_only_a_hypothetical_sensitivity(tmp_path: Path) -> None:
    with TradeJournal(tmp_path / "forward.sqlite3") as journal:
        _closed(
            journal,
            "rent-scenario",
            exit_lamports=100_000,
            action="paper_opened",
            ata_rent_lamports=10_000,
        )
        result = summarize_forward_paper(journal, 2_000_000)

    assert result["autonomous"]["net_pnl_lamports"] == 0
    assert result["autonomous"]["ata_rent_reserved_lamports"] == 10_000
    assert result["autonomous"]["net_pnl_if_full_ata_rent_reclaimed_lamports"] == 10_000
    assert "Hypothetical" in result["rent_recovery_sensitivity"]


def test_observed_drawdown_includes_realized_cash_after_partial_exit(tmp_path: Path) -> None:
    with TradeJournal(tmp_path / "forward.sqlite3") as journal:
        mint = str(Pubkey.new_unique())
        journal.open_paper_position(
            position_id="partial-win",
            mint=mint,
            entry_lamports=100_000,
            token_amount=1_000_000,
            simulated_units=100,
            starting_lamports=2_000_000,
            execution=_execution("buy", 100_000),
        )
        journal.record_strategy_decision(
            _decision(mint), action="paper_opened", position_id="partial-win"
        )
        journal.mark_paper_position("partial-win", 200_000, execution=_execution("mark", 200_000))
        journal.partial_close_paper_position(
            "partial-win",
            sold_token_amount=500_000,
            exit_lamports=120_000,
            advance_tp_stage=True,
            exit_reason="take_profit_1",
            execution=_execution("sell", 120_000),
        )
        journal.mark_paper_position("partial-win", 80_000, execution=_execution("mark", 80_000))
        journal.close_paper_position(
            "partial-win",
            60_000,
            exit_reason="trailing_stop",
            execution=_execution("sell", 60_000),
        )
        result = summarize_forward_paper(journal, 2_000_000)

    assert result["autonomous"]["trades"] == 1
    assert result["autonomous"]["net_pnl_lamports"] == 80_000
    assert result["autonomous"]["trades_with_bounded_mark_gaps"] == 1
    # Peak liquidation value is 200k. The partial sale leaves 120k cash,
    # then 80k of tokens; final proceeds are 180k, a 10% drawdown.
    assert result["autonomous"]["max_observed_mark_drawdown_pct"] == 10.0


def test_partial_mark_uses_inventory_when_mark_and_sale_timestamps_tie() -> None:
    same_time = "2026-11-03T08:25:39.020215+00:00"
    marks = [(same_time, 200_000, 1_000_000), (same_time, 80_000, 500_000)]
    sells = [("sell", 500_000, 120_000), ("sell", 500_000, 60_000)]
    assert _position_mark_values(marks, sells, 180_000, 1_000_000) == [
        200_000,
        200_000,
        180_000,
    ]
    assert _position_mark_values([(same_time, 80_000, 750_000)], sells, 180_000, 1_000_000) is None


def test_open_positions_without_entry_evidence_are_visible_but_not_counted(tmp_path: Path) -> None:
    with TradeJournal(tmp_path / "forward.sqlite3") as journal:
        mint = str(Pubkey.new_unique())
        for position_id, complete in (("legacy-open", False), ("current-open", True)):
            journal.open_paper_position(
                position_id=position_id,
                mint=mint,
                entry_lamports=100_000,
                token_amount=1_000_000,
                simulated_units=100,
                starting_lamports=2_000_000,
                execution=_execution("buy", 100_000),
            )
            journal.record_strategy_decision(
                _decision(mint, complete=complete),
                action="paper_opened",
                position_id=position_id,
            )
            journal.mark_paper_position(
                position_id,
                90_000,
                execution=_execution("mark", 90_000) if complete else None,
            )
        result = summarize_forward_paper(journal, 2_000_000)

    assert result["open_positions"] == 2
    assert result["open_positions_without_signal_evidence"] == 1
    assert result["autonomous"]["trades"] == 0
    assert result["quote_mark_history"] == {
        "total_marks": 2,
        "costed_marks": 1,
        "positions_with_marks": 2,
        "open_positions_with_marks": 2,
        "coverage_window_seconds": 720,
        "open_positions_with_bounded_mark_gaps": 1,
        "closed_positions_with_bounded_mark_gaps": 0,
    }


def test_mark_coverage_rejects_missing_entry_exit_and_intermediate_observations() -> None:
    opened = "2026-11-03T00:00:00+00:00"
    closed = "2026-11-03T00:15:00+00:00"
    marks = [
        "2026-11-03T00:04:00+00:00",
        "2026-11-03T00:10:00+00:00",
    ]
    assert _mark_path_covered(opened, closed, marks, window_seconds=720)
    assert not _mark_path_covered(opened, closed, marks[1:], window_seconds=540)
    assert not _mark_path_covered(opened, closed, marks[:1], window_seconds=540)
    assert not _mark_path_covered(opened, closed, [], window_seconds=720)
    assert not _mark_path_covered(
        opened,
        closed,
        ["2026-11-03T00:04:00+00:00", "2026-11-03T00:14:00+00:00"],
        window_seconds=540,
    )
