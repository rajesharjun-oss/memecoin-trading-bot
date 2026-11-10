"""Read-only evidence report for the current paper research database.

Run with ``python -m memecoin_bot.verification`` from the project directory.
It never creates paper fills or submits a transaction.
"""

from __future__ import annotations

import json
from collections import Counter, defaultdict
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from itertools import pairwise
from math import isfinite
from statistics import median
from typing import Any, cast

from .config import Settings
from .constants import LIVE_EXECUTION_LOCKED, MARKET_POLICY_VERSION, MAX_ABSOLUTE_24H_CHANGE_PCT
from .execution import (
    estimated_additional_exit_fee_bps,
    estimated_paper_swap_fraction_bps,
    estimated_replay_costs,
    estimated_round_trip_cost_bps,
)
from .forward_performance import summarize_forward_paper
from .journal import TradeJournal
from .ohlcv import TIMEFRAME_SECONDS
from .operations import summarize_ohlcv_collection_capacity
from .opportunity import (
    summarize_decision_followup_paths,
    summarize_recent_price_paths,
    summarize_snapshot_decision_paths,
)
from .strategy import (
    confirm_candidate_candles,
    evaluate_candidate,
    make_replay_market_scorer,
    primary_decision_reason,
)
from .strategy_lab import (
    CANDLE_PROVENANCE_VERSION,
    DECISION_FOLLOWUP_METHOD,
    DEFAULT_SIGNAL_THRESHOLD,
    RESEARCH_COHORT_KEY,
    RESEARCH_SELECTION_METHOD,
    Timeframe,
    analyze_entry,
    load_verified_candles,
    multi_frame_entry_verdict,
    research_cohort_mints,
    research_focus_size,
    run_backtest,
    timeframe_overlap_diagnostic,
)


def summarize_market_eligible_followup_replay(
    journal: TradeJournal, settings: Settings, *, as_of: int | None = None
) -> dict[str, Any]:
    """Replay a prospective market-eligible sample separately from random launches."""
    observed_at = int(datetime.now(UTC).timestamp()) if as_of is None else as_of
    selections = journal.decision_followup_selections(limit=20)
    selected = [
        item for item in selections if item.get("selection_method") == DECISION_FOLLOWUP_METHOD
    ]
    output: list[dict[str, Any]] = []
    for selection in reversed(selected):
        mint = str(selection["mint"])
        selected_at = int(str(selection["selected_at"]))
        expires_at = int(str(selection["expires_at"]))
        row: dict[str, Any] = {
            "mint": mint,
            "selected_at": selected_at,
            "expires_at": expires_at,
            "selection_window_complete": observed_at >= expires_at,
            "selection_population_count": int(str(selection["population_count"])),
            "trades": 0,
            "censored_trades": 0,
            "historical_market_gate_parity": False,
            "paper_entry_schedule_parity": False,
        }
        selected_market = journal.market_snapshot_before(mint, captured_at=selected_at)
        pool = selected_market.get("pool") if selected_market else None
        captured_at = selected_market.get("captured_at") if selected_market else None
        if (
            not isinstance(pool, str)
            or not pool
            or type(captured_at) is not int
            or not 0 <= selected_at - captured_at <= 180
        ):
            row["status"] = "missing_selection_market"
            output.append(row)
            continue
        row["pool"] = pool
        candles = load_verified_candles(journal, mint, pool, "1m")
        if not candles:
            row["status"] = "missing_1m_history"
            output.append(row)
            continue
        replay_costs = estimated_replay_costs(settings)
        result = run_backtest(
            candles,
            exit_model="paper_polling_proxy",
            paper_poll_seconds=settings.paper_exit_interval_seconds,
            minimum_liquidity_usd=float(settings.strategy_min_liquidity_usd),
            tp_levels_bps=settings.paper_tp_levels_bps,
            tp_sell_bps=settings.paper_tp_sell_bps,
            stop_loss_bps=settings.paper_stop_loss_bps,
            execution_cost_bps=estimated_round_trip_cost_bps(settings),
            fixed_cost_bps=replay_costs.fixed_cost_bps,
            refundable_ata_reserve_bps=replay_costs.refundable_ata_reserve_bps,
            additional_exit_fee_bps=estimated_additional_exit_fee_bps(settings),
            position_fraction_bps=estimated_paper_swap_fraction_bps(settings),
            trailing_activation_bps=settings.paper_trailing_activation_bps,
            trailing_stop_bps=settings.paper_trailing_stop_bps,
            max_hold_seconds=settings.paper_max_hold_seconds,
            minimum_exit_liquidity_usd=float(settings.paper_liquidity_breaker_usd),
            max_consecutive_losses=settings.paper_max_consecutive_losses,
            max_drawdown_bps=settings.paper_max_drawdown_bps,
            daily_loss_bps=settings.paper_daily_loss_bps,
            entry_not_before=selected_at,
            entry_windows=((selected_at, expires_at),),
            confirmation_frames={
                frame: load_verified_candles(journal, mint, pool, frame)
                for frame in ("5m", "15m", "1h")
            },
            audit_observations=journal.audit_observations(mint),
            market_snapshots=journal.market_snapshots(mint),
            minimum_volume_24h_usd=float(settings.strategy_min_volume_24h_usd),
            expected_pool=pool,
            market_score_replayer=make_replay_market_scorer(journal, settings, mint),
        )
        trades = int(result["trades"])
        censored = int(result.get("censored_trades", 0))
        status = str(result["status"])
        if status == "complete" and censored:
            status = "partial_closed_trades" if trades else "censored_unclosed"
        if status == "complete" and observed_at < expires_at:
            status = "collecting"
        row.update(
            {
                "status": status,
                "available_1m_candles": len(candles),
                "trades": trades,
                "censored_trades": censored,
                "censored_reasons": dict(
                    Counter(str(item["reason"]) for item in result.get("censored_log", []))
                ),
                "entry_decisions_in_window": result.get("entry_decisions_in_window"),
                "entry_rejection_counts": result.get("entry_rejection_counts", {}),
                "historical_market_gate_parity": result.get("historical_market_gate_parity", False),
                "paper_entry_schedule_parity": result.get("paper_entry_schedule_parity", False),
                "win_rate_pct": result.get("win_rate_pct") if trades else None,
                "profit_factor": result.get("profit_factor") if trades else None,
                "net_return_pct": result.get("return_pct") if trades else None,
                "max_drawdown_pct": result.get("max_drawdown_pct") if trades else None,
                "expectancy_pct": result.get("expectancy_pct") if trades else None,
                "average_holding_seconds": result.get("average_holding_seconds")
                if trades
                else None,
                "full_rent_recovery_sensitivity": (
                    result.get("full_rent_recovery_sensitivity") if trades else None
                ),
                "loss_reasons": dict(
                    Counter(
                        str(trade["exit_reason"])
                        for trade in result.get("trade_log", [])
                        if float(trade["return_pct"]) < 0
                    )
                ),
            }
        )
        output.append(row)
    return {
        "scope": "latest_20_prospectively_selected_market_eligible_subjects",
        "selection_method": DECISION_FOLLOWUP_METHOD,
        "selection_records": len(selected),
        "selection_windows_complete": sum(bool(row["selection_window_complete"]) for row in output),
        "selection_windows_collecting": sum(not row["selection_window_complete"] for row in output),
        "older_method_records_excluded": len(selections) - len(selected),
        "closed_trades": sum(int(row["trades"]) for row in output),
        "censored_trades": sum(int(row["censored_trades"]) for row in output),
        "statuses": dict(Counter(str(row["status"]) for row in output)),
        "results": output,
        "limitation": (
            "Conditional market-eligible sample, not all discovered launches; "
            "historical execution and paper entry scheduling are not at parity"
        ),
    }


def _entry_cutoff(value: object) -> int | None:
    if not isinstance(value, str):
        return None
    try:
        discovered = datetime.fromisoformat(value)
    except ValueError:
        return None
    if discovered.tzinfo is None:
        return None
    return int(discovered.timestamp()) + 1


def verify_paper_entry_signal(
    journal: TradeJournal,
    decision: dict[str, object],
    *,
    default_minimum_liquidity_usd: float,
) -> dict[str, object]:
    """Replay candle values known at a saved entry or observed decision time."""
    evidence = decision.get("evidence")
    mint = decision.get("mint")
    if not isinstance(evidence, dict) or not isinstance(mint, str):
        return {"status": "unverifiable", "reason": "entry_evidence_missing"}
    expected_verdict = decision.get("verdict", "eligible")
    if not isinstance(expected_verdict, str) or expected_verdict not in {"eligible", "observe"}:
        return {"status": "unverifiable", "reason": "entry_verdict_invalid"}
    decision_at = evidence.get("decision_as_of")
    pool = evidence.get("pool")
    recorded = evidence.get("candle_signals")
    latest_recorded = evidence.get("latest_closed_candle_at")
    frames: tuple[Timeframe, ...] = ("1m", "5m", "15m", "1h")
    if (
        type(decision_at) is not int
        or decision_at <= 0
        or not isinstance(pool, str)
        or not pool
        or not isinstance(recorded, dict)
        or not isinstance(latest_recorded, dict)
        or any(not isinstance(recorded.get(frame), dict) for frame in frames)
    ):
        return {"status": "unverifiable", "reason": "entry_signal_evidence_incomplete"}
    policy = evidence.get("candle_policy")
    policy_source = "recorded" if isinstance(policy, dict) else "current_settings_fallback"
    raw_liquidity = (
        policy.get("minimum_liquidity_usd")
        if isinstance(policy, dict)
        else default_minimum_liquidity_usd
    )
    raw_threshold = (
        policy.get("signal_threshold") if isinstance(policy, dict) else DEFAULT_SIGNAL_THRESHOLD
    )
    try:
        minimum_liquidity = Decimal(str(raw_liquidity))
    except (InvalidOperation, ValueError):
        return {"status": "unverifiable", "reason": "entry_policy_invalid"}
    if (
        not minimum_liquidity.is_finite()
        or minimum_liquidity <= 0
        or not isfinite(float(minimum_liquidity))
        or type(raw_threshold) is not int
        or not 1 <= raw_threshold <= 100
    ):
        return {"status": "unverifiable", "reason": "entry_policy_invalid"}
    signals = {}
    known_counts: dict[str, int] = {}
    latest_matches: dict[str, bool] = {}
    frame_matches: dict[str, bool] = {}
    for frame in frames:
        period = TIMEFRAME_SECONDS[frame]
        known = [
            candle
            for candle in load_verified_candles(journal, mint, pool, frame, now=decision_at)
            if type(candle.first_fetched_at) is int
            and type(candle.first_observed_values_at) is int
            and candle.opened_at + period <= candle.first_fetched_at <= decision_at
            and candle.opened_at + period <= candle.first_observed_values_at <= decision_at
        ]
        known_counts[frame] = len(known)
        latest_matches[frame] = latest_recorded.get(frame) == (
            known[-1].opened_at if known else None
        )
        signal = analyze_entry(
            known,
            minimum_liquidity_usd=float(minimum_liquidity),
            timeframe=frame,
            signal_threshold=raw_threshold,
            as_of=decision_at,
        )
        signals[frame] = signal
        frame_matches[frame] = json.loads(json.dumps(signal.as_dict())) == recorded[frame]
    eligible, _ = multi_frame_entry_verdict(signals)
    provenance_limited_frames = [
        frame
        for frame in frames
        if known_counts[frame] < 8
        and recorded[frame].get("status") != "collecting"
        and (not frame_matches[frame] or not latest_matches[frame])
    ]
    matched = (
        eligible == (expected_verdict == "eligible")
        and all(frame_matches.values())
        and all(latest_matches.values())
    )
    only_provenance_limited = bool(provenance_limited_frames) and all(
        frame_matches[frame] and latest_matches[frame]
        for frame in frames
        if frame not in provenance_limited_frames
    )
    return {
        "status": "matched"
        if matched
        else "unverifiable"
        if only_provenance_limited
        else "mismatch",
        "reason": "historical_candle_provenance_incomplete" if only_provenance_limited else None,
        "provenance_limited_frames": provenance_limited_frames,
        "policy_source": policy_source,
        "expected_verdict": expected_verdict,
        "decision_as_of": decision_at,
        "known_candles": known_counts,
        "frame_matches": frame_matches,
        "latest_candle_matches": latest_matches,
        "replayed_entry_eligible": eligible,
    }


def verify_paper_entry_market_gate(
    decision: dict[str, object], settings: Settings
) -> dict[str, object]:
    """Recompute a recorded market gate with its saved policy and inputs."""
    evidence = decision.get("evidence")
    mint = decision.get("mint")
    if not isinstance(evidence, dict) or not isinstance(mint, str):
        return {"status": "unverifiable", "reason": "entry_evidence_missing"}
    if evidence.get("market_policy_version") != MARKET_POLICY_VERSION:
        return {"status": "unverifiable", "reason": "market_policy_version_missing_or_unknown"}
    policy = evidence.get("market_policy")
    inputs = evidence.get("market_gate_inputs")
    evaluated_at_ms = evidence.get("market_evaluated_at_ms")
    if not isinstance(policy, dict) or not isinstance(inputs, dict):
        return {"status": "unverifiable", "reason": "market_gate_evidence_incomplete"}
    liquidity = policy.get("minimum_liquidity_usd")
    volume = policy.get("minimum_volume_24h_usd")
    minimum_score = policy.get("minimum_score")
    if (
        type(liquidity) is not int
        or liquidity <= 0
        or type(volume) is not int
        or volume < 0
        or type(minimum_score) is not int
        or not 1 <= minimum_score <= 100
        or policy.get("maximum_absolute_24h_change_pct") != MAX_ABSOLUTE_24H_CHANGE_PCT
        or type(evaluated_at_ms) is not int
        or evaluated_at_ms <= 0
        or inputs.get("mint") != mint
    ):
        return {"status": "unverifiable", "reason": "market_gate_policy_or_inputs_invalid"}
    replay_settings = replace(
        settings,
        strategy_min_liquidity_usd=liquidity,
        strategy_min_volume_24h_usd=volume,
        strategy_min_score=minimum_score,
    )
    replayed = evaluate_candidate(inputs, replay_settings, now_ms=evaluated_at_ms)
    matched = (
        replayed.verdict == evidence.get("market_gate_verdict")
        and replayed.score == evidence.get("market_gate_score")
        and replayed.evidence["market_gate_reasons"] == evidence.get("market_gate_reasons")
    )
    return {
        "status": "matched" if matched else "mismatch",
        "replayed_verdict": replayed.verdict,
        "replayed_score": replayed.score,
    }


def summarize_saved_market_gate_reproducibility(
    journal: TradeJournal, settings: Settings, *, limit: int = 5_000
) -> dict[str, object]:
    """Recompute bounded saved market gates; this does not prove historical data freshness."""
    if not 1 <= limit <= 50_000:
        raise ValueError("Market-gate verification limit must be between 1 and 50000")
    connection = journal._connection
    total = int(connection.execute("SELECT COUNT(*) FROM strategy_decisions").fetchone()[0])
    statuses: Counter[str] = Counter()
    entry_statuses: Counter[str] = Counter()
    unverifiable_reasons: Counter[str] = Counter()
    checked = 0
    for mint, action, raw_evidence in connection.execute(
        """SELECT mint, action, evidence_json FROM strategy_decisions
           ORDER BY created_at DESC, rowid DESC LIMIT ?""",
        (limit,),
    ):
        checked += 1
        try:
            outcome = verify_paper_entry_market_gate(
                {"mint": mint, "evidence": json.loads(raw_evidence)}, settings
            )
            status = str(outcome["status"])
            reason = str(outcome.get("reason") or "")
        except Exception as exc:
            status = "unverifiable"
            reason = f"verification_{type(exc).__name__}"
        statuses[status] += 1
        if action in {"paper_opened", "call_paper_opened"}:
            entry_statuses[status] += 1
        if status == "unverifiable":
            unverifiable_reasons[reason or "unknown"] += 1
    return {
        "scope": "latest_saved_decisions_recomputed_from_saved_policy_and_inputs",
        "total_decisions": total,
        "checked": checked,
        "truncated": checked < total,
        "statuses": dict(statuses),
        "paper_entry_statuses": dict(entry_statuses),
        "unverifiable_reasons": dict(unverifiable_reasons),
        "historical_market_data_parity": False,
    }


def summarize_decision_cycle_integrity(rows: list[dict[str, object]]) -> dict[str, object]:
    """Check whether bounded saved decisions preserve complete worker cycles."""
    cycles: dict[str, list[dict[str, object]]] = defaultdict(list)
    legacy = 0
    for row in rows:
        evidence = row.get("evidence")
        cycle_id = evidence.get("decision_cycle_id") if isinstance(evidence, dict) else None
        if isinstance(cycle_id, str) and cycle_id:
            cycles[cycle_id].append(row)
        else:
            legacy += 1
    statuses: Counter[str] = Counter()
    summaries: list[dict[str, object]] = []
    for cycle_id, decisions in cycles.items():
        evidence_rows = [cast(dict[str, object], row["evidence"]) for row in decisions]
        sizes = [item.get("decision_cycle_size") for item in evidence_rows]
        ranks = [item.get("decision_cycle_rank") for item in evidence_rows]
        started = [item.get("decision_cycle_started_at") for item in evidence_rows]
        breakers = [item.get("decision_cycle_breakers") for item in evidence_rows]
        opened = sum(row.get("action") == "paper_opened" for row in decisions)
        expected_size = sizes[0]
        valid_ranks = [rank for rank in ranks if type(rank) is int]
        valid_started = [value for value in started if isinstance(value, str)]
        valid = (
            type(expected_size) is int
            and 1 <= expected_size <= 100
            and all(size == expected_size for size in sizes)
            and len(valid_ranks) == len(ranks)
            and len(set(valid_ranks)) == len(ranks)
            and all(1 <= rank <= expected_size for rank in valid_ranks)
            and len(valid_started) == len(started)
            and len(set(valid_started)) == 1
            and all(value == breakers[0] for value in breakers)
            and isinstance(breakers[0], list)
            and opened <= 1
        )
        status = (
            "inconsistent"
            if not valid
            else "complete"
            if len(decisions) == expected_size
            else "partial_recent_window"
        )
        statuses[status] += 1
        summaries.append(
            {
                "cycle_id": cycle_id,
                "status": status,
                "recorded_decisions": len(decisions),
                "expected_decisions": expected_size if valid else None,
                "paper_opened": opened,
            }
        )
    return {
        "scope": "latest_100_saved_decisions",
        "sampled_decisions": len(rows),
        "legacy_without_cycle_id": legacy,
        "statuses": dict(statuses),
        "cycles": summaries,
    }


def summarize_recent_decisions(rows: list[dict[str, object]]) -> dict[str, object]:
    """Count observed gates in the latest saved decisions without inferring returns."""
    actions: Counter[str] = Counter()
    verdicts: Counter[str] = Counter()
    candle_statuses: dict[str, Counter[str]] = {"1m": Counter(), "5m": Counter()}
    audit_safe = 0
    market_gate_passed = 0
    required_frames_ready = 0
    required_frames_confirmed = 0
    for row in rows:
        actions[str(row.get("action") or "unknown")] += 1
        verdicts[str(row.get("verdict") or "unknown")] += 1
        evidence = row.get("evidence")
        if not isinstance(evidence, dict):
            continue
        if evidence.get("audit_status") == "safe":
            audit_safe += 1
        signals = evidence.get("candle_signals")
        if not isinstance(signals, dict):
            continue
        # Candle confirmation runs only after the market-only strategy gate.
        market_gate_passed += 1
        required: list[dict[str, object]] = []
        for frame in ("1m", "5m"):
            signal = signals.get(frame)
            if not isinstance(signal, dict):
                candle_statuses[frame]["missing"] += 1
                continue
            required.append(signal)
            status = signal.get("status")
            candle_statuses[frame][status if isinstance(status, str) else "missing"] += 1
        if len(required) == 2 and all(signal.get("status") == "ready" for signal in required):
            required_frames_ready += 1
        if len(required) == 2 and all(signal.get("confirmed") is True for signal in required):
            required_frames_confirmed += 1
    return {
        "scope": "latest_saved_decisions_up_to_100",
        "sample_size": len(rows),
        "latest_at": rows[0].get("created_at") if rows else None,
        "actions": dict(actions),
        "verdicts": dict(verdicts),
        "audit_safe": audit_safe,
        "market_gate_passed": market_gate_passed,
        "required_frames_ready": required_frames_ready,
        "required_frames_confirmed": required_frames_confirmed,
        "candle_statuses": {frame: dict(counts) for frame, counts in candle_statuses.items()},
    }


def summarize_collector_signal_outcomes(price_paths: dict[str, object]) -> dict[str, object]:
    """Describe prospective rejection paths without interpreting them as fills."""
    frames = price_paths.get("horizons")
    output: dict[str, object] = {}
    for horizon in ("5", "15"):
        frame = frames.get(horizon) if isinstance(frames, dict) else None
        rows = frame.get("rows") if isinstance(frame, dict) else None
        groups: dict[tuple[str, str], dict[str, Any]] = {}
        for row in rows if isinstance(rows, list) else []:
            if not isinstance(row, dict):
                continue
            verdict = str(row.get("verdict") or "unknown")[:20]
            reason = str(row.get("final_reason") or "unknown")[:160]
            group = groups.setdefault(
                (verdict, reason),
                {
                    "count": 0,
                    "statuses": Counter(),
                    "closes": [],
                    "favorable": [],
                    "adverse": [],
                    "timely_mints": set(),
                },
            )
            group["count"] += 1
            outcome = row.get("outcome")
            status = (
                str(outcome.get("status") or "unknown") if isinstance(outcome, dict) else "unknown"
            )
            if status != "observed_timely" or not isinstance(outcome, dict):
                group["statuses"][status] += 1
                continue
            values = (
                outcome.get("horizon_close_pct"),
                outcome.get("max_favorable_pct"),
                outcome.get("max_adverse_pct"),
            )
            numeric_values: list[float] = []
            for value in values:
                if isinstance(value, bool) or not isinstance(value, (int, float)):
                    break
                if not isfinite(value):
                    break
                numeric_values.append(float(value))
            if len(numeric_values) != 3:
                group["statuses"]["invalid_timely_values"] += 1
                continue
            group["statuses"][status] += 1
            group["closes"].append(numeric_values[0])
            group["favorable"].append(numeric_values[1])
            group["adverse"].append(numeric_values[2])
            mint = row.get("mint")
            if isinstance(mint, str) and mint:
                group["timely_mints"].add(mint)
        summaries = []
        for (verdict, reason), group in sorted(
            groups.items(), key=lambda item: (-item[1]["count"], item[0])
        ):
            closes = group["closes"]
            summaries.append(
                {
                    "verdict": verdict,
                    "reason": reason,
                    "observations": group["count"],
                    "outcome_statuses": dict(group["statuses"]),
                    "timely_outcomes": len(closes),
                    "timely_distinct_mints": len(group["timely_mints"]),
                    "interpretation_status": (
                        "descriptive_only"
                        if len(closes) >= 30 and len(group["timely_mints"]) >= 30
                        else "insufficient_timely_sample"
                    ),
                    "median_horizon_close_pct": round(median(closes), 4) if closes else None,
                    "median_max_favorable_pct": (
                        round(median(group["favorable"]), 4) if closes else None
                    ),
                    "median_max_adverse_pct": (
                        round(median(group["adverse"]), 4) if closes else None
                    ),
                    "positive_close_count": sum(value > 0 for value in closes),
                }
            )
        output[horizon] = {
            "sample_size": len(rows) if isinstance(rows, list) else 0,
            "reason_groups": summaries,
        }
    return {
        "scope": "latest_age_eligible_focused_signal_observations_price_only",
        "minimum_timely_outcomes_per_reason_for_description": 30,
        "minimum_distinct_mints_per_reason_for_description": 30,
        "performance_inference": "none_without_executable_costed_forward_trades",
        "horizons": output,
    }


def summarize_current_research_lanes(
    cohort_order: list[str],
    observed_1m: list[tuple[str, int, object, object]],
    replay: list[dict[str, Any]],
    *,
    focus_size: int = 8,
    current_windows: dict[str, tuple[int, int]] | None = None,
) -> dict[str, object]:
    """Expose capture by the current cohort lanes without relabeling older cohorts."""
    current = set(cohort_order)
    output: dict[str, object] = {
        "scope": "current_runtime_cohort_only",
        "replay_status_scope": "all_recorded_windows_for_current_mints",
        "unclassified_historical_replay_mints": sum(
            str(row.get("mint")) not in current for row in replay
        ),
    }
    for lane, mints in (
        ("focus", set(cohort_order[:focus_size])),
        ("secondary", set(cohort_order[focus_size:])),
    ):
        bars = [
            row
            for row in observed_1m
            if row[0] in mints
            and (
                current_windows is None
                or (
                    (window := current_windows.get(row[0])) is not None
                    and window[0] <= row[1] < window[1]
                )
            )
        ]
        outcomes = [row for row in replay if str(row.get("mint")) in mints]
        reasons: Counter[str] = Counter()
        for outcome in outcomes:
            raw = outcome.get("censored_reasons")
            if isinstance(raw, dict):
                reasons.update(
                    {
                        str(reason): count
                        for reason, count in raw.items()
                        if type(count) is int and count > 0
                    }
                )
        output[lane] = {
            "selected_mints": len(mints),
            "membership_1m_candles": len(bars),
            "fetched_within_120s_of_close": sum(
                isinstance(fetched, int) and opened + 60 <= fetched <= opened + 180
                for _, opened, fetched, _ in bars
            ),
            "trusted_and_fetched_within_120s_of_close": sum(
                isinstance(fetched, int)
                and isinstance(observed, int)
                and opened + 60 <= fetched <= opened + 180
                and opened + 60 <= observed <= opened + 180
                for _, opened, fetched, observed in bars
            ),
            "replay_statuses": dict(Counter(str(row.get("status")) for row in outcomes)),
            "censored_reasons": dict(reasons),
        }
    return output


def summarize_current_research_capture(
    journal: TradeJournal,
    cohort_order: list[str],
    *,
    cohort_started_at: int | None,
    focus_size: int,
    as_of: int,
) -> dict[str, object]:
    """Measure timely closed 1m bars against elapsed slots in this cohort only."""
    output: dict[str, object] = {
        "scope": "current_cohort_current_recorded_pool",
        "window_note": "Missing minute bars can reflect no trades or unavailable provider data",
    }
    windows = (
        {
            str(mint): (int(selected_at), int(expires_at))
            for mint, selected_at, expires_at in journal._connection.execute(
                "SELECT mint, selected_at, expires_at FROM research_cohort_memberships "
                "WHERE cohort_started_at=?",
                (cohort_started_at,),
            )
        }
        if type(cohort_started_at) is int
        else {}
    )
    for lane, mints in (
        ("focus", cohort_order[:focus_size]),
        ("secondary", cohort_order[focus_size:]),
    ):
        eligible_slots = observed_bars = timely_bars = 0
        missing_pool = 0
        for mint in mints:
            window = windows.get(mint)
            if window is None:
                continue
            selected_at, expires_at = window
            start = (selected_at + 59) // 60 * 60
            last_open = (min(expires_at, as_of) - 60) // 60 * 60
            eligible_slots += max(0, (last_open - start) // 60 + 1)
            candidate = journal.discovery_candidate(mint)
            pool = candidate.get("pair_address") if candidate else None
            if not isinstance(pool, str) or not pool:
                missing_pool += 1
                continue
            for opened_at, first_fetch, first_observed in journal._connection.execute(
                "SELECT opened_at, first_fetched_at, first_observed_values_at "
                "FROM pool_candles WHERE mint=? AND pool=? AND timeframe='1m' "
                "AND opened_at>=? AND opened_at<=?",
                (mint, pool, start, last_open),
            ):
                if not isinstance(first_fetch, int) or first_fetch > as_of:
                    continue
                observed_bars += 1
                if (
                    isinstance(first_fetch, int)
                    and isinstance(first_observed, int)
                    and opened_at + 60 <= first_fetch <= opened_at + 180
                    and opened_at + 60 <= first_observed <= opened_at + 180
                ):
                    timely_bars += 1
        output[lane] = {
            "selected_mints": len(mints),
            "eligible_minute_slots": eligible_slots,
            "observed_closed_bars": observed_bars,
            "trusted_timely_bars": timely_bars,
            "mints_without_current_pool": missing_pool,
        }
    return output


def summarize_current_entry_readiness(
    journal: TradeJournal, settings: Settings
) -> dict[str, object]:
    """Use the production entry gates to expose today's data bottlenecks."""
    candidates = journal.discovery_candidates(limit=100)
    market_ready = [
        (candidate, decision)
        for candidate in candidates
        if (decision := evaluate_candidate(candidate, settings)).verdict == "eligible"
    ]
    decisions = [
        confirm_candidate_candles(candidate, decision, settings, journal)
        for candidate, decision in market_ready
    ]
    frame_statuses: dict[str, dict[str, int]] = {}
    for frame in ("1m", "5m", "15m", "1h"):
        frame_statuses[frame] = dict(
            Counter(
                str(signal.get("status"))
                for decision in decisions
                if isinstance(signals := decision.evidence.get("candle_signals"), dict)
                and isinstance(signal := signals.get(frame), dict)
            )
        )
    evaluated = sum(
        isinstance(decision.evidence.get("candle_signals"), dict) for decision in decisions
    )
    candle_data_limited = 0
    signal_misaligned = 0
    for decision in decisions:
        signals = decision.evidence.get("candle_signals")
        if not isinstance(signals, dict):
            continue
        required = [signals.get(frame) for frame in ("1m", "5m")]
        if any(
            not isinstance(signal, dict) or signal.get("status") != "ready" for signal in required
        ):
            candle_data_limited += 1
        elif decision.verdict != "eligible":
            signal_misaligned += 1
    return {
        "scope": "latest_100_current_discovery_candidates",
        "candidates": len(candidates),
        "market_gate_eligible": len(market_ready),
        "pre_candle_gate_blocked": len(decisions) - evaluated,
        "candle_evaluated": evaluated,
        "candle_data_limited": candle_data_limited,
        "signal_misaligned": signal_misaligned,
        "entry_eligible": sum(decision.verdict == "eligible" for decision in decisions),
        "final_reasons": dict(
            Counter(
                reason
                for decision in decisions
                if (
                    reason := primary_decision_reason(
                        decision.verdict, decision.reasons, decision.evidence
                    )
                )
            )
        ),
        "frame_statuses": frame_statuses,
    }


def summarize_candle_revisions(journal: TradeJournal) -> dict[str, Any]:
    """Describe observed provider corrections without implying complete coverage."""
    rows = journal._connection.execute(
        """SELECT mint, pool, timeframe, opened_at, first_observed_values_at,
                  revision_detected_at
           FROM pool_candles WHERE revision_detected_at IS NOT NULL"""
    ).fetchall()
    first_fetch_delays = [
        int(revised_at) - int(first_observed_at)
        for _, _, _, _, first_observed_at, revised_at in rows
        if type(first_observed_at) is int
        and type(revised_at) is int
        and revised_at >= first_observed_at
    ]
    close_delays = [
        int(revised_at) - (int(opened_at) + TIMEFRAME_SECONDS[str(timeframe)])
        for _, _, timeframe, opened_at, _, revised_at in rows
        if str(timeframe) in TIMEFRAME_SECONDS
        and type(opened_at) is int
        and type(revised_at) is int
        and revised_at >= opened_at + TIMEFRAME_SECONDS[str(timeframe)]
    ]
    columns = {
        str(row[1]) for row in journal._connection.execute("PRAGMA table_info(pool_candles)")
    }
    measured = (
        journal._connection.execute(
            """SELECT max_price_revision_bps, max_volume_revision_usd
               FROM pool_candles WHERE revision_detected_at IS NOT NULL
               AND (max_price_revision_bps > 0 OR max_volume_revision_usd > 0)"""
        ).fetchall()
        if {"max_price_revision_bps", "max_volume_revision_usd"} <= columns
        else []
    )
    price_changes = [float(row[0]) for row in measured if row[0] is not None and row[0] > 0]
    volume_changes = [float(row[1]) for row in measured if row[1] is not None and row[1] > 0]
    return {
        "scope": "detected_changes_on_repeated_fetches_only",
        "revised_candles": len(rows),
        "affected_pools": len({(mint, pool) for mint, pool, *_ in rows}),
        "by_timeframe": dict(Counter(str(row[2]) for row in rows)),
        "median_seconds_after_first_observation": (
            median(first_fetch_delays) if first_fetch_delays else None
        ),
        "median_seconds_after_close": median(close_delays) if close_delays else None,
        "revised_candles_with_measured_magnitude": len(measured),
        "measured_price_changes": len(price_changes),
        "median_max_price_revision_bps": median(price_changes) if price_changes else None,
        "largest_price_revision_bps": max(price_changes, default=None),
        "measured_volume_changes": len(volume_changes),
        "largest_volume_revision_usd": max(volume_changes, default=None),
        "coverage_note": (
            "Only changes seen on a later fetch are counted. Earlier provider revisions and "
            "bars never fetched again are unknown; this is not an error rate. "
            "Revision magnitudes are measured only on fetches after the metric was added."
        ),
    }


def report(settings: Settings) -> dict[str, Any]:
    with TradeJournal(settings.state_path, readonly=True) as journal:
        connection = journal._connection  # Read-only SQL for aggregate coverage.
        count_queries = {
            "discovery_candidates": "SELECT COUNT(*) FROM discovery_candidates",
            "audit_observations": "SELECT COUNT(*) FROM audit_observations",
            "market_snapshots": "SELECT COUNT(*) FROM market_snapshots",
            "narrative_signal_observations": ("SELECT COUNT(*) FROM narrative_signal_observations"),
            "pool_candles": "SELECT COUNT(*) FROM pool_candles",
            "strategy_decisions": "SELECT COUNT(*) FROM strategy_decisions",
            "collector_signal_observations": ("SELECT COUNT(*) FROM collector_signal_observations"),
            "paper_positions": "SELECT COUNT(*) FROM paper_positions",
            "paper_fills": "SELECT COUNT(*) FROM paper_fills",
            "strategy_backtests": "SELECT COUNT(*) FROM strategy_backtests",
        }
        counts = {
            table: int(connection.execute(query).fetchone()[0])
            for table, query in count_queries.items()
        }
        snapshots_with_capture = int(
            connection.execute(
                "SELECT COUNT(*) FROM market_snapshots WHERE captured_at IS NOT NULL"
            ).fetchone()[0]
        )
        snapshots_with_market_gate_inputs = int(
            connection.execute(
                """SELECT COUNT(*) FROM market_snapshots
                   WHERE captured_at IS NOT NULL AND price_change_24h IS NOT NULL
                   AND pair_created_at IS NOT NULL"""
            ).fetchone()[0]
        )
        snapshot_sources = (
            dict(
                connection.execute("SELECT source, COUNT(*) FROM market_snapshots GROUP BY source")
            )
            if any(
                row[1] == "source"
                for row in connection.execute("PRAGMA table_info(market_snapshots)")
            )
            else {"legacy_unknown": counts["market_snapshots"]}
        )
        decisions = dict(
            connection.execute(
                "SELECT action, COUNT(*) FROM strategy_decisions GROUP BY action"
            ).fetchall()
        )
        positions = dict(
            connection.execute(
                "SELECT status, COUNT(*) FROM paper_positions GROUP BY status"
            ).fetchall()
        )
        linked_entry_total = int(
            connection.execute(
                "SELECT COUNT(*) FROM strategy_decisions "
                "WHERE action='paper_opened' AND paper_position_id IS NOT NULL"
            ).fetchone()[0]
        )
        entry_parity: list[dict[str, object]] = []
        for decision_id, mint, position_id, raw_evidence in connection.execute(
            "SELECT decision_id, mint, paper_position_id, evidence_json "
            "FROM strategy_decisions WHERE action='paper_opened' "
            "AND paper_position_id IS NOT NULL ORDER BY created_at DESC LIMIT 100"
        ):
            try:
                evidence = json.loads(raw_evidence)
                parity_result = verify_paper_entry_signal(
                    journal,
                    {"mint": mint, "evidence": evidence},
                    default_minimum_liquidity_usd=float(settings.strategy_min_liquidity_usd),
                )
                market_result = verify_paper_entry_market_gate(
                    {"mint": mint, "evidence": evidence}, settings
                )
            except Exception as exc:
                parity_result = {"status": "unverifiable", "reason": type(exc).__name__}
                market_result = {"status": "unverifiable", "reason": type(exc).__name__}
            entry_parity.append(
                {
                    "decision_id": decision_id,
                    "position_id": position_id,
                    **parity_result,
                    "market_gate": market_result,
                }
            )
        saved_backtests = []
        for strategy, raw in connection.execute(
            "SELECT strategy_name, result_json FROM strategy_backtests ORDER BY created_at"
        ):
            saved = json.loads(raw)
            source = saved.get("data_source", "legacy_unverified")
            model = saved.get("entry_model")
            status = saved.get("status")
            if source != "geckoterminal_pool_trades":
                status = "legacy_unverified"
            elif model is None:
                status = "legacy_entry_model_unverified"
            elif saved.get("candle_provenance_version") != CANDLE_PROVENANCE_VERSION:
                status = "legacy_candle_provenance_unverified"
            saved_backtests.append(
                {
                    "strategy": strategy,
                    "status": status,
                    "recorded_status": saved.get("status"),
                    "data_source": source,
                    "entry_model": model,
                    "candle_provenance_version": saved.get("candle_provenance_version"),
                }
            )
        stored_groups = connection.execute(
            "SELECT mint, pool, timeframe, COUNT(*), MIN(opened_at), MAX(opened_at) "
            "FROM pool_candles GROUP BY mint, pool, timeframe ORDER BY mint, pool, timeframe"
        ).fetchall()
        stored_stats = {
            (str(mint), str(pool), str(frame)): (count, first, last)
            for mint, pool, frame, count, first, last in stored_groups
        }
        groups = [
            (mint, pool, frame, *stored_stats.get((mint, pool, frame), (0, None, None)))
            for mint, pool in sorted({(key[0], key[1]) for key in stored_stats})
            for frame in TIMEFRAME_SECONDS
            if (mint, pool, frame) in stored_stats or (mint, pool, "1m") in stored_stats
        ]
        replay_costs = estimated_replay_costs(settings)
        costs = replay_costs.execution_cost_bps
        additional_exit_fee = estimated_additional_exit_fee_bps(settings)
        swap_fraction_bps = estimated_paper_swap_fraction_bps(settings)
        cohort_state = journal.runtime_state(RESEARCH_COHORT_KEY) or {}
        research_observation = journal.runtime_state("ohlcv_research_active_focus") or {}
        research_observation_history = research_observation.get("history")
        cohort_started_at = cohort_state.get("selected_at")
        current_windows = (
            {
                str(mint): (int(selected_at), int(expires_at))
                for mint, selected_at, expires_at in connection.execute(
                    "SELECT mint, selected_at, expires_at FROM research_cohort_memberships "
                    "WHERE cohort_started_at=?",
                    (cohort_started_at,),
                )
            }
            if type(cohort_started_at) is int
            else {}
        )
        sampled_mints = {
            str(row[0])
            for row in connection.execute(
                "SELECT DISTINCT mint FROM research_cohort_memberships WHERE selection_method = ?",
                (RESEARCH_SELECTION_METHOD,),
            )
        }
        legacy_sampled_mints = {
            str(row[0])
            for row in connection.execute(
                "SELECT DISTINCT mint FROM research_cohort_memberships WHERE selection_method != ?",
                (RESEARCH_SELECTION_METHOD,),
            )
        }
        research_windows = {
            mint: journal.research_entry_windows(mint, selection_method=RESEARCH_SELECTION_METHOD)
            for mint in sampled_mints
        }
        observed_1m = [
            (str(mint), int(opened_at), first_fetched_at, first_observed_values_at)
            for mint, opened_at, first_fetched_at, first_observed_values_at in connection.execute(
                "SELECT mint, opened_at, first_fetched_at, first_observed_values_at "
                "FROM pool_candles WHERE timeframe='1m'"
            )
            if mint in sampled_mints
            and any(start <= opened_at < end for start, end in research_windows[str(mint)])
        ]
        known_first_fetch = sum(isinstance(first, int) for _, _, first, _ in observed_1m)
        trusted_first_values = sum(isinstance(first, int) for _, _, _, first in observed_1m)
        timely_first_fetch = sum(
            isinstance(first, int) and opened + 60 <= first <= opened + 180
            for _, opened, first, _ in observed_1m
        )
        trusted_timely_first_fetch = sum(
            isinstance(first, int)
            and isinstance(observed, int)
            and opened + 60 <= first <= opened + 180
            and opened + 60 <= observed <= opened + 180
            for _, opened, first, observed in observed_1m
        )
        coverage: list[dict[str, Any]] = []
        five_minute_overlap: list[dict[str, Any]] = []
        higher_timeframe_overlap: list[dict[str, Any]] = []
        replay: list[dict[str, Any]] = []
        replay_mints_with_1m: set[str] = set()
        for mint, pool, frame, count, first, last in groups:
            if frame not in TIMEFRAME_SECONDS:
                continue
            verified_frame = cast(Timeframe, frame)
            period = TIMEFRAME_SECONDS[frame]
            candidate = connection.execute(
                "SELECT first_seen FROM discovery_candidates WHERE mint=?", (mint,)
            ).fetchone()
            cutoff = _entry_cutoff(candidate[0]) if candidate else None
            selected_at = journal.first_research_selected_at(
                mint, selection_method=RESEARCH_SELECTION_METHOD
            )
            entry_windows = journal.research_entry_windows(
                mint, selection_method=RESEARCH_SELECTION_METHOD
            )
            if selected_at is not None:
                cutoff = max(cutoff or selected_at, selected_at)
            candles = load_verified_candles(journal, mint, pool, verified_frame)
            if not candles and count == 0:
                continue
            missing_intervals = sum(
                right.opened_at - left.opened_at != period for left, right in pairwise(candles)
            )
            coverage.append(
                {
                    "mint": mint,
                    "pool": pool,
                    "timeframe": frame,
                    "count": len(candles),
                    "stored_count": count,
                    "derived_from_1m_count": (
                        sum(c.samples == period // 60 for c in candles) if frame != "1m" else 0
                    ),
                    "derived_5m_count": sum(c.samples == 5 for c in candles),
                    "first_opened_at": candles[0].opened_at if candles else first,
                    "last_opened_at": candles[-1].opened_at if candles else last,
                    "aligned": all(c.opened_at % period == 0 for c in candles),
                    "missing_intervals": missing_intervals,
                    "candles_with_contemporaneous_liquidity": sum(
                        c.liquidity_usd > 0 for c in candles
                    ),
                    "entry_not_before": cutoff,
                    "research_selected_at": selected_at,
                    "research_entry_window_count": len(entry_windows),
                }
            )
            if frame == "5m":
                five_minute_overlap.append(
                    {
                        "mint": mint,
                        "pool": pool,
                        **timeframe_overlap_diagnostic(journal, mint, pool, "5m"),
                    }
                )
            elif frame in {"15m", "1h"}:
                higher_timeframe_overlap.append(
                    {
                        "mint": mint,
                        "pool": pool,
                        "timeframe": frame,
                        **timeframe_overlap_diagnostic(journal, mint, pool, verified_frame),
                    }
                )
            if frame != "1m" or mint not in sampled_mints:
                continue
            if not any(
                start <= candle.opened_at < end
                for candle in candles
                for start, end in entry_windows
            ):
                # A saved bar from before selection (or after expiry) cannot
                # establish coverage of this prospective research sample.
                continue
            replay_mints_with_1m.add(mint)
            if cutoff is None:
                replay.append(
                    {
                        "mint": mint,
                        "pool": pool,
                        "status": "missing_discovery_time",
                        "trades": 0,
                        "historical_market_gate_parity": False,
                        "paper_entry_schedule_parity": False,
                    }
                )
                continue
            if swap_fraction_bps == 0:
                replay.append(
                    {
                        "mint": mint,
                        "pool": pool,
                        "status": "insufficient_paper_risk_budget",
                        "trades": 0,
                        "historical_market_gate_parity": False,
                        "paper_entry_schedule_parity": False,
                    }
                )
                continue
            result = run_backtest(
                candles,
                exit_model="paper_polling_proxy",
                paper_poll_seconds=settings.paper_exit_interval_seconds,
                minimum_liquidity_usd=float(settings.strategy_min_liquidity_usd),
                tp_levels_bps=settings.paper_tp_levels_bps,
                tp_sell_bps=settings.paper_tp_sell_bps,
                stop_loss_bps=settings.paper_stop_loss_bps,
                execution_cost_bps=costs,
                fixed_cost_bps=replay_costs.fixed_cost_bps,
                refundable_ata_reserve_bps=replay_costs.refundable_ata_reserve_bps,
                additional_exit_fee_bps=additional_exit_fee,
                position_fraction_bps=swap_fraction_bps,
                trailing_activation_bps=settings.paper_trailing_activation_bps,
                trailing_stop_bps=settings.paper_trailing_stop_bps,
                max_hold_seconds=settings.paper_max_hold_seconds,
                minimum_exit_liquidity_usd=float(settings.paper_liquidity_breaker_usd),
                max_consecutive_losses=settings.paper_max_consecutive_losses,
                max_drawdown_bps=settings.paper_max_drawdown_bps,
                daily_loss_bps=settings.paper_daily_loss_bps,
                entry_not_before=cutoff,
                entry_windows=entry_windows or None,
                confirmation_frames={
                    frame: load_verified_candles(journal, mint, pool, frame)
                    for frame in ("5m", "15m", "1h")
                },
                audit_observations=journal.audit_observations(mint),
                market_snapshots=journal.market_snapshots(mint),
                minimum_volume_24h_usd=float(settings.strategy_min_volume_24h_usd),
                expected_pool=pool,
                market_score_replayer=make_replay_market_scorer(journal, settings, mint),
            )
            measured = int(result["trades"]) > 0
            censored = int(result.get("censored_trades", 0))
            if result["status"] != "complete":
                replay_status = str(result["status"])
            elif measured and censored:
                replay_status = "partial_closed_trades"
            elif measured:
                replay_status = "complete"
            elif censored:
                replay_status = "censored_unclosed"
            else:
                replay_status = "no_eligible_trades"
            replay.append(
                {
                    "mint": mint,
                    "pool": pool,
                    "status": replay_status,
                    "entry_model": result["entry_model"],
                    "historical_market_gate_parity": result["historical_market_gate_parity"],
                    "historical_market_hard_gates": result.get("historical_market_hard_gates"),
                    "historical_market_score_gate_enabled": result.get(
                        "historical_market_score_gate_enabled"
                    ),
                    "historical_market_score_evaluations": result.get(
                        "historical_market_score_evaluations", 0
                    ),
                    "paper_entry_schedule_parity": result["paper_entry_schedule_parity"],
                    "historical_safety_audit": result.get("historical_safety_audit"),
                    "trades": result["trades"],
                    "censored_trades": censored,
                    "censored_reasons": dict(
                        Counter(str(item["reason"]) for item in result.get("censored_log", []))
                    ),
                    "entry_decisions_in_window": result.get("entry_decisions_in_window"),
                    "entry_rejection_counts": result.get("entry_rejection_counts", {}),
                    "win_rate_pct": result.get("win_rate_pct") if measured else None,
                    "profit_factor": result.get("profit_factor") if measured else None,
                    "return_pct": result["return_pct"] if measured else None,
                    "max_drawdown_pct": result.get("max_drawdown_pct") if measured else None,
                    "expectancy_pct": result.get("expectancy_pct") if measured else None,
                    "full_rent_recovery_sensitivity": (
                        result.get("full_rent_recovery_sensitivity") if measured else None
                    ),
                    "average_holding_seconds": (
                        result.get("average_holding_seconds") if measured else None
                    ),
                    "loss_reasons": dict(
                        Counter(
                            str(trade["exit_reason"])
                            for trade in result["trade_log"]
                            if float(trade["return_pct"]) < 0
                        )
                    ),
                }
            )
        for mint in sorted(sampled_mints - replay_mints_with_1m):
            replay.append(
                {
                    "mint": mint,
                    "pool": None,
                    "status": "missing_1m_history",
                    "trades": 0,
                    "historical_market_gate_parity": False,
                    "paper_entry_schedule_parity": False,
                }
            )
        recent_decisions = journal.strategy_decisions()
        candle_decisions = [
            decision
            for decision in recent_decisions
            if isinstance(evidence := decision.get("evidence"), dict)
            and isinstance(evidence.get("candle_signals"), dict)
        ]
        candle_parity = [
            verify_paper_entry_signal(
                journal,
                decision,
                default_minimum_liquidity_usd=float(settings.strategy_min_liquidity_usd),
            )
            for decision in candle_decisions
        ]
        opportunity_as_of = int(datetime.now().timestamp())
        opportunity_cohorts = {
            horizon: journal.strategy_decisions(before=opportunity_as_of - (horizon + 3) * 60)
            for horizon in (5, 15)
        }
        collector_observations = journal.collector_signal_observations()
        collector_cohorts = {
            horizon: journal.collector_signal_observations(
                before=opportunity_as_of - (horizon + 3) * 60
            )
            for horizon in (5, 15)
        }
        collector_candle_paths = summarize_recent_price_paths(
            journal, collector_cohorts, as_of=opportunity_as_of
        )
        collector_snapshot_paths = summarize_snapshot_decision_paths(
            journal, collector_cohorts, as_of=opportunity_as_of
        )
        collector_frame_statuses: dict[str, Counter[str]] = {
            "1m": Counter(),
            "5m": Counter(),
        }
        for observation in collector_observations:
            evidence = observation.get("evidence")
            signals = evidence.get("candle_signals") if isinstance(evidence, dict) else None
            for frame in collector_frame_statuses:
                signal = signals.get(frame) if isinstance(signals, dict) else None
                status = signal.get("status") if isinstance(signal, dict) else None
                collector_frame_statuses[frame][str(status or "not_evaluated")] += 1
        return {
            "as_of": datetime.now().astimezone().isoformat(),
            "paper_campaign": journal.runtime_state("paper_campaign"),
            "live_execution_locked": LIVE_EXECUTION_LOCKED,
            "environment_live_flag": settings.live_trading_enabled,
            "counts": counts,
            "snapshots_with_capture_time": snapshots_with_capture,
            "snapshots_with_24h_move_and_pair_age": snapshots_with_market_gate_inputs,
            "market_snapshots_by_source": snapshot_sources,
            "research_pool_market_probe": journal.runtime_state("research_pool_market_probe"),
            "decision_actions": decisions,
            "recent_decision_funnel": summarize_recent_decisions(recent_decisions),
            "collector_signal_funnel": {
                "scope": "latest_100_successfully_refreshed_candidates_while_paper_worker_active",
                "total_observations": counts["collector_signal_observations"],
                "sample_size": len(collector_observations),
                "unique_mints": len({str(row["mint"]) for row in collector_observations}),
                "verdicts": dict(Counter(str(row["verdict"]) for row in collector_observations)),
                "frame_statuses": {
                    frame: dict(statuses) for frame, statuses in collector_frame_statuses.items()
                },
                "selection_bias": (
                    "Only mints refreshed by the focused free OHLCV schedule are observed"
                ),
            },
            "current_entry_readiness": summarize_current_entry_readiness(journal, settings),
            "ohlcv_collection_capacity": summarize_ohlcv_collection_capacity(journal),
            "candle_revisions": summarize_candle_revisions(journal),
            "decision_cycle_integrity": summarize_decision_cycle_integrity(recent_decisions),
            "recent_candle_decision_parity": {
                "scope": "latest_100_saved_decisions_with_candle_evidence",
                "checked": len(candle_parity),
                "statuses": dict(Counter(str(row["status"]) for row in candle_parity)),
                "expected_verdicts": dict(
                    Counter(str(decision["verdict"]) for decision in candle_decisions)
                ),
            },
            "recent_decision_price_paths": summarize_recent_price_paths(
                journal,
                opportunity_cohorts,
                as_of=opportunity_as_of,
            ),
            "recent_snapshot_decision_paths": summarize_snapshot_decision_paths(
                journal,
                opportunity_cohorts,
                as_of=opportunity_as_of,
            ),
            "collector_signal_price_paths": {
                "population": "prospective_focused_collector_observations_only",
                "candles": collector_candle_paths,
                "snapshots": collector_snapshot_paths,
            },
            "collector_signal_rejection_outcomes": summarize_collector_signal_outcomes(
                collector_candle_paths
            ),
            "paper_positions_by_status": positions,
            "forward_paper_performance": summarize_forward_paper(
                journal,
                settings.paper_starting_lamports,
                paper_poll_seconds=settings.paper_exit_interval_seconds,
            ),
            "paper_entry_signal_parity": {
                "scope": "latest_100_linked_paper_entries",
                "total_linked_entries": linked_entry_total,
                "checked": len(entry_parity),
                "statuses": dict(Counter(str(row["status"]) for row in entry_parity)),
                "market_gate_statuses": dict(
                    Counter(
                        str(market.get("status"))
                        for row in entry_parity
                        if isinstance(market := row.get("market_gate"), dict)
                    )
                ),
                "entries": entry_parity,
            },
            "saved_market_gate_reproducibility": summarize_saved_market_gate_reproducibility(
                journal, settings
            ),
            "estimated_round_trip_cost_bps": costs,
            "estimated_fixed_round_trip_cost_bps": replay_costs.fixed_cost_bps,
            "estimated_refundable_ata_reserve_bps": (replay_costs.refundable_ata_reserve_bps),
            "estimated_additional_exit_fee_bps": additional_exit_fee,
            "estimated_paper_swap_fraction_bps": swap_fraction_bps,
            "paper_total_debit_budget_bps": settings.paper_risk_per_trade_bps,
            "research_cohort": {
                "size": len(research_cohort_mints(journal)),
                "focus_size": min(
                    research_focus_size(journal), len(research_cohort_mints(journal))
                ),
                "population_count": cohort_state.get("population_count"),
                "selected_at": cohort_state.get("selected_at"),
                "expires_at": cohort_state.get("expires_at"),
                "selection_method": cohort_state.get("selection_method"),
            },
            "research_active_observation": {
                "mint": research_observation.get("mint"),
                "selected_at": research_observation.get("selected_at"),
                "cohort_selected_at": research_observation.get("cohort_selected_at"),
                "selection_tier": research_observation.get("selection_tier"),
                "ended_windows_recorded": (
                    len(research_observation_history)
                    if isinstance(research_observation_history, list)
                    else 0
                ),
            },
            "replay_population": {
                "scope": "recorded_v2_recent_discovery_memberships_only",
                "history_scope": "saved_1m_bar_inside_recorded_membership_window",
                "selection_method": RESEARCH_SELECTION_METHOD,
                "selected_mints": len(sampled_mints),
                "legacy_membership_mints_excluded": len(legacy_sampled_mints),
                "mints_with_1m_history": len(replay_mints_with_1m),
                "mints_without_1m_history": len(sampled_mints - replay_mints_with_1m),
            },
            "research_1m_fetch_availability": {
                "membership_candles": len(observed_1m),
                "first_fetch_known": known_first_fetch,
                "first_fetch_unknown": len(observed_1m) - known_first_fetch,
                "first_observed_values_trusted": trusted_first_values,
                "first_observed_values_unknown": len(observed_1m) - trusted_first_values,
                "fetched_within_120s_of_close": timely_first_fetch,
                "trusted_and_fetched_within_120s_of_close": trusted_timely_first_fetch,
            },
            "current_research_lane_availability": summarize_current_research_lanes(
                research_cohort_mints(journal),
                observed_1m,
                replay,
                focus_size=research_focus_size(journal),
                current_windows=current_windows,
            ),
            "decision_followup_price_paths": summarize_decision_followup_paths(
                journal, as_of=opportunity_as_of
            ),
            "saved_backtests": saved_backtests,
            "ohlcv_coverage": coverage,
            "five_minute_overlap": five_minute_overlap,
            "higher_timeframe_overlap": higher_timeframe_overlap,
            "baseline_candlestick_replay": replay,
            "market_eligible_followup_replay": summarize_market_eligible_followup_replay(
                journal, settings
            ),
            "strategy_comparison": {
                "status": "unavailable",
                "baseline_strategy": "existing_four_factor_multi_timeframe_candles",
                "baseline_comparable_closed_trades": sum(int(row["trades"]) for row in replay),
                "baseline_historical_market_gate_parity": bool(replay)
                and all(row.get("historical_market_gate_parity") is True for row in replay),
                "upgraded_momentum_strategy": "not_implemented_indicators_deferred",
                "early_launch_strategy": "not_implemented",
                "reason": (
                    "The existing four-factor candle strategy is the baseline. "
                    "No distinct upgraded momentum or early-launch strategy is implemented."
                ),
            },
        }


def main() -> None:
    print(json.dumps(report(Settings.from_env()), indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
