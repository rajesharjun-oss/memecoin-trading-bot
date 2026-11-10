from __future__ import annotations

from dataclasses import replace
from typing import Any

from memecoin_bot.evaluation import _aggregate, run_walk_forward_evaluation
from memecoin_bot.strategy_lab import Candle


def _candles(count: int = 60) -> list[Candle]:
    output: list[Candle] = []
    price = 1.0
    for index in range(count):
        price *= 1.018 if index % 10 != 9 else 0.94
        output.append(
            Candle(
                opened_at=1_700_000_000 + index * 60,
                open=price * 0.995,
                high=price * 1.04,
                low=price * 0.98,
                close=price,
                volume_delta_usd=100 + index * 40,
                liquidity_usd=75_000,
                samples=1,
                source="geckoterminal_pool_trades",
                first_fetched_at=1_700_000_000 + (index + 1) * 60 + 1,
                first_observed_values_at=1_700_000_000 + (index + 1) * 60 + 1,
            )
        )
    return output


def _evaluate(
    candles: list[Candle], *, audit_observations: list[dict[str, object]] | None = None
) -> dict[str, Any]:
    return run_walk_forward_evaluation(
        candles,
        minimum_liquidity_usd=25_000,
        tp_levels_bps=(2_000, 5_000),
        tp_sell_bps=(5_000, 5_000),
        stop_loss_bps=2_500,
        execution_cost_bps=100,
        audit_observations=audit_observations,
    )


def test_walk_forward_requires_history() -> None:
    result = _evaluate(_candles(20))
    assert result["status"] == "insufficient_history"
    assert result["minimum_candles"] == 36
    assert result["promotion_ready"] is False
    assert result["historical_market_gate_parity"] is False
    assert result["paper_entry_schedule_parity"] is False


def test_cost_stress_does_not_inflate_refundable_rent() -> None:
    result = run_walk_forward_evaluation(
        _candles(),
        minimum_liquidity_usd=25_000,
        tp_levels_bps=(2_000, 5_000),
        tp_sell_bps=(5_000, 5_000),
        stop_loss_bps=2_500,
        execution_cost_bps=3_000,
        fixed_cost_bps=2_900,
        refundable_ata_reserve_bps=2_500,
    )
    assert result["base_refundable_ata_reserve_bps"] == 2_500
    assert result["stressed_refundable_ata_reserve_bps"] == 2_500
    assert result["stressed_execution_cost_bps"] == 3_250
    assert result["stressed_fixed_cost_bps"] == 3_100


def test_walk_forward_rejects_token_without_cohort_membership() -> None:
    result = run_walk_forward_evaluation(
        _candles(),
        minimum_liquidity_usd=25_000,
        tp_levels_bps=(2_000, 5_000),
        tp_sell_bps=(5_000, 5_000),
        stop_loss_bps=2_500,
        execution_cost_bps=100,
        entry_windows=(),
    )
    assert result["status"] == "outside_research_cohort"
    assert result["trades"] == 0
    assert result["promotion_ready"] is False


def test_walk_forward_discloses_unobserved_candles() -> None:
    candles = _candles(60)
    unavailable = [
        replace(candle, first_fetched_at=candles[-1].opened_at + 120) for candle in candles
    ]
    result = _evaluate(unavailable)
    assert result["status"] == "insufficient_candle_availability"
    assert result["trades"] == 0
    assert result["promotion_ready"] is False


def test_walk_forward_discloses_missing_point_in_time_audits() -> None:
    result = _evaluate(_candles(), audit_observations=[])
    assert result["status"] == "insufficient_audit_history"
    assert result["promotion_ready"] is False


def test_walk_forward_fails_closed_without_historical_market_snapshots() -> None:
    result = run_walk_forward_evaluation(
        _candles(),
        minimum_liquidity_usd=25_000,
        minimum_volume_24h_usd=50_000,
        market_snapshots=[],
        expected_pool="pool",
        tp_levels_bps=(2_000, 5_000),
        tp_sell_bps=(5_000, 5_000),
        stop_loss_bps=2_500,
        execution_cost_bps=100,
    )
    assert result["status"] == "insufficient_market_history"
    assert result["promotion_ready"] is False
    assert result["out_of_sample"]["trades"] == 0
    assert all(
        fold["out_of_sample"]["status"] == "insufficient_market_history" for fold in result["folds"]
    )


def test_walk_forward_marks_mixed_market_history_as_partial_coverage() -> None:
    candles = _candles()
    result = run_walk_forward_evaluation(
        candles,
        minimum_liquidity_usd=25_000,
        minimum_volume_24h_usd=50_000,
        market_snapshots=[
            {
                "captured_at": candles[40].opened_at,
                "pool": "pool",
                "liquidity_usd": "75000",
                "volume_24h_usd": "100000",
                "price_change_24h": "0",
                "pair_created_at": candles[0].opened_at - 3_600,
            }
        ],
        expected_pool="pool",
        tp_levels_bps=(2_000, 5_000),
        tp_sell_bps=(5_000, 5_000),
        stop_loss_bps=2_500,
        execution_cost_bps=100,
    )
    assert result["out_of_sample_fold_statuses"]["insufficient_market_history"] >= 1
    assert result["out_of_sample_fold_statuses"]["complete"] >= 1
    assert result["status"] == "partial_out_of_sample_coverage"
    assert result["out_of_sample_coverage_complete"] is False
    assert result["promotion_ready"] is False


def test_walk_forward_freezes_parameters_before_unseen_window() -> None:
    candles = _candles(60)
    baseline = _evaluate(candles)
    changed = list(candles)
    for index in range(24, 36):
        changed[index] = replace(
            changed[index],
            high=changed[index].high * 4,
            low=changed[index].low * 0.2,
            close=changed[index].close * 2,
        )
    mutated = _evaluate(changed)

    assert baseline["status"] == "insufficient_training_trades"
    assert baseline["reason"] == "No closed training trades support parameter selection"
    assert baseline["lookahead_protection"] is False
    assert baseline["entry_data_point_in_time"] is True
    assert baseline["validation_gates"]["paper_exit_model_parity"] is False
    assert baseline["folds"][0]["frozen_parameters"] == mutated["folds"][0]["frozen_parameters"]
    assert "cost_stress_150pct" in baseline
    assert len(baseline["ablations"]) == 5
    assert all(
        item["scope"] == "frozen_out_of_sample_folds" and item["folds"] == len(baseline["folds"])
        for item in baseline["ablations"]
    )
    full_model = baseline["ablations"][0]
    assert full_model["variant"] == "full_model"
    assert full_model["trades"] == baseline["out_of_sample"]["trades"]
    assert full_model["return_pct"] == baseline["out_of_sample"]["return_pct"]
    assert full_model["censored_trades"] == baseline["out_of_sample"]["censored_trades"]
    assert all(item["status"] == "no_closed_trades" for item in baseline["ablations"])


def test_walk_forward_reports_validation_gates_and_regimes() -> None:
    result = _evaluate(_candles())
    gates = result["validation_gates"]
    assert set(gates) == {
        "positive_expectancy",
        "profit_factor_1_25",
        "positive_cost_stress",
        "minimum_200_closed_positions",
        "no_censored_out_of_sample_positions",
        "unbiased_multi_token_universe",
        "point_in_time_safety_audit",
        "paper_exit_model_parity",
        "historical_market_gate_parity",
        "paper_entry_schedule_parity",
    }
    assert gates["no_censored_out_of_sample_positions"] == (
        result["out_of_sample"]["censored_trades"] == 0
    )
    assert result["promotion_ready"] is False
    assert gates["minimum_200_closed_positions"] is False
    assert gates["unbiased_multi_token_universe"] is False
    assert gates["historical_market_gate_parity"] is False
    assert gates["paper_entry_schedule_parity"] is False
    assert isinstance(result["regimes"], list)


def test_out_of_sample_drawdown_accumulates_across_folds() -> None:
    folds = [
        {
            "trades": 1,
            "trade_log": [{"return_pct": -5.0}],
            "return_pct": -5.0,
            "position_fraction_bps": 10_000,
        },
        {
            "trades": 1,
            "trade_log": [{"return_pct": -5.0}],
            "return_pct": -5.0,
            "position_fraction_bps": 10_000,
        },
    ]
    result = _aggregate(folds)
    assert result["return_pct"] == -9.75
    assert result["max_drawdown_pct"] == 9.75
    assert result["trades"] == len(result["trade_log"]) == 2
