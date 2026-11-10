from __future__ import annotations

import uuid
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Literal

from .config import Settings
from .constants import MARKET_POLICY_VERSION, MAX_ABSOLUTE_24H_CHANGE_PCT
from .execution import (
    PaperExecutionError,
    paper_execution_failure_reason,
    paper_swap_input_lamports,
)
from .journal import JournalError, TradeJournal
from .operations import paper_exit_monitor_issues, paper_watchdog_issues
from .paper import PaperTradingService, StalePaperDecisionError
from .strategy_lab import (
    DEFAULT_SIGNAL_THRESHOLD,
    Timeframe,
    analyze_entry,
    load_verified_candles,
    multi_frame_entry_verdict,
)

MAX_NARRATIVE_AGE_SECONDS = 900
MAX_NARRATIVE_MENTION_AGE_SECONDS = 3_600


def market_policy_snapshot(settings: Settings) -> dict[str, int]:
    """The nonsecret market thresholds that must match a replayed paper cycle."""
    return {
        "minimum_liquidity_usd": settings.strategy_min_liquidity_usd,
        "minimum_volume_24h_usd": settings.strategy_min_volume_24h_usd,
        "maximum_absolute_24h_change_pct": MAX_ABSOLUTE_24H_CHANGE_PCT,
        "minimum_score": settings.strategy_min_score,
        "maximum_narrative_age_seconds": MAX_NARRATIVE_AGE_SECONDS,
        "maximum_narrative_mention_age_seconds": MAX_NARRATIVE_MENTION_AGE_SECONDS,
    }


@dataclass(frozen=True, slots=True)
class StrategyDecision:
    mint: str
    score: int
    verdict: str
    reasons: tuple[str, ...]
    evidence: dict[str, object] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def primary_decision_reason(verdict: object, reasons: object, evidence: object) -> str | None:
    """Report a blocking market gate before optional score explanations."""
    if verdict == "reject" and isinstance(evidence, dict):
        gates = evidence.get("market_gate_reasons")
        if isinstance(gates, (list, tuple)):
            for gate in gates:
                if isinstance(gate, str) and gate:
                    return gate
    if isinstance(reasons, (list, tuple)):
        for reason in reversed(reasons):
            if isinstance(reason, str) and reason:
                return reason
    return None


def _decimal(value: object) -> Decimal | None:
    if value in (None, ""):
        return None
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return parsed if parsed.is_finite() else None


def _observed_age_seconds(value: object, evaluated_at_ms: int) -> float | None:
    if not isinstance(value, str):
        return None
    try:
        observed = datetime.fromisoformat(value)
        if observed.tzinfo is None:
            return None
        return evaluated_at_ms / 1_000 - observed.timestamp()
    except (ValueError, OverflowError):
        return None


def open_position_liquidity_status(
    settings: Settings,
    journal: TradeJournal,
    open_mints: set[str],
    *,
    now: datetime | None = None,
) -> tuple[list[str], list[str]]:
    """Return thin and unverified open mints using fresh, matching pool snapshots."""
    current = (now or datetime.now(UTC)).timestamp()
    thin: list[str] = []
    unverified: list[str] = []
    for mint in sorted(open_mints):
        candidate = journal.discovery_candidate(mint)
        expected_pool = candidate.get("pair_address") if candidate else None
        snapshots = journal.market_snapshots(mint, limit=1)
        latest = snapshots[-1] if snapshots else None
        captured_at = latest.get("captured_at") if latest else None
        age = current - captured_at if isinstance(captured_at, int) else None
        if (
            not isinstance(expected_pool, str)
            or not expected_pool
            or latest is None
            or latest.get("pool") != expected_pool
            or age is None
            or not -30 <= age <= 180
        ):
            unverified.append(mint)
            continue
        liquidity = _decimal(latest.get("liquidity_usd"))
        if liquidity is None or liquidity < 0:
            unverified.append(mint)
        elif liquidity < settings.paper_liquidity_breaker_usd:
            thin.append(mint)
    return thin, unverified


def evaluate_candidate(
    candidate: dict[str, object],
    settings: Settings,
    *,
    now_ms: int | None = None,
) -> StrategyDecision:
    mint = str(candidate.get("mint") or "")
    reasons: list[str] = []
    gates: list[str] = []
    score = 0
    evaluated_at_ms = now_ms if now_ms is not None else int(datetime.now(UTC).timestamp() * 1_000)

    if candidate.get("audit_status") != "safe":
        gates.append("on-chain audit did not pass")
    else:
        score += 30
        reasons.append("on-chain audit passed (+30)")

    liquidity = _decimal(candidate.get("liquidity_usd"))
    minimum_liquidity = Decimal(settings.strategy_min_liquidity_usd)
    if liquidity is None:
        gates.append("liquidity is unavailable")
    elif liquidity < minimum_liquidity:
        gates.append(f"liquidity ${liquidity:,.0f} is below ${minimum_liquidity:,.0f} minimum")
    else:
        liquidity_ratio = liquidity / minimum_liquidity
        liquidity_points = (
            25
            if liquidity_ratio >= 10
            else 20
            if liquidity_ratio >= 5
            else 15
            if liquidity_ratio >= 2
            else 10
        )
        score += liquidity_points
        reasons.append(f"liquidity ${liquidity:,.0f} (+{liquidity_points})")

    volume = _decimal(candidate.get("volume_24h_usd"))
    minimum_volume = Decimal(settings.strategy_min_volume_24h_usd)
    if volume is None:
        gates.append("24h volume is unavailable")
    elif volume < minimum_volume:
        gates.append(f"24h volume ${volume:,.0f} is below ${minimum_volume:,.0f} minimum")
    elif liquidity is not None and liquidity > 0:
        turnover = volume / liquidity
        volume_points = (
            20
            if turnover >= 2
            else 15
            if turnover >= 1
            else 10
            if turnover >= Decimal("0.5")
            else 5
        )
        score += volume_points
        reasons.append(f"24h turnover {turnover:.2f}x (+{volume_points})")

    change = _decimal(candidate.get("price_change_24h"))
    if change is None:
        reasons.append("momentum unavailable (+0)")
    elif abs(change) > MAX_ABSOLUTE_24H_CHANGE_PCT:
        gates.append(f"24h move {change:+.1f}% exceeds manipulation guard")
    else:
        momentum_points = (
            15
            if Decimal("5") <= change <= Decimal("50")
            else 10
            if Decimal("0") <= change < Decimal("5")
            else 6
            if Decimal("-10") <= change < 0
            else 3
        )
        score += momentum_points
        reasons.append(f"24h momentum {change:+.1f}% (+{momentum_points})")

    created_at = candidate.get("pair_created_at")
    if isinstance(created_at, int) and created_at > 0:
        age_minutes = max(0, (evaluated_at_ms - created_at) // 60_000)
        if age_minutes < 10:
            reasons.append("pair is under 10 minutes old (+0)")
        elif age_minutes <= 7 * 24 * 60:
            score += 10
            reasons.append("pair age is inside the 10m-7d strategy window (+10)")
        else:
            score += 4
            reasons.append("pair is older than the primary strategy window (+4)")
    else:
        reasons.append("pair age unavailable (+0)")

    narrative_value = candidate.get("narrative_signal")
    narrative = narrative_value if isinstance(narrative_value, dict) else None
    if narrative:
        narrative_age = _observed_age_seconds(narrative.get("updated_at"), evaluated_at_ms)
        mention_age = _observed_age_seconds(narrative.get("latest_mention_at"), evaluated_at_ms)
        narrative_fresh = (
            narrative_age is not None and 0 <= narrative_age <= MAX_NARRATIVE_AGE_SECONDS
        )
        mention_recent = (
            mention_age is not None and 0 <= mention_age <= MAX_NARRATIVE_MENTION_AGE_SECONDS
        )
        confidence = str(narrative.get("confidence") or "low")
        explicit_mentions = narrative.get("explicit_contract_mentions")
        explicit_count = explicit_mentions if isinstance(explicit_mentions, int) else 0
        unique_sources_value = narrative.get("unique_sources")
        unique_sources = unique_sources_value if isinstance(unique_sources_value, int) else 0
        raw_flags = narrative.get("risk_flags")
        flags = {str(flag) for flag in raw_flags} if isinstance(raw_flags, list) else set()
        if (
            "security_claim_present" in flags
            and confidence == "high"
            and explicit_count > 0
            and unique_sources >= 2
        ):
            gates.append("corroborated contract-linked security claim requires manual review")
        elif not narrative_fresh:
            reasons.append("narrative observation is missing or older than 15 minutes (+0)")
        elif not mention_recent:
            reasons.append("latest underlying mention is missing or older than 1 hour (+0)")
        elif (
            confidence == "high"
            and explicit_count > 0
            and "promotion_dominant" not in flags
            and "social_only_evidence" not in flags
        ):
            score += 5
            reasons.append("multi-source contract-linked narrative confirmation (+5)")
        elif (
            confidence == "medium"
            and explicit_count > 0
            and "promotion_dominant" not in flags
            and "social_only_evidence" not in flags
        ):
            score += 2
            reasons.append("contract-linked narrative evidence is not fully corroborated (+2)")
        else:
            reasons.append("narrative evidence is weak, promotional, or ticker-linked (+0)")
    else:
        reasons.append("no token-linked narrative evidence (+0)")

    score = min(score, 100)
    market_verdict = (
        "reject" if gates else "eligible" if score >= settings.strategy_min_score else "observe"
    )
    evidence: dict[str, object] = {
        "strategy": "baseline_four_factor",
        "market_policy_version": MARKET_POLICY_VERSION,
        "market_evaluated_at_ms": evaluated_at_ms,
        "market_gate_verdict": market_verdict,
        "market_gate_score": score,
        "market_policy": market_policy_snapshot(settings),
        "market_gate_inputs": {
            "mint": mint,
            "audit_status": candidate.get("audit_status"),
            "liquidity_usd": candidate.get("liquidity_usd"),
            "volume_24h_usd": candidate.get("volume_24h_usd"),
            "price_change_24h": candidate.get("price_change_24h"),
            "pair_created_at": candidate.get("pair_created_at"),
            "narrative_signal": (
                {
                    "updated_at": narrative.get("updated_at"),
                    "latest_mention_at": narrative.get("latest_mention_at"),
                    "confidence": narrative.get("confidence"),
                    "explicit_contract_mentions": narrative.get("explicit_contract_mentions"),
                    "unique_sources": narrative.get("unique_sources"),
                    "risk_flags": narrative.get("risk_flags"),
                }
                if narrative
                else None
            ),
        },
        "market_gate_reasons": list(gates),
        "audit_status": candidate.get("audit_status"),
        "top10_concentration_bps": candidate.get("top10_concentration_bps"),
        "pool": candidate.get("pair_address"),
        "liquidity_usd": candidate.get("liquidity_usd"),
        "volume_24h_usd": candidate.get("volume_24h_usd"),
        "price_change_24h": candidate.get("price_change_24h"),
        "pair_created_at": candidate.get("pair_created_at"),
    }
    if gates:
        return StrategyDecision(mint, score, "reject", tuple([*gates, *reasons]), evidence)
    verdict = market_verdict
    reasons.append(
        f"score {score} {'meets' if verdict == 'eligible' else 'is below'} "
        f"the {settings.strategy_min_score} entry threshold"
    )
    return StrategyDecision(mint, score, verdict, tuple(reasons), evidence)


def replay_market_verdict(
    journal: TradeJournal,
    settings: Settings,
    *,
    mint: str,
    snapshot: dict[str, object],
    decision_at_ms: int,
) -> tuple[str, int | None] | None:
    """Apply the paper market scorer to inputs captured by a replay decision."""
    try:
        narrative = journal.narrative_signal_before(mint, decision_at_us=decision_at_ms * 1_000)
    except JournalError:
        return "narrative_history_invalid", None
    if narrative is None:
        return None
    try:
        recorded_policy = journal.market_policy_before(mint, decision_at_us=decision_at_ms * 1_000)
    except JournalError:
        return "market_policy_invalid", None
    if recorded_policy is None:
        return "market_policy_missing", None
    observed_at_us = recorded_policy.get("observed_at_us")
    if type(observed_at_us) is not int or not 0 <= decision_at_ms * 1_000 - observed_at_us <= (
        2 * settings.paper_automation_interval_seconds * 1_000_000
    ):
        return "market_policy_stale", None
    stored_values = recorded_policy.get("policy")
    if (
        type(recorded_policy.get("version")) is not int
        or recorded_policy["version"] != MARKET_POLICY_VERSION
        or not isinstance(stored_values, dict)
        or any(type(value) is not int for value in stored_values.values())
        or stored_values != market_policy_snapshot(settings)
    ):
        return "market_policy_mismatch", None
    candidate: dict[str, object] = {
        "mint": mint,
        "audit_status": "safe",  # The replay separately verifies the timestamped audit.
        "liquidity_usd": snapshot.get("liquidity_usd"),
        "volume_24h_usd": snapshot.get("volume_24h_usd"),
        "price_change_24h": snapshot.get("price_change_24h"),
        "pair_created_at": snapshot.get("pair_created_at"),
        "narrative_signal": narrative.get("signal"),
    }
    decision = evaluate_candidate(candidate, settings, now_ms=decision_at_ms)
    return decision.verdict, decision.score


def make_replay_market_scorer(
    journal: TradeJournal, settings: Settings, mint: str
) -> Callable[[dict[str, object], int], tuple[str, int | None] | None]:
    def score(snapshot: dict[str, object], decision_at_ms: int) -> tuple[str, int | None] | None:
        return replay_market_verdict(
            journal,
            settings,
            mint=mint,
            snapshot=snapshot,
            decision_at_ms=decision_at_ms,
        )

    return score


def attempt_paper_entry(
    *,
    settings: Settings,
    paper: PaperTradingService,
    journal: TradeJournal,
    decision: StrategyDecision,
    entry_source: Literal["autonomous", "call_inbox"] = "autonomous",
) -> tuple[str, dict[str, Any] | None]:
    if decision.verdict != "eligible":
        return ("rejected" if decision.verdict == "reject" else "observed"), None
    breakers = paper_entry_breakers(settings, journal)
    if breakers:
        return f"circuit_breaker:{breakers[0]}", None
    open_positions = [
        position for position in journal.paper_positions() if position["status"] == "open"
    ]
    if decision.mint in {str(position["mint"]) for position in open_positions}:
        return "duplicate_skipped", None
    if len(open_positions) >= settings.strategy_max_open_positions:
        return "position_limit", None
    cash = journal.paper_cash_lamports(settings.paper_starting_lamports)
    size = paper_swap_input_lamports(settings, cash)
    if size < 1_000_000:
        return "insufficient_paper_risk_budget", None
    try:
        opened = paper.open(
            mint=decision.mint,
            lamports=size,
            slippage_bps=100,
            decision=decision,
            entry_source=entry_source,
        )
    except PaperExecutionError as exc:
        return f"simulation_failed:PaperExecutionError:{paper_execution_failure_reason(exc)}", None
    except StalePaperDecisionError:
        return "decision_changed_before_entry", None
    except Exception as exc:
        return f"simulation_failed:{type(exc).__name__}", None
    return "paper_opened", opened


def paper_entry_breakers(settings: Settings, journal: TradeJournal) -> list[str]:
    """Pause every paper entry path when portfolio evidence or risk is unsafe."""
    reasons: list[str] = []
    now = datetime.now(UTC)
    campaign = journal.runtime_state("paper_campaign") or {}
    if campaign.get("mode") == "forward_paper_only":
        if campaign.get("status") == "interrupted":
            reasons.append("paper_campaign_interrupted")
        else:
            reasons.extend(
                paper_exit_monitor_issues(
                    journal.runtime_state("paper_exit_monitor") or {},
                    settings.paper_exit_interval_seconds,
                    now=now,
                )
            )
            reasons.extend(
                paper_watchdog_issues(journal.runtime_state("paper_watchdog") or {}, now=now)
            )
    risk = journal.paper_risk_metrics()
    daily_loss_limit = settings.paper_starting_lamports * settings.paper_daily_loss_bps // 10_000
    if int(risk["daily_realized_pnl_lamports"]) <= -daily_loss_limit:
        reasons.append("daily_loss_limit")
    if int(risk["consecutive_losses"]) >= settings.paper_max_consecutive_losses:
        reasons.append("consecutive_loss_limit")

    open_positions = [p for p in journal.paper_positions() if p["status"] == "open"]
    if len(open_positions) >= settings.strategy_max_open_positions:
        reasons.append("position_limit")
    marked_value = 0
    marks_fresh = True
    for position in open_positions:
        mark = position.get("last_value_lamports")
        quoted_at = position.get("last_quoted_at")
        try:
            mark_time = datetime.fromisoformat(str(quoted_at))
        except ValueError:
            mark_time = None
        mark_age = (now - mark_time).total_seconds() if mark_time and mark_time.tzinfo else None
        if not isinstance(mark, int) or mark <= 0 or mark_age is None or not -30 <= mark_age <= 180:
            marks_fresh = False
        else:
            marked_value += mark

    thin, unverified = open_position_liquidity_status(
        settings, journal, {str(position["mint"]) for position in open_positions}, now=now
    )
    if thin:
        reasons.append("open_position_liquidity_limit")
    if unverified:
        reasons.append("open_position_liquidity_unverified")
    if not marks_fresh:
        reasons.append("open_position_mark_stale")
    else:
        cash = journal.paper_cash_lamports(settings.paper_starting_lamports)
        equity = cash + marked_value
        max_drawdown = settings.paper_starting_lamports * settings.paper_max_drawdown_bps // 10_000
        if settings.paper_starting_lamports - equity >= max_drawdown:
            reasons.append("portfolio_drawdown_limit")
    return reasons


def confirm_candidate_candles(
    candidate: dict[str, object],
    decision: StrategyDecision,
    settings: Settings,
    journal: TradeJournal,
) -> StrategyDecision:
    """Apply the same closed-candle entry gate to autonomous and pasted calls."""
    if decision.verdict != "eligible":
        return decision
    pool = candidate.get("pair_address")
    if not isinstance(pool, str) or not pool:
        return StrategyDecision(
            decision.mint,
            min(decision.score, settings.strategy_min_score - 1),
            "observe",
            tuple([*decision.reasons, "Verified pool address is unavailable"]),
            decision.evidence,
        )
    now = datetime.now(UTC)
    last_seen = candidate.get("last_seen")
    try:
        audited_at = datetime.fromisoformat(str(last_seen))
    except ValueError:
        audited_at = None
    audit_age = (now - audited_at).total_seconds() if audited_at and audited_at.tzinfo else None
    if audit_age is None or not -60 <= audit_age <= 600:
        return StrategyDecision(
            decision.mint,
            min(decision.score, settings.strategy_min_score - 1),
            "observe",
            tuple([*decision.reasons, "On-chain audit is missing or older than 10 minutes"]),
            {**decision.evidence, "audit_last_seen": last_seen},
        )
    snapshots = journal.market_snapshots(decision.mint)
    current_snapshot = snapshots[-1] if snapshots else None
    captured_at = current_snapshot.get("captured_at") if current_snapshot else None
    market_age = now.timestamp() - captured_at if isinstance(captured_at, int) else None
    if (
        current_snapshot is None
        or current_snapshot.get("pool") != pool
        or market_age is None
        or not -30 <= market_age <= 180
    ):
        return StrategyDecision(
            decision.mint,
            min(decision.score, settings.strategy_min_score - 1),
            "observe",
            tuple([*decision.reasons, "Pool liquidity and market snapshot are missing or stale"]),
            {**decision.evidence, "audit_last_seen": last_seen, "market_captured_at": captured_at},
        )
    frames: tuple[Timeframe, ...] = ("1m", "5m", "15m", "1h")
    candle_sets = {
        frame: load_verified_candles(journal, decision.mint, pool, frame) for frame in frames
    }
    as_of = int(now.timestamp())
    signals = {
        timeframe: analyze_entry(
            candle_sets[timeframe],
            minimum_liquidity_usd=float(settings.strategy_min_liquidity_usd),
            timeframe=timeframe,
            signal_threshold=DEFAULT_SIGNAL_THRESHOLD,
            as_of=as_of,
        )
        for timeframe in frames
    }
    signal_evidence: dict[str, object] = {
        **decision.evidence,
        "decision_as_of": as_of,
        "audit_last_seen": last_seen,
        "market_captured_at": captured_at,
        "candle_policy": {
            "minimum_liquidity_usd": str(settings.strategy_min_liquidity_usd),
            "signal_threshold": DEFAULT_SIGNAL_THRESHOLD,
        },
        "candle_signals": {frame: signals[frame].as_dict() for frame in frames},
        "latest_closed_candle_at": {
            frame: candle_sets[frame][-1].opened_at if candle_sets[frame] else None
            for frame in frames
        },
    }
    reasons = list(decision.reasons)
    eligible, candle_reason = multi_frame_entry_verdict(signals)
    reasons.append(candle_reason)
    verdict = "eligible" if eligible else "observe"
    required = (signals["1m"], signals["5m"])
    score = (
        min(100, (decision.score + required[0].score + required[1].score) // 3)
        if verdict == "eligible"
        else min(decision.score, settings.strategy_min_score - 1)
    )
    return StrategyDecision(decision.mint, score, verdict, tuple(reasons), signal_evidence)


def refreshed_paper_entry_opportunities(
    settings: Settings, journal: TradeJournal, refreshed_mints: Sequence[str]
) -> list[str]:
    """Record narrow prospective signals and find confirmed paper opportunities."""
    requested = {mint for mint in refreshed_mints if isinstance(mint, str) and mint}
    if not requested:
        return []
    candidates = journal.discovery_candidates(limit=100)
    opportunities: list[str] = []
    for candidate in candidates:
        if candidate.get("mint") not in requested:
            continue
        decision = confirm_candidate_candles(
            candidate, evaluate_candidate(candidate, settings), settings, journal
        )
        journal.record_collector_signal_observation(decision)
        if decision.verdict == "eligible":
            opportunities.append(decision.mint)
    return opportunities


def run_paper_decision_cycle(
    *,
    settings: Settings,
    paper: PaperTradingService,
    journal: TradeJournal,
    forced_breaker_reasons: Sequence[str] = (),
) -> dict[str, Any]:
    # Continue observing signals during an entry lock, without invoking execution.
    cycle_id = str(uuid.uuid4())
    cycle_started_at = datetime.now(UTC).isoformat()
    breaker_reasons = list(
        dict.fromkeys([*forced_breaker_reasons, *paper_entry_breakers(settings, journal)])
    )
    candidates = journal.discovery_candidates(limit=100)
    decisions: list[StrategyDecision] = []
    for candidate in candidates:
        decisions.append(
            confirm_candidate_candles(
                candidate, evaluate_candidate(candidate, settings), settings, journal
            )
        )
    decisions.sort(key=lambda decision: decision.score, reverse=True)
    opened: dict[str, Any] | None = None
    ranked_decisions: list[StrategyDecision] = []

    for rank, initial_decision in enumerate(decisions, start=1):
        decision = StrategyDecision(
            initial_decision.mint,
            initial_decision.score,
            initial_decision.verdict,
            initial_decision.reasons,
            {
                **initial_decision.evidence,
                "decision_cycle_id": cycle_id,
                "decision_cycle_started_at": cycle_started_at,
                "decision_cycle_rank": rank,
                "decision_cycle_size": len(decisions),
                "decision_cycle_breakers": list(breaker_reasons),
            },
        )
        ranked_decisions.append(decision)
        if breaker_reasons:
            action = (
                f"circuit_breaker:{breaker_reasons[0]}"
                if decision.verdict == "eligible"
                else "rejected"
                if decision.verdict == "reject"
                else "observed"
            )
            candidate_opened = None
        elif opened:
            action = (
                "deferred_after_paper_open"
                if decision.verdict == "eligible"
                else "rejected"
                if decision.verdict == "reject"
                else "observed"
            )
            candidate_opened = None
        else:
            action, candidate_opened = attempt_paper_entry(
                settings=settings,
                paper=paper,
                journal=journal,
                decision=decision,
            )
        position = candidate_opened.get("position") if candidate_opened else None
        if position is None:
            journal.record_strategy_decision(decision, action=action)
        if candidate_opened:
            opened = candidate_opened

    return {
        "cycle_id": cycle_id,
        "evaluated": len(decisions),
        "opened": opened,
        "entry_paused": bool(breaker_reasons),
        "breaker_reasons": breaker_reasons,
        "decisions": [decision.as_dict() for decision in ranked_decisions],
    }
