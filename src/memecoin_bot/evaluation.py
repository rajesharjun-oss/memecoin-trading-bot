from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Callable, Mapping
from math import prod
from statistics import mean
from typing import Any

from .ohlcv import TIMEFRAME_SECONDS
from .strategy_lab import Candle, FactorName, StrategyLabError, Timeframe, run_backtest


def _objective(result: dict[str, Any]) -> float:
    trades = int(result.get("trades", 0))
    if trades <= 0:
        return float("-inf")
    return float(result.get("return_pct", 0)) - float(result.get("max_drawdown_pct", 0))


def _aggregate(results: list[dict[str, Any]]) -> dict[str, Any]:
    logs = [trade for result in results for trade in result.get("trade_log", [])]
    returns = [float(trade.get("return_pct", 0)) for trade in logs]
    fold_growth = [1 + float(result.get("return_pct", 0)) / 100 for result in results]
    positive = sum(value for value in returns if value > 0)
    negative = abs(sum(value for value in returns if value < 0))
    equity = 1.0
    peak_equity = 1.0
    max_drawdown = 0.0
    for result in results:
        position_fraction = float(result.get("position_fraction_bps", 10_000)) / 10_000
        for trade in result.get("trade_log", []):
            equity *= 1 + position_fraction * float(trade["return_pct"]) / 100
            peak_equity = max(peak_equity, equity)
            max_drawdown = max(max_drawdown, (peak_equity - equity) / peak_equity)
    return {
        "folds": len(results),
        "trades": sum(int(result.get("trades", 0)) for result in results),
        "censored_trades": sum(int(result.get("censored_trades", 0)) for result in results),
        "win_rate_pct": (
            round(sum(value > 0 for value in returns) / len(returns) * 100, 2) if returns else 0.0
        ),
        "return_pct": round((prod(fold_growth) - 1) * 100, 4) if fold_growth else 0.0,
        "max_drawdown_pct": round(max_drawdown * 100, 4),
        "expectancy_pct": round(mean(returns), 4) if returns else 0.0,
        "profit_factor": round(positive / negative, 4) if negative > 0 else None,
        "trade_log": logs[-200:],
    }


def _group_performance(trades: list[dict[str, Any]], key: str) -> list[dict[str, Any]]:
    grouped: dict[str, list[float]] = defaultdict(list)
    for trade in trades:
        grouped[str(trade.get(key, "unknown"))].append(float(trade.get("return_pct", 0)))
    output: list[dict[str, Any]] = []
    for label, values in sorted(grouped.items()):
        output.append(
            {
                key: label,
                "trades": len(values),
                "win_rate_pct": round(sum(value > 0 for value in values) / len(values) * 100, 2),
                "expectancy_pct": round(mean(values), 4),
            }
        )
    return output


def _score_calibration(trades: list[dict[str, Any]]) -> list[dict[str, Any]]:
    buckets: dict[str, list[float]] = defaultdict(list)
    for trade in trades:
        score = int(trade.get("signal_score", 0))
        lower = min(90, score // 10 * 10)
        label = "90-100" if lower >= 90 else f"{lower}-{lower + 9}"
        buckets[label].append(float(trade.get("return_pct", 0)))
    return [
        {
            "score_bucket": label,
            "trades": len(values),
            "win_rate_pct": round(sum(value > 0 for value in values) / len(values) * 100, 2),
            "expectancy_pct": round(mean(values), 4),
        }
        for label, values in sorted(buckets.items())
    ]


def run_walk_forward_evaluation(
    candles: list[Candle],
    *,
    timeframe: Timeframe = "1m",
    minimum_liquidity_usd: float,
    tp_levels_bps: tuple[int, ...],
    tp_sell_bps: tuple[int, ...],
    stop_loss_bps: int,
    execution_cost_bps: int,
    fixed_cost_bps: int = 0,
    refundable_ata_reserve_bps: int = 0,
    additional_exit_fee_bps: int = 0,
    position_fraction_bps: int = 100,
    trailing_activation_bps: int = 3_000,
    trailing_stop_bps: int = 1_500,
    max_hold_seconds: int = 86_400,
    minimum_exit_liquidity_usd: float = 15_000,
    max_consecutive_losses: int = 3,
    max_drawdown_bps: int = 1_000,
    daily_loss_bps: int = 500,
    entry_not_before: int | None = None,
    entry_windows: tuple[tuple[int, int], ...] | None = None,
    closed_paper_positions: int = 0,
    minimum_train_candles: int = 24,
    test_candles: int = 12,
    confirmation_frames: Mapping[Timeframe, list[Candle]] | None = None,
    audit_observations: list[dict[str, object]] | None = None,
    market_snapshots: list[dict[str, object]] | None = None,
    minimum_volume_24h_usd: float = 0,
    expected_pool: str | None = None,
    market_score_replayer: (
        Callable[[dict[str, object], int], tuple[str, int | None] | None] | None
    ) = None,
    paper_poll_seconds: int = 300,
) -> dict[str, Any]:
    if not 0 <= refundable_ata_reserve_bps <= fixed_cost_bps <= execution_cost_bps <= 5_000:
        raise StrategyLabError("Walk-forward replay costs are inconsistent")
    entry_model = (
        "multi_timeframe_candle_gate"
        if confirmation_frames is not None
        else "single_frame_research"
    )
    parity_flags = {
        "historical_market_gate_parity": False,
        "paper_entry_schedule_parity": False,
    }
    if entry_windows == ():
        return {
            **parity_flags,
            "status": "outside_research_cohort",
            "reason": "No point-in-time membership in the selected research cohort",
            "trades": 0,
            "folds": [],
            "promotion_ready": False,
            "entry_model": entry_model,
        }

    def frames_closed_by(cutoff: int) -> dict[Timeframe, list[Candle]] | None:
        if confirmation_frames is None:
            return None
        return {
            frame: [
                candle for candle in rows if candle.opened_at + TIMEFRAME_SECONDS[frame] <= cutoff
            ]
            for frame, rows in confirmation_frames.items()
        }

    def audits_recorded_by(cutoff: int) -> list[dict[str, object]] | None:
        if audit_observations is None:
            return None
        selected: list[dict[str, object]] = []
        for item in audit_observations:
            observed_at_us = item.get("observed_at_us")
            if isinstance(observed_at_us, int) and observed_at_us <= cutoff * 1_000_000:
                selected.append(item)
        return selected

    def markets_recorded_by(cutoff: int) -> list[dict[str, object]] | None:
        if market_snapshots is None:
            return None
        return [
            item
            for item in market_snapshots
            if type(item.get("captured_at")) is int and int(str(item["captured_at"])) <= cutoff
        ]

    minimum_required = minimum_train_candles + test_candles
    if len(candles) < minimum_required:
        return {
            **parity_flags,
            "status": "insufficient_history",
            "minimum_candles": minimum_required,
            "available_candles": len(candles),
            "methodology": "anchored walk-forward; parameters selected on train only",
            "folds": [],
            "promotion_ready": False,
            "entry_model": entry_model,
        }
    if any(candle.source != "geckoterminal_pool_trades" for candle in candles):
        return {
            **parity_flags,
            "status": "unverified_data",
            "reason": "Walk-forward evaluation requires authentic pool OHLCV",
            "available_candles": len(candles),
            "folds": [],
            "promotion_ready": False,
            "entry_model": entry_model,
        }
    if confirmation_frames is not None and any(
        candle.source != "geckoterminal_pool_trades"
        for rows in confirmation_frames.values()
        for candle in rows
    ):
        return {
            **parity_flags,
            "status": "unverified_data",
            "reason": "Confirmation frames require authentic pool OHLCV",
            "folds": [],
            "promotion_ready": False,
            "entry_model": entry_model,
        }
    if confirmation_frames is not None and len(confirmation_frames.get("5m", [])) < 8:
        return {
            **parity_flags,
            "status": "insufficient_confirmation_history",
            "reason": "Need eight authentic closed 5m candles",
            "folds": [],
            "promotion_ready": False,
            "entry_model": entry_model,
        }
    if audit_observations is not None and not audit_observations:
        return {
            **parity_flags,
            "status": "insufficient_audit_history",
            "reason": "No timestamped audit observations have been recorded",
            "folds": [],
            "promotion_ready": False,
            "entry_model": entry_model,
        }

    threshold_candidates = (70, 75, 80, 90)
    stop_candidates = tuple(
        sorted({max(500, stop_loss_bps - 1_000), stop_loss_bps, min(9_000, stop_loss_bps + 1_000)})
    )
    folds: list[dict[str, Any]] = []
    out_of_sample: list[dict[str, Any]] = []
    stressed_out_of_sample: list[dict[str, Any]] = []
    ablation_factors: tuple[FactorName, ...] = ("trend", "momentum", "volume", "liquidity")
    ablation_folds: dict[str, list[dict[str, Any]]] = {
        "full_model": [],
        **{f"without_{factor}": [] for factor in ablation_factors},
    }
    train_end = minimum_train_candles
    fold_number = 1
    # The cost stress applies to execution friction, not refundable capital.
    stressed_nonrent = min(
        5_000 - refundable_ata_reserve_bps,
        round((execution_cost_bps - refundable_ata_reserve_bps) * 1.5),
    )
    stressed_cost_bps = refundable_ata_reserve_bps + stressed_nonrent
    stressed_fixed_bps = min(
        stressed_cost_bps,
        refundable_ata_reserve_bps + round((fixed_cost_bps - refundable_ata_reserve_bps) * 1.5),
    )
    while train_end + test_candles <= len(candles):
        train = candles[:train_end]
        candidates: list[tuple[float, int, int, dict[str, Any]]] = []
        for threshold in threshold_candidates:
            for candidate_stop in stop_candidates:
                result = run_backtest(
                    train,
                    exit_model="paper_polling_proxy",
                    paper_poll_seconds=paper_poll_seconds,
                    timeframe=timeframe,
                    minimum_liquidity_usd=minimum_liquidity_usd,
                    tp_levels_bps=tp_levels_bps,
                    tp_sell_bps=tp_sell_bps,
                    stop_loss_bps=candidate_stop,
                    signal_threshold=threshold,
                    execution_cost_bps=execution_cost_bps,
                    fixed_cost_bps=fixed_cost_bps,
                    refundable_ata_reserve_bps=refundable_ata_reserve_bps,
                    additional_exit_fee_bps=additional_exit_fee_bps,
                    position_fraction_bps=position_fraction_bps,
                    trailing_activation_bps=trailing_activation_bps,
                    trailing_stop_bps=trailing_stop_bps,
                    max_hold_seconds=max_hold_seconds,
                    minimum_exit_liquidity_usd=minimum_exit_liquidity_usd,
                    max_consecutive_losses=max_consecutive_losses,
                    max_drawdown_bps=max_drawdown_bps,
                    daily_loss_bps=daily_loss_bps,
                    entry_not_before=entry_not_before,
                    entry_windows=entry_windows,
                    confirmation_frames=frames_closed_by(
                        train[-1].opened_at + TIMEFRAME_SECONDS[timeframe]
                    ),
                    audit_observations=audits_recorded_by(
                        train[-1].opened_at + TIMEFRAME_SECONDS[timeframe]
                    ),
                    market_snapshots=markets_recorded_by(
                        train[-1].opened_at + TIMEFRAME_SECONDS[timeframe]
                    ),
                    minimum_volume_24h_usd=minimum_volume_24h_usd,
                    expected_pool=expected_pool,
                    market_score_replayer=market_score_replayer,
                )
                candidates.append((_objective(result), threshold, candidate_stop, result))
        candidates.sort(key=lambda item: (item[0], -item[1], -item[2]), reverse=True)
        _score, selected_threshold, selected_stop, train_result = candidates[0]

        context_start = max(0, train_end - 8)
        test_end = train_end + test_candles
        replay = candles[context_start:test_end]
        entry_start = train_end - context_start
        test_cutoff = candles[test_end - 1].opened_at + TIMEFRAME_SECONDS[timeframe]
        test_frames = frames_closed_by(test_cutoff)
        test_audits = audits_recorded_by(test_cutoff)
        test_markets = markets_recorded_by(test_cutoff)

        def evaluate_fold(
            fold_candles: list[Candle],
            *,
            fold_stop_bps: int,
            fold_signal_threshold: int,
            fold_entry_start: int,
            fold_frames: Mapping[Timeframe, list[Candle]] | None,
            fold_audits: list[dict[str, object]] | None,
            fold_markets: list[dict[str, object]] | None,
            cost_bps: int,
            fixed_bps: int,
            rent_bps: int,
            ata_reserved: bool,
            exit_fee_bps: int,
            disabled: frozenset[FactorName],
        ) -> dict[str, Any]:
            return run_backtest(
                fold_candles,
                exit_model="paper_polling_proxy",
                paper_poll_seconds=paper_poll_seconds,
                timeframe=timeframe,
                minimum_liquidity_usd=minimum_liquidity_usd,
                tp_levels_bps=tp_levels_bps,
                tp_sell_bps=tp_sell_bps,
                stop_loss_bps=fold_stop_bps,
                signal_threshold=fold_signal_threshold,
                execution_cost_bps=cost_bps,
                fixed_cost_bps=fixed_bps,
                refundable_ata_reserve_bps=rent_bps,
                ata_already_reserved=ata_reserved,
                additional_exit_fee_bps=exit_fee_bps,
                position_fraction_bps=position_fraction_bps,
                trailing_activation_bps=trailing_activation_bps,
                trailing_stop_bps=trailing_stop_bps,
                max_hold_seconds=max_hold_seconds,
                minimum_exit_liquidity_usd=minimum_exit_liquidity_usd,
                max_consecutive_losses=max_consecutive_losses,
                max_drawdown_bps=max_drawdown_bps,
                daily_loss_bps=daily_loss_bps,
                entry_not_before=entry_not_before,
                entry_windows=entry_windows,
                entry_start_index=fold_entry_start,
                disabled_factors=disabled,
                confirmation_frames=fold_frames,
                audit_observations=fold_audits,
                market_snapshots=fold_markets,
                minimum_volume_24h_usd=minimum_volume_24h_usd,
                expected_pool=expected_pool,
                market_score_replayer=market_score_replayer,
            )

        test_result = evaluate_fold(
            replay,
            fold_stop_bps=selected_stop,
            fold_signal_threshold=selected_threshold,
            fold_entry_start=entry_start,
            fold_frames=test_frames,
            fold_audits=test_audits,
            fold_markets=test_markets,
            cost_bps=execution_cost_bps,
            fixed_bps=fixed_cost_bps,
            rent_bps=refundable_ata_reserve_bps,
            ata_reserved=any(result.get("trades", 0) for result in out_of_sample),
            exit_fee_bps=additional_exit_fee_bps,
            disabled=frozenset(),
        )
        stressed_result = evaluate_fold(
            replay,
            fold_stop_bps=selected_stop,
            fold_signal_threshold=selected_threshold,
            fold_entry_start=entry_start,
            fold_frames=test_frames,
            fold_audits=test_audits,
            fold_markets=test_markets,
            cost_bps=stressed_cost_bps,
            fixed_bps=stressed_fixed_bps,
            rent_bps=refundable_ata_reserve_bps,
            ata_reserved=any(result.get("trades", 0) for result in stressed_out_of_sample),
            exit_fee_bps=min(5_000, round(additional_exit_fee_bps * 1.5)),
            disabled=frozenset(),
        )
        out_of_sample.append(test_result)
        stressed_out_of_sample.append(stressed_result)
        ablation_folds["full_model"].append(test_result)
        for factor in ablation_factors:
            ablation_folds[f"without_{factor}"].append(
                evaluate_fold(
                    replay,
                    fold_stop_bps=selected_stop,
                    fold_signal_threshold=selected_threshold,
                    fold_entry_start=entry_start,
                    fold_frames=test_frames,
                    fold_audits=test_audits,
                    fold_markets=test_markets,
                    cost_bps=execution_cost_bps,
                    fixed_bps=fixed_cost_bps,
                    rent_bps=refundable_ata_reserve_bps,
                    ata_reserved=any(
                        result.get("trades", 0) for result in ablation_folds[f"without_{factor}"]
                    ),
                    exit_fee_bps=additional_exit_fee_bps,
                    disabled=frozenset({factor}),
                )
            )
        folds.append(
            {
                "fold": fold_number,
                "train_start_at": train[0].opened_at,
                "train_end_at": train[-1].opened_at,
                "test_start_at": candles[train_end].opened_at,
                "test_end_at": candles[test_end - 1].opened_at,
                "frozen_parameters": {
                    "signal_threshold": selected_threshold,
                    "stop_loss_bps": selected_stop,
                    "execution_cost_bps": execution_cost_bps,
                    "fixed_cost_bps": fixed_cost_bps,
                    "refundable_ata_reserve_bps": refundable_ata_reserve_bps,
                },
                "train": {key: value for key, value in train_result.items() if key != "trade_log"},
                "out_of_sample": {
                    key: value for key, value in test_result.items() if key != "trade_log"
                },
            }
        )
        train_end += test_candles
        fold_number += 1

    summary = _aggregate(out_of_sample)
    stressed = _aggregate(stressed_out_of_sample)
    trades = summary["trade_log"]
    fold_statuses = Counter(str(result.get("status")) for result in out_of_sample)
    coverage_complete = bool(out_of_sample) and fold_statuses == {"complete": len(out_of_sample)}

    ablations: list[dict[str, Any]] = []
    for variant, results in ablation_folds.items():
        result = _aggregate(results)
        ablations.append(
            {
                "variant": variant,
                "scope": "frozen_out_of_sample_folds",
                "status": "closed_trades_observed" if result["trades"] else "no_closed_trades",
                "folds": result["folds"],
                "trades": result["trades"],
                "return_pct": result["return_pct"],
                "expectancy_pct": result["expectancy_pct"],
                "max_drawdown_pct": result["max_drawdown_pct"],
                "profit_factor": result["profit_factor"],
                "censored_trades": result["censored_trades"],
            }
        )

    profit_factor = summary.get("profit_factor")
    if out_of_sample and all(
        result.get("status") == "insufficient_market_history" for result in out_of_sample
    ):
        evaluation_status = "insufficient_market_history"
    elif out_of_sample and all(
        result.get("status")
        in {"insufficient_candle_availability", "insufficient_confirmation_history"}
        for result in out_of_sample
    ):
        evaluation_status = "insufficient_candle_availability"
    elif not coverage_complete:
        evaluation_status = "partial_out_of_sample_coverage"
    elif not any(int(fold["train"].get("trades", 0)) > 0 for fold in folds):
        evaluation_status = "insufficient_training_trades"
    elif summary["trades"] == 0:
        evaluation_status = "no_closed_out_of_sample_trades"
    else:
        evaluation_status = "complete"
    evaluation_reason = {
        "insufficient_candle_availability": "Unseen folds lack timely observed candle history",
        "partial_out_of_sample_coverage": (
            "At least one unseen fold lacks the point-in-time inputs needed for evaluation"
        ),
        "insufficient_training_trades": "No closed training trades support parameter selection",
        "no_closed_out_of_sample_trades": "No closed trades in the unseen test windows",
    }.get(evaluation_status)
    gates = {
        "positive_expectancy": float(summary["expectancy_pct"]) > 0,
        "profit_factor_1_25": profit_factor is not None and float(profit_factor) >= 1.25,
        "positive_cost_stress": float(stressed["return_pct"]) > 0,
        "minimum_200_closed_positions": closed_paper_positions >= 200,
        "no_censored_out_of_sample_positions": summary["censored_trades"] == 0,
        "unbiased_multi_token_universe": False,
        "point_in_time_safety_audit": False,
        "paper_exit_model_parity": False,
        "historical_market_gate_parity": False,
        "paper_entry_schedule_parity": False,
    }
    return {
        **parity_flags,
        "status": evaluation_status,
        "reason": evaluation_reason,
        "available_candles": len(candles),
        "out_of_sample_fold_statuses": dict(fold_statuses),
        "out_of_sample_coverage_complete": coverage_complete,
        "trades": summary["trades"],
        "win_rate_pct": summary["win_rate_pct"],
        "return_pct": summary["return_pct"],
        "max_drawdown_pct": summary["max_drawdown_pct"],
        "expectancy_pct": summary["expectancy_pct"],
        "profit_factor": summary["profit_factor"],
        "methodology": (
            "anchored walk-forward; threshold and stop selected on train only, then frozen for "
            "the next unseen window"
        ),
        "lookahead_protection": False,
        "entry_data_point_in_time": True,
        "exit_execution_model": "closed_candle_poll_next_open_proxy",
        "paper_poll_seconds": paper_poll_seconds,
        "paper_exit_rule_parity": True,
        "exit_observation_bound_seconds": 120,
        "base_execution_cost_bps": execution_cost_bps,
        "base_fixed_cost_bps": fixed_cost_bps,
        "base_refundable_ata_reserve_bps": refundable_ata_reserve_bps,
        "stressed_execution_cost_bps": stressed_cost_bps,
        "stressed_fixed_cost_bps": stressed_fixed_bps,
        "stressed_refundable_ata_reserve_bps": refundable_ata_reserve_bps,
        "additional_exit_fee_bps": additional_exit_fee_bps,
        "closed_paper_positions": closed_paper_positions,
        "universe_scope": "single_current_discovery_candidate",
        "entry_model": entry_model,
        "folds": folds,
        "out_of_sample": summary,
        "cost_stress_150pct": stressed,
        "regimes": _group_performance(trades, "regime"),
        "score_calibration": _score_calibration(trades),
        "ablations": ablations,
        "validation_gates": gates,
        "promotion_ready": all(gates.values()),
    }
