"""Evidence-gated forward paper results from the local trade journal.

Only fully closed, costed strategy positions enter the performance sample.
User-initiated calls and manual paper entries stay separate from autonomous entries.
"""

from __future__ import annotations

import json
from collections import Counter, defaultdict
from datetime import UTC, datetime
from itertools import pairwise
from statistics import mean
from typing import Any

from .constants import MARKET_POLICY_VERSION
from .journal import TradeJournal

MINIMUM_INTERPRETABLE_TRADES = 30
_FRAMES = ("1m", "5m", "15m", "1h")
_MARKET_INPUT_KEYS = (
    "mint",
    "audit_status",
    "liquidity_usd",
    "volume_24h_usd",
    "price_change_24h",
    "pair_created_at",
    "narrative_signal",
)
_MARKET_POLICY_KEYS = (
    "minimum_liquidity_usd",
    "minimum_volume_24h_usd",
    "maximum_absolute_24h_change_pct",
    "minimum_score",
)
_COST_FIELDS = (
    "network_fee_lamports",
    "priority_fee_lamports",
    "ata_rent_lamports",
    "modeled_slippage_lamports",
    "explicit_cost_lamports",
    "quote_age_ms",
)


def _object(raw: object) -> dict[str, Any]:
    try:
        value = json.loads(str(raw))
    except (TypeError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _list(raw: object) -> list[Any]:
    try:
        value = json.loads(str(raw))
    except (TypeError, json.JSONDecodeError):
        return []
    return value if isinstance(value, list) else []


def _costed_execution(raw: object, *, side: str, amount_key: str, amount: int) -> bool:
    execution = _object(raw)
    return (
        execution.get("side") == side
        and execution.get(amount_key) == amount
        and all(type(execution.get(key)) is int and execution[key] >= 0 for key in _COST_FIELDS)
    )


def has_signal_evidence(evidence: object) -> bool:
    """Require the point-in-time candle and market fields used in forward results."""
    if not isinstance(evidence, dict):
        return False
    signals = evidence.get("candle_signals")
    market_policy = evidence.get("market_policy")
    market_inputs = evidence.get("market_gate_inputs")
    candle_policy = evidence.get("candle_policy")
    return (
        evidence.get("market_policy_version") == MARKET_POLICY_VERSION
        and type(evidence.get("market_evaluated_at_ms")) is int
        and evidence.get("market_gate_verdict") == "eligible"
        and type(evidence.get("market_gate_score")) is int
        and isinstance(market_policy, dict)
        and type(market_policy.get("minimum_score")) is int
        and all(key in market_policy for key in _MARKET_POLICY_KEYS)
        and isinstance(market_inputs, dict)
        and isinstance(market_inputs.get("mint"), str)
        and all(key in market_inputs for key in _MARKET_INPUT_KEYS)
        and isinstance(candle_policy, dict)
        and type(candle_policy.get("signal_threshold")) is int
        and "minimum_liquidity_usd" in candle_policy
        and isinstance(evidence.get("decision_as_of"), int)
        and isinstance(evidence.get("market_captured_at"), int)
        and isinstance(evidence.get("audit_last_seen"), str)
        and isinstance(signals, dict)
        and all(
            isinstance(signals.get(frame), dict)
            and isinstance(signals[frame].get("status"), str)
            and type(signals[frame].get("confirmed")) is bool
            and type(signals[frame].get("score")) is int
            for frame in _FRAMES
        )
    )


def _signal_evidence(raw: object) -> bool:
    return has_signal_evidence(_object(raw))


def _entry_revalidation(raw: object, opened: object, mint: str) -> bool:
    """Check that the saved final signal was current when the paper fill committed."""
    validation = _object(raw).get("entry_revalidation")
    if not isinstance(validation, dict):
        return False
    try:
        opened_at = datetime.fromisoformat(str(opened))
        audited_at = datetime.fromisoformat(str(validation.get("audit_last_seen")))
    except ValueError:
        return False
    if opened_at.tzinfo is None or audited_at.tzinfo is None:
        return False
    decision_at = validation.get("decision_as_of")
    market_at = validation.get("market_captured_at")
    market_evaluated_at = validation.get("market_evaluated_at_ms")
    score = validation.get("score")
    market_score = validation.get("market_gate_score")
    market_inputs = validation.get("market_gate_inputs")
    market_policy = validation.get("market_policy")
    candle_policy = validation.get("candle_policy")
    reasons = validation.get("reasons")
    signals = validation.get("candle_signals")
    latest = validation.get("latest_closed_candle_at")
    if (
        type(decision_at) is not int
        or type(market_at) is not int
        or type(market_evaluated_at) is not int
        or type(score) is not int
        or type(market_score) is not int
        or not 0 <= score <= 100
        or not 0 <= market_score <= 100
        or validation.get("verdict") != "eligible"
        or validation.get("market_policy_version") != MARKET_POLICY_VERSION
        or validation.get("market_gate_verdict") != "eligible"
        or not isinstance(reasons, list)
        or not all(isinstance(reason, str) for reason in reasons)
        or not isinstance(market_inputs, dict)
        or market_inputs.get("mint") != mint
        or market_inputs.get("audit_status") != "safe"
        or not all(key in market_inputs for key in _MARKET_INPUT_KEYS)
        or not isinstance(market_policy, dict)
        or not all(key in market_policy for key in _MARKET_POLICY_KEYS)
        or not isinstance(candle_policy, dict)
        or "minimum_liquidity_usd" not in candle_policy
        or type(candle_policy.get("signal_threshold")) is not int
        or not isinstance(signals, dict)
        or not isinstance(latest, dict)
    ):
        return False
    opened_seconds = opened_at.timestamp()
    audit_age = (opened_at - audited_at).total_seconds()
    if not (
        -30 <= opened_seconds - decision_at <= 120
        and -30_000 <= opened_seconds * 1_000 - market_evaluated_at <= 120_000
        and -30 <= opened_seconds - market_at <= 180
        and -60 <= audit_age <= 600
    ):
        return False
    for frame, seconds in (("1m", 60), ("5m", 300)):
        signal = signals.get(frame)
        candle_at = latest.get(frame)
        if (
            not isinstance(signal, dict)
            or signal.get("status") != "ready"
            or signal.get("confirmed") is not True
            or type(signal.get("score")) is not int
            or type(candle_at) is not int
            or not 0 <= decision_at - (candle_at + seconds) <= 2 * seconds
        ):
            return False
    for frame in ("15m", "1h"):
        signal = signals.get(frame)
        if (
            not isinstance(signal, dict)
            or not isinstance(signal.get("status"), str)
            or type(signal.get("confirmed")) is not bool
            or type(signal.get("score")) is not int
            or signal.get("status")
            in {"conflicting_prices", "mixed_provenance", "revised_values", "unverified_values"}
            or (signal.get("status") == "ready" and signal.get("trend") == "down")
        ):
            return False
    return True


def _period_seconds(opened: object, closed: object) -> float | None:
    try:
        start = datetime.fromisoformat(str(opened))
        end = datetime.fromisoformat(str(closed))
    except ValueError:
        return None
    if start.tzinfo is None or end.tzinfo is None:
        return None
    seconds = (end - start).total_seconds()
    return seconds if seconds >= 0 else None


def _mark_path_covered(
    opened: object, closed_or_now: object, marks: list[str], *, window_seconds: int
) -> bool:
    if not marks:
        return False
    try:
        ordered = sorted(marks, key=datetime.fromisoformat)
    except (TypeError, ValueError):
        return False
    boundaries = [str(opened), *ordered, str(closed_or_now)]
    return all(
        (gap := _period_seconds(left, right)) is not None and gap <= window_seconds
        for left, right in pairwise(boundaries)
    )


def _observed_mark_drawdown_pct(entry_lamports: int, values: list[int]) -> float:
    peak = entry_lamports
    maximum = 0.0
    for value in values:
        peak = max(peak, value)
        maximum = max(maximum, (peak - value) / peak * 100)
    return round(maximum, 4)


def _position_mark_values(
    marks: list[tuple[str, int, int]],
    sells: list[tuple[Any, ...]],
    final_proceeds: int,
    original_tokens: int,
) -> list[int] | None:
    """Value the actual position: realized cash plus quoted remaining tokens."""
    # Windows timestamps can be identical across a mark and the next sell.
    # Token inventory identifies which fills preceded each mark without
    # guessing their order from equal wall-clock times.
    proceeds_by_sold = {0: 0}
    sold_tokens = 0
    realized_proceeds = 0
    for fill in sells:
        sold_tokens += int(fill[1])
        realized_proceeds += int(fill[2])
        if sold_tokens > original_tokens:
            return None
        proceeds_by_sold[sold_tokens] = realized_proceeds
    values: list[int] = []
    for _observed_at, remaining_value, remaining_tokens in marks:
        proceeds = proceeds_by_sold.get(original_tokens - remaining_tokens)
        if proceeds is None:
            return None
        values.append(proceeds + remaining_value)
    return [*values, final_proceeds]


def _metrics(trades: list[dict[str, Any]], starting_lamports: int) -> dict[str, Any]:
    if not trades:
        return {
            "status": "no_complete_closed_trades",
            "trades": 0,
            "win_rate_pct": None,
            "profit_factor": None,
            "net_pnl_lamports": None,
            "ata_rent_reserved_lamports": None,
            "net_pnl_if_full_ata_rent_reclaimed_lamports": None,
            "net_trade_return_pct": None,
            "max_closed_trade_drawdown_pct": None,
            "max_observed_mark_drawdown_pct": None,
            "trades_with_bounded_mark_gaps": 0,
            "expectancy_pct": None,
            "average_holding_seconds": None,
            "loss_reasons": {},
        }
    net_values = [int(trade["net_lamports"]) for trade in trades]
    rent_reserved = sum(int(trade["ata_rent_lamports"]) for trade in trades)
    returns = [
        value / int(trade["entry_lamports"]) * 100
        for value, trade in zip(net_values, trades, strict=True)
    ]
    profits = sum(value for value in net_values if value > 0)
    losses = abs(sum(value for value in net_values if value < 0))
    equity = starting_lamports
    peak = equity
    drawdown = 0.0
    for value in net_values:
        equity += value
        peak = max(peak, equity)
        drawdown = max(drawdown, (peak - equity) / peak if peak > 0 else 0.0)
    total_entry = sum(int(trade["entry_lamports"]) for trade in trades)
    mark_drawdowns = [
        float(trade["observed_mark_drawdown_pct"])
        for trade in trades
        if trade.get("observed_mark_drawdown_pct") is not None
    ]
    return {
        "status": (
            "limited_sample" if len(trades) < MINIMUM_INTERPRETABLE_TRADES else "descriptive_sample"
        ),
        "trades": len(trades),
        "win_rate_pct": round(sum(value > 0 for value in net_values) / len(trades) * 100, 2),
        "profit_factor": round(profits / losses, 4) if losses else None,
        "net_pnl_lamports": sum(net_values),
        "ata_rent_reserved_lamports": rent_reserved,
        "net_pnl_if_full_ata_rent_reclaimed_lamports": sum(net_values) + rent_reserved,
        "net_trade_return_pct": round(sum(net_values) / total_entry * 100, 4),
        "max_closed_trade_drawdown_pct": round(drawdown * 100, 4),
        "max_observed_mark_drawdown_pct": max(mark_drawdowns) if mark_drawdowns else None,
        "trades_with_bounded_mark_gaps": len(mark_drawdowns),
        "expectancy_pct": round(mean(returns), 4),
        "average_holding_seconds": round(mean(float(t["holding_seconds"]) for t in trades), 2),
        "loss_reasons": dict(
            Counter(str(trade["exit_reason"]) for trade in trades if int(trade["net_lamports"]) < 0)
        ),
    }


def summarize_forward_paper(
    journal: TradeJournal, starting_lamports: int, *, paper_poll_seconds: int = 300
) -> dict[str, Any]:
    """Return descriptive performance with explicit exclusions and no promotion claim."""
    if paper_poll_seconds <= 0:
        raise ValueError("Paper polling interval must be positive")
    connection = journal._connection
    campaign = journal.runtime_state("paper_campaign") or {}
    require_router_quote_parity = campaign.get("mode") == "forward_paper_only"
    campaign_start: datetime | None = None
    if require_router_quote_parity:
        try:
            parsed_start = datetime.fromisoformat(str(campaign.get("started_at")))
        except ValueError:
            pass
        else:
            if parsed_start.tzinfo is not None:
                campaign_start = parsed_start
    positions = connection.execute(
        """SELECT position_id, mint, entry_lamports, token_amount, opened_at,
                  closed_at, realized_lamports, realized_cost_lamports,
                  remaining_token_amount, entry_execution_json
           FROM paper_positions WHERE status = 'closed'
           ORDER BY closed_at, position_id"""
    ).fetchall()
    open_rows = connection.execute(
        "SELECT position_id, mint, opened_at, entry_execution_json "
        "FROM paper_positions WHERE status = 'open'"
    ).fetchall()
    open_ids = [str(row[0]) for row in open_rows]
    mark_rows = connection.execute(
        "SELECT position_id, observed_at, remaining_token_amount, value_lamports, "
        "performance_value_lamports, execution_json "
        "FROM paper_position_marks ORDER BY rowid"
    ).fetchall()
    marked_positions = {str(row[0]) for row in mark_rows}
    costed_marks: dict[str, list[tuple[str, int, int]]] = defaultdict(list)
    for position_id, observed_at, remaining_tokens, value, _performance_value, raw in mark_rows:
        if _costed_execution(raw, side="mark", amount_key="cash_credit_lamports", amount=value):
            costed_marks[str(position_id)].append(
                (str(observed_at), int(value), int(remaining_tokens))
            )
    mark_window_seconds = 2 * paper_poll_seconds + 120
    now = datetime.now(UTC).isoformat()
    position_periods = connection.execute(
        "SELECT position_id, opened_at, closed_at, status FROM paper_positions"
    ).fetchall()
    covered_open = 0
    covered_closed = 0
    covered_positions: set[str] = set()
    for position_id, opened_at, closed_at, status in position_periods:
        covered = _mark_path_covered(
            opened_at,
            closed_at if status == "closed" else now,
            [observed_at for observed_at, _, _ in costed_marks[str(position_id)]],
            window_seconds=mark_window_seconds,
        )
        if covered:
            covered_positions.add(str(position_id))
        covered_open += int(status == "open" and covered)
        covered_closed += int(status == "closed" and covered)
    quote_mark_history = {
        "total_marks": len(mark_rows),
        "costed_marks": sum(map(len, costed_marks.values())),
        "positions_with_marks": len(marked_positions),
        "open_positions_with_marks": len(marked_positions & set(open_ids)),
        "coverage_window_seconds": mark_window_seconds,
        "open_positions_with_bounded_mark_gaps": covered_open,
        "closed_positions_with_bounded_mark_gaps": covered_closed,
    }
    decisions: dict[str, list[tuple[Any, ...]]] = defaultdict(list)
    for row in connection.execute(
        """SELECT paper_position_id, mint, score, verdict, reasons_json, evidence_json, action
           FROM strategy_decisions WHERE paper_position_id IS NOT NULL"""
    ):
        decisions[str(row[0])].append(tuple(row[1:]))
    open_without_signal_evidence = sum(
        len(decisions.get(str(position_id), [])) != 1
        or not _signal_evidence(decisions[str(position_id)][0][4])
        or (require_router_quote_parity and not _entry_revalidation(entry_execution, opened, mint))
        for position_id, mint, opened, entry_execution in open_rows
    )
    fills: dict[str, list[tuple[Any, ...]]] = defaultdict(list)
    for row in connection.execute(
        """SELECT position_id, side, input_amount, output_amount, cost_lamports,
                  exit_reason, execution_json
           FROM paper_fills ORDER BY created_at, rowid"""
    ):
        fills[str(row[0])].append(tuple(row[1:]))

    samples: dict[str, list[dict[str, Any]]] = {"autonomous": [], "call_inbox": []}
    excluded: Counter[str] = Counter()
    manual_closed = 0
    campaign_closed_total = 0
    for position in positions:
        (
            position_id,
            mint,
            entry,
            tokens,
            opened,
            closed,
            proceeds,
            realized_cost,
            remaining,
            entry_execution,
        ) = position
        if require_router_quote_parity:
            if campaign_start is None:
                excluded["campaign_start_unverified"] += 1
                continue
            try:
                opened_at = datetime.fromisoformat(str(opened))
            except ValueError:
                excluded["invalid_campaign_open_time"] += 1
                continue
            if opened_at.tzinfo is None:
                excluded["invalid_campaign_open_time"] += 1
                continue
            if opened_at < campaign_start:
                excluded["pre_campaign_position"] += 1
                continue
        campaign_closed_total += 1
        linked = decisions.get(str(position_id), [])
        if len(linked) != 1:
            excluded["missing_or_ambiguous_entry_decision"] += 1
            continue
        decision_mint, score, verdict, reasons, evidence, action = linked[0]
        if action == "manual_paper_opened":
            manual_closed += 1
            continue
        if action not in ("paper_opened", "call_paper_opened"):
            excluded["unrecognized_entry_path"] += 1
            continue
        lane = "autonomous" if action == "paper_opened" else "call_inbox"
        if (
            decision_mint != mint
            or verdict != "eligible"
            or type(score) is not int
            or not _list(reasons)
            or not _signal_evidence(evidence)
            or _object(evidence).get("market_gate_inputs", {}).get("mint") != mint
        ):
            excluded["entry_signal_evidence_missing"] += 1
            continue
        if not isinstance(entry, int) or entry <= 0 or not isinstance(tokens, int) or tokens <= 0:
            excluded["invalid_position_amounts"] += 1
            continue
        if not _costed_execution(
            entry_execution, side="buy", amount_key="cash_debit_lamports", amount=entry
        ):
            excluded["entry_cost_evidence_missing"] += 1
            continue
        if (
            require_router_quote_parity
            and _object(entry_execution).get("quote_source") != "v2-build"
        ):
            excluded["entry_route_parity_missing"] += 1
            continue
        if require_router_quote_parity and not _entry_revalidation(entry_execution, opened, mint):
            excluded["entry_revalidation_missing_or_invalid"] += 1
            continue
        holding = _period_seconds(opened, closed)
        if holding is None:
            excluded["invalid_holding_period"] += 1
            continue
        position_fills = fills.get(str(position_id), [])
        buys = [fill for fill in position_fills if fill[0] == "buy"]
        sells = [fill for fill in position_fills if fill[0] == "sell"]
        if (
            len(buys) != 1
            or not sells
            or remaining != 0
            or not isinstance(proceeds, int)
            or proceeds != sum(int(fill[2]) for fill in sells)
            or realized_cost != entry
            or sum(int(fill[1]) for fill in sells) != tokens
            or int(buys[0][1]) != entry
            or int(buys[0][2]) != tokens
        ):
            excluded["fill_reconciliation_failed"] += 1
            continue
        if not all(
            isinstance(fill[4], str)
            and fill[4]
            and _costed_execution(
                fill[5], side="sell", amount_key="cash_credit_lamports", amount=int(fill[2])
            )
            for fill in sells
        ):
            excluded["exit_cost_or_reason_missing"] += 1
            continue
        if require_router_quote_parity and any(
            _object(fill[5]).get("quote_source") != "v2-build" for fill in sells
        ):
            excluded["exit_route_parity_missing"] += 1
            continue
        # A closed fill after an unmonitored interval does not prove the
        # strategy would have held through that interval. Keep it in the
        # immutable ledger, but exclude it from forward performance claims.
        if require_router_quote_parity and str(position_id) not in covered_positions:
            excluded["unbounded_quote_mark_gap"] += 1
            continue
        mark_values = (
            _position_mark_values(
                sorted(costed_marks[str(position_id)], key=lambda item: item[0]),
                sells,
                int(proceeds),
                tokens,
            )
            if str(position_id) in covered_positions
            else None
        )
        samples[lane].append(
            {
                "entry_lamports": entry,
                "net_lamports": int(proceeds) - entry,
                "ata_rent_lamports": int(_object(entry_execution)["ata_rent_lamports"]),
                "holding_seconds": holding,
                "exit_reason": str(sells[-1][4]),
                "observed_mark_drawdown_pct": (
                    _observed_mark_drawdown_pct(entry, mark_values)
                    if mark_values is not None
                    else None
                ),
            }
        )

    return {
        "scope": "closed_costed_positions_with_recorded_entry_signals",
        "minimum_interpretable_trades": MINIMUM_INTERPRETABLE_TRADES,
        "closed_positions_total": len(positions),
        "campaign_started_at": campaign_start.isoformat() if campaign_start else None,
        "campaign_status": campaign.get("status", "unrecorded"),
        "campaign_interruption": campaign.get("interruption"),
        "campaign_closed_positions_total": campaign_closed_total,
        "open_positions": len(open_ids),
        "open_positions_without_signal_evidence": open_without_signal_evidence,
        "quote_mark_history": quote_mark_history,
        "manual_closed_excluded": manual_closed,
        "incomplete_closed_excluded": sum(excluded.values()),
        "exclusion_reasons": dict(excluded),
        "autonomous": _metrics(samples["autonomous"], starting_lamports),
        "call_inbox": _metrics(samples["call_inbox"], starting_lamports),
        "rent_recovery_sensitivity": (
            "Hypothetical full token-account rent recovery after account closure; "
            "the paper worker does not simulate closing token accounts"
        ),
        "promotion_ready": False,
    }
