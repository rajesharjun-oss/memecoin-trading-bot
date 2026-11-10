"""Ex-post price paths after saved paper decisions, never executable PnL."""

from __future__ import annotations

from bisect import bisect_right
from collections import Counter
from collections.abc import Mapping
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from itertools import pairwise
from math import ceil, isfinite
from typing import Any, cast

from .journal import TradeJournal
from .strategy import primary_decision_reason
from .strategy_lab import Candle, load_verified_candles


def snapshot_decision_price_change(
    decision: dict[str, object],
    snapshots: list[dict[str, object]],
    *,
    horizon_minutes: int,
    as_of: int,
) -> dict[str, object]:
    """Measure a bounded price-only DEX snapshot change, never a fill or candle."""
    if horizon_minutes not in (5, 15):
        raise ValueError("Opportunity horizon must be 5 or 15 minutes")
    evidence = decision.get("evidence")
    pool = evidence.get("pool") if isinstance(evidence, dict) else None
    if not isinstance(pool, str) or not pool:
        return {"status": "missing_pool"}
    try:
        recorded_at = datetime.fromisoformat(str(decision["created_at"]))
    except (KeyError, ValueError):
        return {"status": "invalid_decision_time"}
    if recorded_at.tzinfo is None:
        return {"status": "invalid_decision_time"}
    decision_at = recorded_at.timestamp()
    if not 0 < decision_at <= as_of:
        return {"status": "invalid_decision_time"}
    target_at = ceil(decision_at + horizon_minutes * 60)
    if as_of < target_at + 120:
        return {"status": "pending_horizon"}
    valid = [
        row
        for row in snapshots
        if type(row.get("captured_at")) is int
        and type(row.get("observed_at")) is int
        and 0 <= cast(int, row["captured_at"]) <= cast(int, row["observed_at"]) <= as_of
    ]
    # Captures only have second precision: the decision's own second is ambiguous.
    entries = [row for row in valid if cast(int, row["captured_at"]) < int(decision_at)]
    if not entries:
        return {"status": "entry_snapshot_missing"}
    entry = max(entries, key=lambda row: cast(int, row["captured_at"]))
    entry_at = cast(int, entry["captured_at"])
    if decision_at - entry_at > 120:
        return {"status": "entry_snapshot_stale"}
    if entry.get("pool") != pool:
        return {"status": "pool_changed"}
    targets = [row for row in valid if cast(int, row["captured_at"]) >= target_at]
    if not targets:
        return {"status": "target_snapshot_missing"}
    target = min(targets, key=lambda row: cast(int, row["captured_at"]))
    target_captured_at = cast(int, target["captured_at"])
    if target_captured_at - target_at > 120:
        return {"status": "target_snapshot_late"}
    if any(
        row.get("pool") != pool
        for row in valid
        if entry_at < cast(int, row["captured_at"]) <= target_captured_at
    ):
        return {"status": "pool_changed"}
    try:
        entry_price = Decimal(str(entry["price_usd"]))
        target_price = Decimal(str(target["price_usd"]))
        if (
            not entry_price.is_finite()
            or not target_price.is_finite()
            or entry_price <= 0
            or target_price <= 0
        ):
            raise InvalidOperation
        price_change_pct = round(float((target_price / entry_price - 1) * 100), 4)
    except (KeyError, TypeError, ValueError, InvalidOperation, OverflowError):
        return {"status": "invalid_snapshot_price"}
    return {
        "status": "observed_snapshot_proxy",
        "horizon_minutes": horizon_minutes,
        "entry_captured_at": entry_at,
        "target_captured_at": target_captured_at,
        "entry_age_seconds": round(decision_at - entry_at, 3),
        "target_delay_seconds": target_captured_at - target_at,
        "price_change_pct": price_change_pct,
    }


def summarize_snapshot_decision_paths(
    journal: TradeJournal,
    decisions_by_horizon: Mapping[int, list[dict[str, object]]],
    *,
    as_of: int,
) -> dict[str, object]:
    """Keep missing snapshot outcomes in the prospective decision denominator."""
    horizons: dict[str, dict[str, object]] = {}
    cache: dict[str, list[dict[str, object]]] = {}
    for horizon in (5, 15):
        decisions = decisions_by_horizon.get(horizon, [])
        rows: list[dict[str, object]] = []
        statuses: Counter[str] = Counter()
        all_reasons: Counter[str] = Counter()
        observed_reasons: Counter[str] = Counter()
        for decision in decisions:
            mint = decision.get("mint")
            if isinstance(mint, str) and mint not in cache:
                cache[mint] = journal.market_snapshots(mint)
            outcome = snapshot_decision_price_change(
                decision,
                cache.get(mint, []) if isinstance(mint, str) else [],
                horizon_minutes=horizon,
                as_of=as_of,
            )
            statuses[str(outcome["status"])] += 1
            final_reason = primary_decision_reason(
                decision.get("verdict"), decision.get("reasons"), decision.get("evidence")
            )
            if isinstance(final_reason, str):
                all_reasons[final_reason] += 1
            if outcome["status"] == "observed_snapshot_proxy" and isinstance(final_reason, str):
                observed_reasons[final_reason] += 1
            rows.append(
                {
                    "decision_id": decision.get("decision_id"),
                    "verdict": decision.get("verdict"),
                    "action": decision.get("action"),
                    "final_reason": final_reason,
                    "outcome": outcome,
                }
            )
        horizons[str(horizon)] = {
            "decision_count": len(decisions),
            "unique_mints": len({row.get("mint") for row in decisions}),
            "statuses": dict(statuses),
            "observed_final_reasons": dict(observed_reasons),
            "final_reason_coverage": {
                reason: {
                    "decisions": count,
                    "observed": observed_reasons[reason],
                    "coverage_pct": round(observed_reasons[reason] * 100 / count, 1),
                }
                for reason, count in all_reasons.items()
            },
            "rows": rows,
        }
    return {
        "scope": "latest_100_age_eligible_decisions_per_horizon_dex_snapshot_price_only",
        "limitations": (
            "Observed DEX snapshots can miss intrahorizon extremes and do not model "
            "entry execution, exit routing, slippage, fees, or trade P&L."
        ),
        "horizons": horizons,
    }


def decision_price_path(
    decision: dict[str, object],
    candles: list[Candle],
    *,
    horizon_minutes: int,
    as_of: int,
) -> dict[str, object]:
    """Measure a future OHLCV path only when every minute has trusted provenance."""
    if horizon_minutes not in (5, 15):
        raise ValueError("Opportunity horizon must be 5 or 15 minutes")
    evidence = decision.get("evidence")
    if (
        not isinstance(evidence, dict)
        or not isinstance(evidence.get("pool"), str)
        or not evidence["pool"]
    ):
        return {"status": "missing_pool"}
    try:
        recorded_at = datetime.fromisoformat(str(decision["created_at"]))
    except (KeyError, ValueError):
        return {"status": "invalid_decision_time"}
    if recorded_at.tzinfo is None:
        return {"status": "invalid_decision_time"}
    decision_at = recorded_at.timestamp()
    if not 0 < decision_at <= as_of:
        return {"status": "invalid_decision_time"}
    if any(right.opened_at <= left.opened_at for left, right in pairwise(candles)):
        return {"status": "invalid_candle_order"}
    opens = [candle.opened_at for candle in candles]
    first_index = bisect_right(opens, decision_at)
    first_expected = (int(decision_at) // 60 + 1) * 60
    horizon_end = first_expected + horizon_minutes * 60
    if as_of < horizon_end + 120:
        return {"status": "pending_horizon"}
    if first_index >= len(candles) or opens[first_index] != first_expected:
        return {"status": "entry_bar_missing"}
    path = candles[first_index : first_index + horizon_minutes]
    if len(path) != horizon_minutes or any(
        candle.opened_at != first_expected + offset * 60 for offset, candle in enumerate(path)
    ):
        return {"status": "future_bar_gap"}
    if any(candle.source != "geckoterminal_pool_trades" for candle in path):
        return {"status": "unverified_source"}
    max_capture_delay = 0
    for candle in path:
        values = (candle.open, candle.high, candle.low, candle.close)
        if (
            any(not isfinite(value) or value <= 0 for value in values)
            or candle.high < max(candle.open, candle.close)
            or candle.low > min(candle.open, candle.close)
        ):
            return {"status": "invalid_candle_values"}
        closed_at = candle.opened_at + 60
        if (
            candle.first_fetched_at is None
            or candle.first_observed_values_at is None
            or not closed_at <= candle.first_fetched_at <= as_of
            or not closed_at <= candle.first_observed_values_at <= as_of
        ):
            return {"status": "untrusted_capture"}
        max_capture_delay = max(
            max_capture_delay,
            candle.first_fetched_at - closed_at,
            candle.first_observed_values_at - closed_at,
        )
    entry = path[0].open
    return {
        "status": "observed_timely" if max_capture_delay <= 120 else "observed_backfilled",
        "proxy_entry_at": first_expected,
        "horizon_minutes": horizon_minutes,
        "max_capture_delay_seconds": max_capture_delay,
        "max_favorable_pct": round((max(candle.high for candle in path) / entry - 1) * 100, 4),
        "max_adverse_pct": round((min(candle.low for candle in path) / entry - 1) * 100, 4),
        "horizon_close_pct": round((path[-1].close / entry - 1) * 100, 4),
    }


def summarize_decision_followup_paths(
    journal: TradeJournal,
    *,
    as_of: int,
    limit: int = 50,
) -> dict[str, object]:
    """Audit a selected collection lane as price-only evidence, never trade PnL."""
    selections = journal.decision_followup_selections(limit=limit)
    cache: dict[tuple[str, str], list[Candle]] = {}
    statuses: dict[str, Counter[str]] = {"5m": Counter(), "15m": Counter()}
    statuses_by_method: dict[str, dict[str, Counter[str]]] = {}
    rows: list[dict[str, object]] = []
    for selection in selections:
        mint = str(selection["mint"])
        method = str(selection["selection_method"])
        if method not in statuses_by_method:
            statuses_by_method[method] = {"5m": Counter(), "15m": Counter()}
        selected_at = cast(int, selection["selected_at"])
        snapshot = journal.market_snapshot_before(mint, captured_at=selected_at)
        pool = snapshot.get("pool") if snapshot else None
        captured_at = snapshot.get("captured_at") if snapshot else None
        if not isinstance(pool, str) or not pool:
            unavailable = "pool_snapshot_unavailable_at_selection"
        elif not isinstance(captured_at, int) or selected_at - captured_at > 120:
            unavailable = "pool_snapshot_stale_at_selection"
        else:
            unavailable = None
        candles: list[Candle] = []
        if unavailable is None and isinstance(pool, str):
            key = (mint, pool)
            if key not in cache:
                cache[key] = load_verified_candles(journal, mint, pool, "1m", now=as_of)
            candles = cache[key]
        outcomes: dict[str, dict[str, object]] = {}
        for horizon in (5, 15):
            outcome: dict[str, object]
            if unavailable is not None:
                outcome = {"status": unavailable}
            else:
                outcome = decision_price_path(
                    {
                        "created_at": datetime.fromtimestamp(selected_at, UTC).isoformat(),
                        "evidence": {"pool": pool},
                    },
                    candles,
                    horizon_minutes=horizon,
                    as_of=as_of,
                )
            statuses[f"{horizon}m"][str(outcome["status"])] += 1
            statuses_by_method[method][f"{horizon}m"][str(outcome["status"])] += 1
            outcomes[f"{horizon}m"] = outcome
        rows.append(
            {
                "mint": mint,
                "selected_at": selected_at,
                "expires_at": selection["expires_at"],
                "population_count": selection["population_count"],
                "selection_method": selection["selection_method"],
                "pool_snapshot_captured_at": captured_at,
                "outcomes": outcomes,
            }
        )
    return {
        "scope": "prospectively_selected_market_gate_followup_price_only",
        "sample_size": len(rows),
        "statuses": {horizon: dict(counts) for horizon, counts in statuses.items()},
        "statuses_by_method": {
            method: {horizon: dict(counts) for horizon, counts in frames.items()}
            for method, frames in statuses_by_method.items()
        },
        "selections": rows,
    }


def summarize_recent_price_paths(
    journal: TradeJournal,
    decisions_by_horizon: Mapping[int, list[dict[str, object]]],
    *,
    as_of: int,
) -> dict[str, object]:
    """Use age-eligible decision cohorts and retain every missing outcome."""
    cache: dict[tuple[str, str], list[Candle]] = {}
    horizons: dict[str, dict[str, object]] = {}
    for horizon in (5, 15):
        decisions = decisions_by_horizon.get(horizon, [])
        rows: list[dict[str, Any]] = []
        statuses: Counter[str] = Counter()
        for decision in decisions:
            evidence = decision.get("evidence")
            mint = decision.get("mint")
            pool = evidence.get("pool") if isinstance(evidence, dict) else None
            candles: list[Candle] = []
            if isinstance(mint, str) and isinstance(pool, str) and pool:
                pair = (mint, pool)
                if pair not in cache:
                    cache[pair] = load_verified_candles(journal, mint, pool, "1m", now=as_of)
                candles = cache[pair]
            outcome = decision_price_path(decision, candles, horizon_minutes=horizon, as_of=as_of)
            statuses[str(outcome["status"])] += 1
            rows.append(
                {
                    "decision_id": decision.get("decision_id"),
                    "mint": decision.get("mint"),
                    "action": decision.get("action"),
                    "verdict": decision.get("verdict"),
                    "final_reason": primary_decision_reason(
                        decision.get("verdict"), decision.get("reasons"), evidence
                    ),
                    "market_gate_reasons": (
                        evidence.get("market_gate_reasons") if isinstance(evidence, dict) else None
                    ),
                    "outcome": outcome,
                }
            )
        horizons[str(horizon)] = {
            "decision_count": len(decisions),
            "unique_mints": len({str(row.get("mint")) for row in decisions}),
            "statuses": dict(statuses),
            "observed_final_reasons": dict(
                Counter(
                    str(row["final_reason"])
                    for row in rows
                    if str(row["outcome"]["status"]).startswith("observed_")
                    and isinstance(row["final_reason"], str)
                )
            ),
            "rows": rows,
        }
    return {
        "scope": "latest_100_age_eligible_decisions_per_horizon_price_only_non_executable",
        "horizons": horizons,
    }
