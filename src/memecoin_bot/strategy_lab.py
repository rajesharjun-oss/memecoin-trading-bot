from __future__ import annotations

import hashlib
import time
from bisect import bisect_right
from collections import Counter
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from itertools import pairwise, zip_longest
from statistics import mean
from typing import Any, Literal

import httpx

from .constants import MAX_ABSOLUTE_24H_CHANGE_PCT
from .exit_policy import ExitPolicy, evaluate_exit_rule
from .hot_radar import evaluate_hot_market
from .journal import TradeJournal
from .market_data import DexScreenerClient, MarketDataError
from .ohlcv import (
    PUBLIC_REQUEST_INTERVAL_SECONDS,
    TIMEFRAME_SECONDS,
    TRIAL_REQUEST_INTERVAL_SECONDS,
    GeckoTerminalOhlcvClient,
    OhlcvError,
    OhlcvRateLimited,
    PoolMarketObservation,
)

Timeframe = Literal["1m", "5m", "15m", "1h"]
FactorName = Literal["trend", "momentum", "volume", "liquidity"]
DEFAULT_SIGNAL_THRESHOLD = 75
RESEARCH_COHORT_KEY = "research_cohort_v2"
RESEARCH_COHORT_SECONDS = 10_800
RESEARCH_DISCOVERY_MAX_AGE_SECONDS = 3_600
RESEARCH_SELECTION_METHOD = "sha256_3h_recent_60m_first_seen_v2"
RESEARCH_FOCUS_SIZE = 2
LEGACY_RESEARCH_FOCUS_SIZE = 8
HIGHER_BUDGET_RETRY_SECONDS = 86_400
DECISION_FOLLOWUP_KEY = "decision_followup_v1"
DECISION_FOLLOWUP_SECONDS = 3_600
DECISION_FOLLOWUP_METHOD = "sha256_60m_market_eligible_v2"
# Persisted replay compatibility includes candle timing and exit liquidity semantics.
CANDLE_PROVENANCE_VERSION = "observed_signal_regime_v17"
HIGHER_FRAME_INTEGRITY_FAILURES = frozenset(
    {"conflicting_prices", "mixed_provenance", "revised_values", "unverified_values"}
)


def research_cohort_mints(journal: TradeJournal) -> list[str]:
    state = journal.runtime_state(RESEARCH_COHORT_KEY) or {}
    raw = state.get("mints")
    if not isinstance(raw, list):
        return []
    return list(dict.fromkeys(mint for mint in raw if isinstance(mint, str) and mint))[:20]


def research_focus_size(journal: TradeJournal) -> int:
    """Keep an existing cohort's collection policy fixed until it expires."""
    state = journal.runtime_state(RESEARCH_COHORT_KEY) or {}
    value = state.get("focus_size")
    return (
        value
        if type(value) is int and value in (RESEARCH_FOCUS_SIZE, LEGACY_RESEARCH_FOCUS_SIZE)
        else LEGACY_RESEARCH_FOCUS_SIZE
    )


def _ohlcv_error_code(error: OhlcvError) -> str:
    if isinstance(error, OhlcvRateLimited):
        return "rate_limit"
    message = str(error)
    if message.endswith("HTTP 404"):
        return "pool_not_found"
    if message.startswith("OHLCV provider returned HTTP "):
        return "provider_http_error"
    if message == "Provider pool does not contain the requested token":
        return "pool_identity_mismatch"
    if message == "OHLCV provider is unavailable":
        return "provider_unavailable"
    return "invalid_ohlcv"


def select_research_cohort(
    journal: TradeJournal, *, limit: int = 20, now: int | None = None
) -> list[str]:
    """Persist a three-hour prospective sample independent of momentum or audit verdict."""
    if not 1 <= limit <= 20:
        raise StrategyLabError("Research cohort size must be between 1 and 20")
    current = int(time.time()) if now is None else now
    state = journal.runtime_state(RESEARCH_COHORT_KEY) or {}
    raw_selected_at = state.get("selected_at")
    selected_at = raw_selected_at if isinstance(raw_selected_at, int) else current
    existing = research_cohort_mints(journal)
    valid = (
        isinstance(raw_selected_at, int) and 0 <= current - selected_at < RESEARCH_COHORT_SECONDS
    )
    if not valid:
        selected_at = current
        existing = []
    focus_size = research_focus_size(journal) if valid else RESEARCH_FOCUS_SIZE
    population: list[str] = []
    for mint in journal.recent_discovery_mints(limit=100):
        candidate = journal.discovery_candidate(mint)
        try:
            first_seen = datetime.fromisoformat(str(candidate["first_seen"])) if candidate else None
            observed_at = int(first_seen.timestamp()) if first_seen and first_seen.tzinfo else -1
        except (TypeError, ValueError, OverflowError):
            continue
        if current - RESEARCH_DISCOVERY_MAX_AGE_SECONDS <= observed_at <= current:
            population.append(mint)
    ranked = sorted(
        population,
        key=lambda mint: hashlib.sha256(
            f"{selected_at // RESEARCH_COHORT_SECONDS}:{mint}".encode()
        ).digest(),
    )
    selected = list(dict.fromkeys([*existing, *ranked]))[:limit]
    # Record the *actual* time each mint entered the sample. This also conservatively
    # backfills a cohort created before the membership table existed.
    journal.record_research_selection(
        selected,
        cohort_started_at=selected_at,
        selected_at=current,
        expires_at=selected_at + RESEARCH_COHORT_SECONDS,
        population_count=len(population),
        method=RESEARCH_SELECTION_METHOD,
    )
    if selected != existing or not valid:
        journal.set_runtime_state(
            RESEARCH_COHORT_KEY,
            {
                "selected_at": selected_at,
                "expires_at": selected_at + RESEARCH_COHORT_SECONDS,
                "mints": selected,
                "population_count": len(population),
                "selection_method": RESEARCH_SELECTION_METHOD,
                "focus_size": focus_size,
            },
        )
    return selected


def select_decision_followup(
    journal: TradeJournal, ready_mints: list[str], *, now: int | None = None
) -> list[str]:
    """Fix one market-eligible subject long enough to mature eight 5m bars."""
    current = int(time.time()) if now is None else now
    state = journal.runtime_state(DECISION_FOLLOWUP_KEY) or {}
    previous_mint = state.get("mint")
    previous_at = state.get("selected_at")
    previous_expires_at = state.get("expires_at")
    if (
        isinstance(previous_mint, str)
        and previous_mint
        and isinstance(previous_at, int)
        and not isinstance(previous_at, bool)
        and isinstance(previous_expires_at, int)
        and not isinstance(previous_expires_at, bool)
        and 0 <= previous_at <= current < previous_expires_at
    ):
        return [previous_mint]
    population = list(dict.fromkeys(mint for mint in ready_mints if mint))
    if not population:
        return []
    selected = min(
        population,
        key=lambda mint: hashlib.sha256(
            f"{current // DECISION_FOLLOWUP_SECONDS}:{mint}".encode()
        ).digest(),
    )
    expires_at = current + DECISION_FOLLOWUP_SECONDS
    journal.record_decision_followup_selection(
        selected,
        selected_at=current,
        expires_at=expires_at,
        population_count=len(population),
        method=DECISION_FOLLOWUP_METHOD,
    )
    journal.set_runtime_state(
        DECISION_FOLLOWUP_KEY,
        {
            "mint": selected,
            "selected_at": current,
            "expires_at": expires_at,
            "population_count": len(population),
            "selection_method": DECISION_FOLLOWUP_METHOD,
        },
    )
    return [selected]


def fetch_research_pool_markets(requested: dict[str, str]) -> list[PoolMarketObservation]:
    """Keep the optional pool probe independently replaceable in offline tests."""
    with GeckoTerminalOhlcvClient() as client:
        return client.pool_markets(requested)


def collect_market_snapshots(
    journal: TradeJournal,
    *,
    limit: int = 20,
    active_candidate_priority: Callable[[dict[str, object]], bool] | None = None,
) -> dict[str, object]:
    collection_started_at = int(time.time())
    open_mints = [
        str(position["mint"])
        for position in journal.paper_positions(limit=100)
        if position.get("status") == "open"
    ]
    cohort_mints = select_research_cohort(journal, limit=limit)
    # Keep the score-independent research sample intact, while also supplying
    # closed candles to currently safe paper candidates outside that sample.
    safe_candidates = [
        candidate
        for candidate in journal.discovery_candidates(limit=100)
        if candidate.get("audit_status") == "safe" and candidate.get("pair_address")
    ]
    safe_watchlist = [str(candidate["mint"]) for candidate in safe_candidates]
    # Market-eligible paper candidates need contemporaneous liquidity snapshots
    # even when they rank below the first 30 addresses in the general watchlist.
    priority_safe = [
        str(candidate["mint"])
        for candidate in safe_candidates
        if active_candidate_priority is not None and active_candidate_priority(candidate)
    ]
    # DEX Screener accepts 30 addresses per request. Two bounded requests keep
    # both the active paper lane and the score-independent research cohort fed.
    existing_followup = select_decision_followup(journal, [])
    mints = list(
        dict.fromkeys(
            [*open_mints, *existing_followup, *priority_safe, *cohort_mints, *safe_watchlist]
        )
    )[:60]
    if not mints:
        return {"collected": 0, "tracked": 0}
    errors: list[str] = []
    error_codes: Counter[str] = Counter()
    markets = {}
    dex_attempted = 0
    dex_succeeded = 0
    try:
        with DexScreenerClient() as client:
            for start in range(0, len(mints), 30):
                dex_attempted += 1
                try:
                    markets.update(client.tokens(mints[start : start + 30]))
                    dex_succeeded += 1
                except (MarketDataError, httpx.HTTPError) as exc:
                    errors.append(
                        f"DEX Screener unavailable (batch {start // 30 + 1}, "
                        f"{type(exc).__name__}); cached pools only"
                    )
    except (MarketDataError, httpx.HTTPError) as exc:
        errors.append(f"DEX Screener unavailable ({type(exc).__name__}); cached pools only")
    for selected_market in markets.values():
        if selected_market.price_usd is None or selected_market.liquidity_usd is None:
            continue
        market_payload = selected_market.as_dict()
        journal.update_candidate_market(
            mint=selected_market.mint,
            market=market_payload,
            hot_signal=evaluate_hot_market(market_payload).as_dict(),
        )
    # Complete closed 1m bars can form exact 5m buckets, so scarce requests
    # go to active 1m and occasional direct 15m/1h bars. A sustained backlog
    # may cautiously try six calls at a ten-second pace; a provider rate limit
    # immediately restores the four-call, fifteen-second schedule.
    stored = 0
    ohlcv_attempted = 0
    ohlcv_succeeded = 0
    ohlcv_cache_hits = 0
    refreshed_mints: set[str] = set()
    research_secondary_attempted = 0
    research_secondary_succeeded = 0
    empty_responses = 0
    stale_responses = 0
    # Scarce 1m requests go first to audited candidates that already clear the
    # strategy's market-only gates. This is collection priority, not approval;
    # the independent research cohort and every paper entry gate remain intact.
    ready_mints: list[str] = []
    refreshed_candidates: dict[str, dict[str, object]] = {}
    if active_candidate_priority is not None:
        # Re-read the full candidate record after DEX enrichment; the compact
        # single-mint lookup intentionally omits liquidity and volume.
        refreshed_candidates = {
            str(candidate["mint"]): candidate
            for candidate in journal.discovery_candidates(limit=100)
        }
        for original in safe_candidates:
            candidate = refreshed_candidates.get(str(original["mint"]))
            if candidate is not None and active_candidate_priority(candidate):
                ready_mints.append(str(candidate["mint"]))
    followup_mints = select_decision_followup(journal, ready_mints)
    # Keep every market-ready mint eligible for oldest-fetch scheduling. The
    # former fixed four-item slice repeatedly selected the same candidates
    # and left later eligible mints without a current 1m candle.
    active_mints = list(dict.fromkeys([*ready_mints, *safe_watchlist]))[: max(4, len(ready_mints))]
    budget_state = journal.runtime_state("ohlcv_request_budget") or {}
    raw_budget = budget_state.get("limit")
    max_request_budget = max(4, min(6, int(60 // TRIAL_REQUEST_INTERVAL_SECONDS)))
    request_budget = (
        min(raw_budget, max_request_budget)
        if isinstance(raw_budget, int) and raw_budget in (4, 6, 8)
        else 4
    )
    raw_fresh_successes = budget_state.get("fresh_network_successes")
    fresh_network_successes = (
        raw_fresh_successes
        if type(raw_fresh_successes) is int and 0 <= raw_fresh_successes < 12
        else 0
    )
    raw_fresh_window = budget_state.get("fresh_window_started_at")
    fresh_window_started_at = (
        raw_fresh_window
        if type(raw_fresh_window) is int and 0 <= int(time.time()) - raw_fresh_window <= 600
        else 0
    )
    if fresh_window_started_at == 0:
        fresh_network_successes = 0
    raw_retry_after = budget_state.get("higher_budget_retry_after")
    higher_budget_retry_after = (
        raw_retry_after
        if isinstance(raw_retry_after, int)
        and not isinstance(raw_retry_after, bool)
        and raw_retry_after > 0
        else 0
    )
    # Preserve quota evidence from the preceding collector run, including an
    # older persisted one-hour cooldown. A six-call trial that lands a 429
    # must not be retried hourly at the cost of missed baseline candles.
    raw_recent_cycles = budget_state.get("recent_cycles")
    recent_quota_hits = (
        [
            row["at"]
            for row in raw_recent_cycles
            if isinstance(row, dict)
            and type(row.get("at")) is int
            and type(row.get("attempted")) is int
            and row["attempted"] > 0
            and row.get("rate_limited") is True
            and 0 <= int(time.time()) - row["at"] < HIGHER_BUDGET_RETRY_SECONDS
        ]
        if isinstance(raw_recent_cycles, list)
        else []
    )
    if recent_quota_hits:
        higher_budget_retry_after = max(
            higher_budget_retry_after,
            max(recent_quota_hits) + HIGHER_BUDGET_RETRY_SECONDS,
        )
    if higher_budget_retry_after > int(time.time()):
        request_budget = 4
        fresh_network_successes = 0
        fresh_window_started_at = 0
    if len(ready_mints) < 8:
        request_budget = 4
        fresh_network_successes = 0
        fresh_window_started_at = 0
    # A new cohort concentrates its normal-cycle research call on two
    # score-independent mints. Existing eight-mint cohorts retain their policy
    # until expiry, so a cohort is not silently relabeled mid-window.
    focus_size = research_focus_size(journal)
    research_focus = cohort_mints[:focus_size]
    schedule = journal.runtime_state("ohlcv_schedule") or {}
    raw_cycle = schedule.get("cycle")
    cycle = raw_cycle if isinstance(raw_cycle, int) and raw_cycle >= 0 else 0

    def pool_for(mint: str) -> str | None:
        market = markets.get(mint)
        cached = journal.discovery_candidate(mint)
        pool = (
            market.pair_address
            if market and market.pair_address
            else (cached.get("pair_address") if cached else None)
        )
        return pool if isinstance(pool, str) and pool else None

    retry_state = (
        journal.runtime_state("ohlcv_retry_cooldown")
        or journal.runtime_state("ohlcv_empty_cooldown")
        or {}
    )
    raw_retry = retry_state.get("entries")
    now_epoch = int(time.time())
    retry_until = (
        {
            key: until
            for key, until in raw_retry.items()
            if isinstance(key, str)
            and isinstance(until, int)
            and not isinstance(until, bool)
            and until > now_epoch
        }
        if isinstance(raw_retry, dict)
        else {}
    )

    def retry_key(mint: str, timeframe: str) -> str | None:
        pool = pool_for(mint)
        return f"{mint}:{pool}:{timeframe}" if pool else None

    def has_candle_slot(mint: str, timeframe: str = "1m") -> bool:
        key = retry_key(mint, timeframe)
        return key is not None and key not in retry_until

    def oldest_fetch(mint: str, frames: tuple[str, ...]) -> int:
        pool = pool_for(mint)
        if pool is None:
            return 0
        fetched: list[int] = []
        for frame in frames:
            rows = journal.pool_candles(mint, pool, frame, limit=1)
            value = rows[0].get("fetched_at") if rows else None
            fetched.append(value if isinstance(value, int) else 0)
        return min(fetched)

    def has_recent_minute_trade(mint: str) -> bool:
        pool = pool_for(mint)
        if pool is None:
            return False
        rows = journal.pool_candles(mint, pool, "1m", limit=1)
        opened_at = rows[0].get("opened_at") if rows else None
        return type(opened_at) is int and now_epoch - 300 <= opened_at <= now_epoch

    def explicitly_inactive(mint: str) -> bool:
        market = markets.get(mint)
        if (
            market is None
            or market.pair_address is None
            or market.price_usd is None
            or market.liquidity_usd is None
            or market.volume_5m_usd is None
        ):
            return False
        try:
            zero_volume = Decimal(market.volume_5m_usd) == 0
        except InvalidOperation:
            return False
        return (
            zero_volume
            and market.txns_5m_buys + market.txns_5m_sells == 0
            and not has_recent_minute_trade(mint)
        )

    inactive_5m_mints = {mint for mint in mints if explicitly_inactive(mint)}

    short_frames: tuple[str, ...] = ("1m",)
    deep_frames: tuple[str, ...] = ("15m", "1h")
    focus_with_slot = [mint for mint in research_focus if has_candle_slot(mint)]
    focus_available = [mint for mint in focus_with_slot if mint not in inactive_5m_mints]
    remaining_cohort_set = set(cohort_mints[focus_size:])
    remaining_cohort = [
        mint
        for mint in cohort_mints[focus_size:]
        if has_candle_slot(mint) and mint not in inactive_5m_mints
    ]
    # Membership stays fixed and score-independent. Use the current DEX batch
    # only to schedule scarce OHLCV requests before inactive sampled pools.
    # Every inactive member remains in the cohort and in availability reports.
    active_research = {
        mint
        for mint in cohort_mints
        if (market := markets.get(mint)) is not None
        and market.txns_5m_buys + market.txns_5m_sells > 0
    }
    active_focus = [mint for mint in focus_available if mint in active_research]
    active_remaining = [mint for mint in remaining_cohort if mint in active_research]
    # Alternate which active research lane gets the first research request.
    # A public quota can interrupt after two calls, so fixed ordering would
    # systematically starve the secondary cohort even when it is trading.
    active_research_lanes = (
        (active_focus, active_remaining) if cycle % 2 == 0 else (active_remaining, active_focus)
    )
    secondary = [
        mint
        for mint in dict.fromkeys([*remaining_cohort, *open_mints])
        if has_candle_slot(mint) and mint not in inactive_5m_mints
    ]
    active_available = [
        mint for mint in active_mints if has_candle_slot(mint) and mint not in inactive_5m_mints
    ]

    def has_recent_revised_candle(mint: str) -> bool:
        pool = pool_for(mint)
        if pool is None:
            return False
        if not journal.pool_has_revision_since(
            mint,
            pool,
            opened_since=now_epoch - 9 * TIMEFRAME_SECONDS["1h"] - 120,
            observed_by=now_epoch,
        ):
            return False
        for timeframe in ("1m", "5m", "15m", "1h"):
            candles = load_verified_candles(journal, mint, pool, timeframe, now=now_epoch)
            if (
                analyze_entry(
                    candles,
                    minimum_liquidity_usd=0,
                    timeframe=timeframe,
                    as_of=now_epoch,
                ).status
                == "revised_values"
            ):
                return True
        return False

    # Keep scarce focus calls on pools that could recover a clean confirmation
    # window. This changes observation priority only; the entry gate still
    # checks every candle and the independent research cohort is unchanged.
    revision_excluded = {mint for mint in active_available if has_recent_revised_candle(mint)}
    # A rotating oldest-fetch schedule cannot give any one mint the eight timely
    # 1m bars needed by the entry gate when the public feed permits four calls
    # per minute. Pin at most two market-eligible mints for one hour. This is a
    # live collection lane, separate from the score-independent replay cohort.
    focus_state = journal.runtime_state("ohlcv_active_focus") or {}
    raw_focus = focus_state.get("mints")
    prior_focus = (
        list(dict.fromkeys(mint for mint in raw_focus if isinstance(mint, str)))[:2]
        if isinstance(raw_focus, list)
        else []
    )
    raw_selected_at = focus_state.get("selected_at")
    raw_recent = focus_state.get("last_selected_at")
    recently_selected = (
        {
            mint: stamp
            for mint, stamp in raw_recent.items()
            if isinstance(mint, str) and type(stamp) is int and 0 <= stamp <= now_epoch
        }
        if isinstance(raw_recent, dict)
        else {}
    )
    # Fewer than five pool transactions in five minutes cannot produce five
    # trade-backed 1m bars, regardless of our request cadence. Retire a quiet
    # pin only after three consecutive fresh market observations, preserving
    # the fixed research cohort and every trading gate.
    raw_low_activity = focus_state.get("low_activity_streak")
    prior_low_activity = raw_low_activity if isinstance(raw_low_activity, dict) else {}
    low_activity_streak: dict[str, int] = {}
    for mint in prior_focus:
        market = markets.get(mint)
        previous = prior_low_activity.get(mint)
        count = previous if type(previous) is int and 0 <= previous <= 3 else 0
        if market is not None:
            transactions = market.txns_5m_buys + market.txns_5m_sells
            count = min(3, count + 1) if transactions < 5 else 0
        low_activity_streak[mint] = count
    low_activity_excluded = {mint for mint in prior_focus if low_activity_streak.get(mint, 0) >= 3}
    active_set = set(active_available) - revision_excluded - low_activity_excluded
    ready_set = set(ready_mints)

    def audit_fresh_enough_for_new_focus(mint: str) -> bool:
        candidate = refreshed_candidates.get(mint)
        if candidate is None:
            return False
        try:
            audited_at = datetime.fromisoformat(str(candidate.get("last_seen")))
        except ValueError:
            return False
        if audited_at.tzinfo is None:
            return False
        # Entry rejects audits older than ten minutes. Leave three minutes
        # for candle collection and the follow-up decision after pin selection.
        age = now_epoch - audited_at.timestamp()
        return -60 <= age <= 420

    def focus_still_current(mint: str) -> bool:
        selected_at = recently_selected.get(mint, raw_selected_at)
        return (
            mint in active_set
            and mint in ready_set
            and type(selected_at) is int
            and 0 <= now_epoch - selected_at < 3_600
        )

    strategy_focus = [mint for mint in prior_focus if focus_still_current(mint)]
    retained_focus = set(strategy_focus)
    stale_audit_excluded = {
        mint
        for mint in (ready_set & active_set) - retained_focus
        if not audit_fresh_enough_for_new_focus(mint)
    }

    def focus_rank(mint: str) -> tuple[bool, int, str]:
        market = markets.get(mint)
        activity = market.txns_5m_buys + market.txns_5m_sells if market else 0
        return (recently_selected.get(mint, 0) > now_epoch - 7_200, -activity, mint)

    for mint in sorted(
        (ready_set & active_set) - set(strategy_focus) - stale_audit_excluded,
        key=focus_rank,
    ):
        if len(strategy_focus) >= 2:
            break
        strategy_focus.append(mint)
    if (
        strategy_focus != prior_focus
        or not isinstance(raw_recent, dict)
        or focus_state.get("revision_excluded") != sorted(revision_excluded)
        or focus_state.get("low_activity_streak") != low_activity_streak
    ):
        for mint in set(strategy_focus) - retained_focus:
            recently_selected[mint] = now_epoch
        journal.set_runtime_state(
            "ohlcv_active_focus",
            {
                "mints": strategy_focus,
                "selected_at": now_epoch,
                "revision_excluded": sorted(revision_excluded),
                "low_activity_streak": {
                    mint: low_activity_streak.get(mint, 0) for mint in strategy_focus
                },
                "last_selected_at": dict(
                    sorted(recently_selected.items(), key=lambda item: item[1], reverse=True)[:100]
                ),
            },
        )
    # Reserve one continuous observation in the independently selected cohort.
    # Within that fixed population prefer active members that currently clear
    # the market-only gate, then other audited active members. Preserve every
    # selection window so later research can disclose observation conditioning.
    cohort_selected_at = (journal.runtime_state(RESEARCH_COHORT_KEY) or {}).get("selected_at")
    research_state = journal.runtime_state("ohlcv_research_active_focus") or {}
    raw_research_mint = research_state.get("mint")
    prior_research_mint = raw_research_mint if isinstance(raw_research_mint, str) else None
    prior_research_at = research_state.get("selected_at")
    available_research = [
        mint for mint in cohort_mints if mint in active_research and has_candle_slot(mint)
    ]
    research_recent_raw = research_state.get("last_selected_at")
    research_recent = (
        {
            mint: stamp
            for mint, stamp in research_recent_raw.items()
            if isinstance(mint, str) and type(stamp) is int and 0 <= stamp <= now_epoch
        }
        if isinstance(research_recent_raw, dict)
        else {}
    )
    same_cohort = (
        type(cohort_selected_at) is int
        and research_state.get("cohort_selected_at") == cohort_selected_at
    )
    research_pin_valid = (
        same_cohort
        and prior_research_mint in available_research
        and type(prior_research_at) is int
        and 0 <= now_epoch - prior_research_at < 3_600
    )
    research_pin = prior_research_mint if research_pin_valid else None
    if research_pin is None and available_research:
        cohort_rank = {mint: index for index, mint in enumerate(cohort_mints)}
        safe_set = set(safe_watchlist)

        def research_rank(mint: str) -> tuple[int, bool, bool, int]:
            tier = 0 if mint in ready_set else 1 if mint in safe_set else 2
            return (
                tier,
                mint in strategy_focus,
                research_recent.get(mint, 0) > now_epoch - 7_200,
                cohort_rank[mint],
            )

        research_pin = min(available_research, key=research_rank)
    if not research_pin_valid or not isinstance(research_recent_raw, dict):
        raw_history = research_state.get("history")
        history = (
            [row for row in raw_history if isinstance(row, dict)]
            if isinstance(raw_history, list)
            else []
        )
        if not research_pin_valid and prior_research_mint and type(prior_research_at) is int:
            history.append(
                {
                    "mint": prior_research_mint,
                    "selected_at": prior_research_at,
                    "ended_at": now_epoch,
                    "cohort_selected_at": research_state.get("cohort_selected_at"),
                    "end_reason": "cohort_changed" if not same_cohort else "expired_or_inactive",
                }
            )
        if research_pin and not research_pin_valid:
            research_recent[research_pin] = now_epoch
        journal.set_runtime_state(
            "ohlcv_research_active_focus",
            {
                "mint": research_pin,
                "selected_at": (
                    prior_research_at if research_pin_valid else now_epoch if research_pin else None
                ),
                "cohort_selected_at": cohort_selected_at,
                "selection_tier": (
                    "market_eligible"
                    if research_pin in ready_set
                    else "audit_safe"
                    if research_pin in safe_watchlist
                    else "active_only"
                    if research_pin
                    else None
                ),
                "last_selected_at": dict(
                    sorted(research_recent.items(), key=lambda item: item[1], reverse=True)[:100]
                ),
                "history": history[-1_000:],
            },
        )
    research_request_lanes = ([research_pin] if research_pin else [], *active_research_lanes)
    deep_available = [
        mint
        for mint in active_available
        if has_candle_slot(mint, "15m") and has_candle_slot(mint, "1h")
    ]
    selected: list[tuple[str, tuple[str, ...]]] = []
    selected_short: set[str] = set()

    def add_short(candidates: list[str], *, limit: int) -> None:
        for mint in sorted(candidates, key=lambda item: oldest_fetch(item, short_frames)):
            if len(selected_short) >= limit:
                break
            if mint not in selected_short:
                selected.append((mint, short_frames))
                selected_short.add(mint)

    followup_due = [
        mint for mint in followup_mints if has_candle_slot(mint) and mint not in inactive_5m_mints
    ]
    # This prospective sample has its own selection record. Reserve one call
    # before rotating active and score-independent research candidates.
    add_short(strategy_focus, limit=2)
    if cycle % 8 != 3 or not deep_available:
        add_short(followup_due, limit=3)

    if cycle % 8 == 7 and focus_available:
        lane = "research_1m"
        # Reserve half the calls for the rest of the fixed cohort. The shared
        # selection limit previously let focus mints consume every slot.
        add_short(active_focus, limit=request_budget // 2)
        add_short(focus_available, limit=request_budget // 2)
        focus_calls = list(selected)
        add_short([research_pin] if research_pin else [], limit=len(selected_short) + 1)
        add_short(active_remaining, limit=request_budget)
        add_short(remaining_cohort, limit=request_budget)
        add_short(secondary, limit=request_budget)
        secondary_calls = selected[len(focus_calls) :]
        add_short(focus_available, limit=request_budget)
        extra_focus_calls = selected[len(focus_calls) + len(secondary_calls) :]
        selected = [
            item
            for pair in zip_longest(focus_calls, secondary_calls)
            for item in pair
            if item is not None
        ] + extra_focus_calls
    elif cycle % 8 == 3 and deep_available:
        lane = "active_deep"
        add_short(active_available, limit=min(max(2, request_budget - 4), len(active_available)))
        deep_focus = [mint for mint in strategy_focus if mint in deep_available]
        deep_mint = min(
            deep_focus or deep_available,
            key=lambda item: oldest_fetch(item, deep_frames),
        )
        selected.append((deep_mint, deep_frames))
        short_budget = request_budget - 2  # Deep frames consume two public calls.
        for candidates in research_request_lanes:
            if len(selected_short) < short_budget:
                add_short(candidates, limit=len(selected_short) + 1)
        add_short(focus_available, limit=short_budget)
        add_short(active_available, limit=short_budget)
        add_short(secondary, limit=short_budget)
    else:
        lane = (
            "active_research_1m"
            if active_available and (focus_available or remaining_cohort)
            else ("active_short" if active_available else "research_1m")
        )
        # At the four-call public quota, the fixed follow-up plus two rotating
        # active candidates still leaves one unbiased research observation.
        research_reserve = 1 if followup_due and request_budget == 4 else 2
        active_budget = (
            request_budget - research_reserve
            if focus_available or remaining_cohort
            else request_budget
        )
        add_short(active_available, limit=min(active_budget, len(active_available)))
        for candidates in research_request_lanes:
            if len(selected_short) < request_budget:
                add_short(candidates, limit=len(selected_short) + 1)
        add_short(focus_available, limit=request_budget)
        add_short(active_available, limit=request_budget)
        add_short(secondary, limit=request_budget)
        if active_available and (focus_available or remaining_cohort):
            # A public quota can interrupt after only two successful calls.
            # Give both purposes a slot before the third request.
            active_calls = [item for item in selected if item[0] in active_available]
            research_calls = [item for item in selected if item[0] not in active_available]
            selected = [
                item
                for pair in zip_longest(active_calls, research_calls)
                for item in pair
                if item is not None
            ]

    if followup_due and selected:
        selected.sort(key=lambda item: item[0] not in followup_due)

    # A complete 1m stream can still miss one minute inside an otherwise
    # active 5m bucket. The provider's direct 5m bar is authentic pool OHLCV
    # and can bridge that gap. Spend only a spare/secondary short request,
    # keep both active 1m pins and any scheduled independent research sample,
    # and never exceed the same public request budget.
    direct_5m_repair: str | None = None
    direct_5m_repair_reason: str | None = None
    direct_5m_repair_displaced_mint: str | None = None
    repair_state = journal.runtime_state("ohlcv_direct_5m_repair_history") or {}
    raw_repair_history = repair_state.get("history")
    repair_history = (
        [row for row in raw_repair_history if isinstance(row, dict)]
        if isinstance(raw_repair_history, list)
        else []
    )
    last_repair_at = repair_history[-1].get("selected_at") if repair_history else None
    repair_due = type(last_repair_at) is not int or now_epoch - last_repair_at >= 300
    if lane != "active_deep" and strategy_focus and repair_due:
        for mint in sorted(strategy_focus, key=lambda item: oldest_fetch(item, ("5m",))):
            if not has_candle_slot(mint, "5m"):
                continue
            pool = pool_for(mint)
            if pool is None:
                continue
            direct_rows = journal.pool_candles(mint, pool, "5m", limit=1)
            last_direct_fetch = direct_rows[0].get("fetched_at") if direct_rows else None
            if type(last_direct_fetch) is int and now_epoch - last_direct_fetch < 300:
                continue
            minute_signal = analyze_entry(
                load_verified_candles(journal, mint, pool, "1m", now=now_epoch),
                minimum_liquidity_usd=0,
                timeframe="1m",
                as_of=now_epoch,
            )
            if minute_signal.status != "ready":
                continue
            five_candles = load_verified_candles(journal, mint, pool, "5m", now=now_epoch)
            # A mixed direct/derived window can conceal a known >1% overlap
            # conflict. A fresh request cannot safely approve that window.
            if any(_has_conflicting_ohlc(candle, now_epoch) for candle in five_candles[-8:]):
                continue
            five_signal = analyze_entry(
                five_candles,
                minimum_liquidity_usd=0,
                timeframe="5m",
                as_of=now_epoch,
            )
            if five_signal.status in {"gap", "mixed_provenance", "stale"}:
                direct_5m_repair = mint
                direct_5m_repair_reason = five_signal.status
                break
    if direct_5m_repair is not None:
        if len(selected) < request_budget:
            selected.append((direct_5m_repair, ("5m",)))
        else:
            independent_short = [
                mint
                for mint, frames in selected
                if frames == short_frames and mint not in strategy_focus
            ]
            research_short = [mint for mint in independent_short if mint in cohort_mints]
            replacement = next(
                (
                    index
                    for index in range(len(selected) - 1, -1, -1)
                    if selected[index][1] == short_frames
                    and selected[index][0] in independent_short
                    and selected[index][0] != research_pin
                    and len(independent_short) >= 2
                    and (selected[index][0] not in research_short or len(research_short) >= 2)
                ),
                None,
            )
            if replacement is not None:
                direct_5m_repair_displaced_mint = selected[replacement][0]
                selected_short.remove(direct_5m_repair_displaced_mint)
                selected[replacement] = (direct_5m_repair, ("5m",))
            else:
                direct_5m_repair = None
                direct_5m_repair_reason = None

    backoff = journal.runtime_state("ohlcv_backoff") or {}
    raw_until = backoff.get("until")
    backoff_until = raw_until if isinstance(raw_until, int) else 0
    rate_limited = backoff_until > int(time.time())
    followup_attempted = 0
    followup_succeeded = 0
    direct_5m_repair_attempted = False
    direct_5m_repair_request_succeeded = False
    direct_5m_repair_bars_returned = 0
    if not rate_limited:
        request_interval = (
            TRIAL_REQUEST_INTERVAL_SECONDS
            if request_budget > 4
            else PUBLIC_REQUEST_INTERVAL_SECONDS
        )
        with GeckoTerminalOhlcvClient() as ohlcv:
            ohlcv.request_interval_seconds = request_interval
            for mint, frames in selected:
                pool = pool_for(mint)
                if pool is None:
                    continue
                for timeframe in frames:
                    try:
                        ohlcv_attempted += 1
                        if mint == direct_5m_repair and timeframe == "5m":
                            direct_5m_repair_attempted = True
                        if mint in followup_mints and timeframe == "1m":
                            followup_attempted += 1
                        if mint in remaining_cohort_set:
                            research_secondary_attempted += 1
                        candles = ohlcv.candles(mint, pool, timeframe)
                        ohlcv_succeeded += 1
                        if mint == direct_5m_repair and timeframe == "5m":
                            direct_5m_repair_request_succeeded = True
                            direct_5m_repair_bars_returned = len(candles)
                        if getattr(ohlcv, "last_call_cache_hit", False):
                            ohlcv_cache_hits += 1
                        if mint in followup_mints and timeframe == "1m":
                            followup_succeeded += 1
                        if mint in remaining_cohort_set:
                            research_secondary_succeeded += 1
                        if candles:
                            stored += journal.record_pool_candles(
                                candles, fetched_at=int(time.time())
                            )
                            latest_close = max(
                                candle.opened_at + TIMEFRAME_SECONDS[timeframe]
                                for candle in candles
                            )
                            if latest_close < int(time.time()) - max(
                                300, 2 * TIMEFRAME_SECONDS[timeframe]
                            ):
                                # Keep the cohort membership and old OHLCV. An inactive pool
                                # only gives an old nonempty page; retry later so scarce
                                # public calls can sample the remaining selected mints.
                                stale_responses += 1
                                retry_until[f"{mint}:{pool}:{timeframe}"] = int(time.time()) + 900
                            else:
                                retry_until.pop(f"{mint}:{pool}:{timeframe}", None)
                                refreshed_mints.add(mint)
                        else:
                            empty_responses += 1
                            retry_until[f"{mint}:{pool}:{timeframe}"] = int(time.time()) + 300
                    except OhlcvRateLimited as exc:
                        error_codes[_ohlcv_error_code(exc)] += 1
                        errors.append(f"{mint[:6]} {timeframe}: {exc}")
                        backoff_until = int(time.time()) + exc.retry_after_seconds
                        journal.set_runtime_state("ohlcv_backoff", {"until": backoff_until})
                        rate_limited = True
                        break
                    except OhlcvError as exc:
                        code = _ohlcv_error_code(exc)
                        error_codes[code] += 1
                        retry_seconds = (
                            900 if code in {"pool_not_found", "pool_identity_mismatch"} else 300
                        )
                        retry_until[f"{mint}:{pool}:{timeframe}"] = int(time.time()) + retry_seconds
                        errors.append(f"{mint[:6]} {timeframe}: {exc}")
                if rate_limited:
                    break
        # A quota failure interrupted this lane. Preserve it across cooldown
        # so the next successful cycle does not silently skip research data.
        if not rate_limited:
            journal.set_runtime_state("ohlcv_schedule", {"cycle": cycle + 1})
    # Some sampled pools have authentic trade candles but DEX Screener omits
    # liquidity. Capture the exact pool's reserve prospectively, without
    # changing the paper entry market gate or inventing historical liquidity.
    pool_probe_state = journal.runtime_state("research_pool_market_probe") or {}
    raw_next_probe = pool_probe_state.get("next_at")
    next_pool_probe_at = raw_next_probe if type(raw_next_probe) is int else 0
    missing_research_market: dict[str, str] = {}
    for mint in cohort_mints:
        market = markets.get(mint)
        if market is not None and market.price_usd is not None and market.liquidity_usd is not None:
            continue
        pool = pool_for(mint)
        if pool is not None:
            missing_research_market[mint] = pool
    pool_probe_status = "not_due"
    pool_probe_recorded = 0
    raw_cursor = pool_probe_state.get("cursor")
    cursor = raw_cursor if type(raw_cursor) is int and raw_cursor >= 0 else 0
    if (
        not rate_limited
        and request_budget == 4
        and missing_research_market
        and int(time.time()) >= next_pool_probe_at
    ):
        missing_items = list(missing_research_market.items())
        start = cursor % len(missing_items)
        selected_probe = dict(
            (missing_items + missing_items)[start : start + min(10, len(missing_items))]
        )
        cursor = (start + len(selected_probe)) % len(missing_items)
        try:
            observations = fetch_research_pool_markets(selected_probe)
            observed_at = int(time.time())
            for observation in observations:
                journal.record_market_snapshot(
                    mint=observation.mint,
                    pool=observation.pool,
                    price_usd=observation.price_usd,
                    liquidity_usd=observation.liquidity_usd,
                    volume_24h_usd=observation.volume_24h_usd,
                    price_change_24h=observation.price_change_24h,
                    pair_created_at=observation.pair_created_at,
                    observed_at=observed_at,
                    source="geckoterminal_pool",
                )
                pool_probe_recorded += 1
            pool_probe_status = "success"
            # This endpoint shares the public provider's budget with the
            # candle lane. Keep the optional research probe well below one/min.
            next_pool_probe_at = observed_at + 300
        except OhlcvRateLimited as exc:
            pool_probe_status = "rate_limited"
            next_pool_probe_at = int(time.time()) + max(600, exc.retry_after_seconds)
            # A pool-probe 429 is evidence that the shared public quota cannot
            # safely support the six-call candle trial right now.
            higher_budget_retry_after = max(
                higher_budget_retry_after, int(time.time()) + HIGHER_BUDGET_RETRY_SECONDS
            )
            request_budget = 4
            fresh_network_successes = 0
            fresh_window_started_at = 0
        except OhlcvError:
            pool_probe_status = "provider_error"
            next_pool_probe_at = int(time.time()) + 600
        journal.set_runtime_state(
            "research_pool_market_probe",
            {
                "next_at": next_pool_probe_at,
                "cursor": cursor,
                "last_status": pool_probe_status,
                "last_recorded": pool_probe_recorded,
                "requested_pools": len(set(selected_probe.values())),
            },
        )
    elif request_budget > 4 and missing_research_market:
        pool_probe_status = "deferred_for_candle_capacity"
    journal.set_runtime_state(
        "ohlcv_retry_cooldown",
        {
            "entries": dict(
                sorted(retry_until.items(), key=lambda item: item[1], reverse=True)[:100]
            )
        },
    )
    if rate_limited:
        if request_budget > 4:
            higher_budget_retry_after = int(time.time()) + HIGHER_BUDGET_RETRY_SECONDS
        request_budget = 4
        fresh_network_successes = 0
        fresh_window_started_at = 0
    elif (
        len(ready_mints) >= 8
        and ohlcv_attempted == request_budget
        and (ohlcv_succeeded == ohlcv_attempted and not error_codes)
        and not empty_responses
        and not stale_responses
    ):
        # Cache reuse is common between minute bars. Count only fresh provider
        # responses in a bounded window, even when a cycle mixes both kinds.
        fresh_calls = ohlcv_succeeded - ohlcv_cache_hits
        if fresh_calls > 0:
            if fresh_network_successes == 0:
                fresh_window_started_at = collection_started_at
            fresh_network_successes += fresh_calls
            if fresh_network_successes >= 12:
                request_budget = min(max_request_budget, request_budget + 2)
                fresh_network_successes = 0
                fresh_window_started_at = 0
    else:
        fresh_network_successes = 0
        fresh_window_started_at = 0
    # Keep a bounded, factual throughput record. The latest worker result alone
    # cannot distinguish a brief provider cooldown from a sustained data gap.
    completed_at = int(time.time())
    raw_cycles = budget_state.get("recent_cycles")
    recent_cycles = (
        [
            row
            for row in raw_cycles
            if isinstance(row, dict)
            and type(row.get("at")) is int
            and completed_at - 3_600 <= row["at"] <= completed_at
            and type(row.get("attempted")) is int
            and type(row.get("succeeded")) is int
            and type(row.get("rate_limited")) is bool
        ]
        if isinstance(raw_cycles, list)
        else []
    )
    recent_cycles.append(
        {
            "started_at": collection_started_at,
            "at": completed_at,
            "attempted": ohlcv_attempted,
            "succeeded": ohlcv_succeeded,
            "cache_hits": ohlcv_cache_hits,
            "rate_limited": rate_limited,
            "market_ready": len(ready_mints),
            "research_focus": len(research_focus),
        }
    )
    journal.set_runtime_state(
        "ohlcv_request_budget",
        {
            "limit": request_budget,
            "fresh_network_successes": fresh_network_successes,
            "fresh_window_started_at": fresh_window_started_at,
            "higher_budget_retry_after": higher_budget_retry_after,
            "recent_cycles": recent_cycles[-120:],
        },
    )
    if direct_5m_repair is not None:
        repair_pool = pool_for(direct_5m_repair)
        status_after: str | None = None
        if direct_5m_repair_request_succeeded and repair_pool is not None:
            status_after = analyze_entry(
                load_verified_candles(journal, direct_5m_repair, repair_pool, "5m"),
                minimum_liquidity_usd=0,
                timeframe="5m",
                as_of=int(time.time()),
            ).status
        repair_history.append(
            {
                "mint": direct_5m_repair,
                "pool": repair_pool,
                "selected_at": collection_started_at,
                "reason": direct_5m_repair_reason,
                "displaced_1m_mint": direct_5m_repair_displaced_mint,
                "displaced_decision_followup": direct_5m_repair_displaced_mint in followup_mints,
                "attempted": direct_5m_repair_attempted,
                "request_succeeded": direct_5m_repair_request_succeeded,
                "bars_returned": direct_5m_repair_bars_returned,
                "five_minute_status_after": status_after,
            }
        )
        journal.set_runtime_state(
            "ohlcv_direct_5m_repair_history", {"history": repair_history[-1_000:]}
        )
    return {
        "collected": len(markets),
        "tracked": len(mints),
        "dex_requests_attempted": dex_attempted,
        "dex_requests_succeeded": dex_succeeded,
        "research_cohort_size": len(cohort_mints),
        "research_cohort_tracked": len(set(cohort_mints) & set(mints)),
        "research_focus_size": len(research_focus),
        "research_focus_with_pool": len(focus_with_slot),
        "inactive_5m_mints_deferred": len(inactive_5m_mints),
        "research_active_5m_size": len(active_research),
        "research_active_5m_eligible": len(active_focus) + len(active_remaining),
        "research_active_focus_size": int(research_pin is not None),
        "research_active_focus_scheduled": int(research_pin in selected_short),
        "strategy_focus_size": len(strategy_focus),
        "strategy_focus_scheduled": len(set(strategy_focus) & selected_short),
        "strategy_focus_revision_excluded": len(revision_excluded),
        "strategy_focus_low_activity_excluded": len(low_activity_excluded),
        "strategy_focus_stale_audit_excluded": len(stale_audit_excluded),
        "research_active_5m_scheduled": len(selected_short & active_research),
        "decision_followup_size": len(followup_mints),
        "decision_followup_due": bool(followup_due),
        "decision_followup_attempted": followup_attempted,
        "decision_followup_succeeded": followup_succeeded,
        "safe_watchlist_size": len(safe_watchlist),
        "safe_watchlist_tracked": len(set(safe_watchlist) & set(mints)),
        "priority_safe_tracked": len(set(priority_safe) & set(mints)),
        "active_watchlist_size": len(active_mints),
        "active_market_ready_size": len(set(active_mints) & set(ready_mints)),
        "market_ready_mints": len(ready_mints),
        "collection_lane": lane,
        "direct_5m_repair_scheduled": direct_5m_repair is not None,
        "direct_5m_repair_attempted": direct_5m_repair_attempted,
        "direct_5m_repair_request_succeeded": direct_5m_repair_request_succeeded,
        "direct_5m_repair_bars_returned": direct_5m_repair_bars_returned,
        "authentic_candles_refreshed": stored,
        "ohlcv_refreshed_mints": sorted(refreshed_mints),
        "ohlcv_requests_attempted": ohlcv_attempted,
        "ohlcv_requests_succeeded": ohlcv_succeeded,
        "ohlcv_cache_hits": ohlcv_cache_hits,
        "ohlcv_request_budget_next": request_budget,
        "ohlcv_higher_budget_retry_after": (
            higher_budget_retry_after if higher_budget_retry_after > int(time.time()) else None
        ),
        "research_secondary_attempted": research_secondary_attempted,
        "research_secondary_succeeded": research_secondary_succeeded,
        "ohlcv_empty_responses": empty_responses,
        "ohlcv_stale_responses": stale_responses,
        "ohlcv_retry_cooldown_size": len(retry_until),
        "ohlcv_provider": "geckoterminal",
        "research_pool_probe_status": pool_probe_status,
        "research_pool_probe_recorded": pool_probe_recorded,
        "research_pool_probe_missing_markets": len(missing_research_market),
        "ohlcv_rate_limited": rate_limited,
        "ohlcv_backoff_until": backoff_until if rate_limited else None,
        "ohlcv_errors": errors[:12],
        "ohlcv_error_codes": dict(error_codes),
    }


class StrategyLabError(ValueError):
    """Raised when a replay scenario or market series is invalid."""


@dataclass(frozen=True, slots=True)
class Candle:
    opened_at: int
    open: float
    high: float
    low: float
    close: float
    volume_delta_usd: float
    liquidity_usd: float
    samples: int
    source: str = "legacy_snapshot_proxy"
    close_disagreement_pct: float | None = None
    first_fetched_at: int | None = None
    first_observed_values_at: int | None = None
    ohlc_disagreement_pct: float | None = None
    overlap_observed_at: int | None = None
    revision_detected_at: int | None = None

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _has_conflicting_ohlc(candle: Candle, as_of: int | None) -> bool:
    difference = (
        candle.ohlc_disagreement_pct
        if candle.ohlc_disagreement_pct is not None
        else candle.close_disagreement_pct
    )
    return (
        difference is not None
        and difference > 1
        and (
            as_of is None
            or (candle.overlap_observed_at is not None and candle.overlap_observed_at <= as_of)
        )
    )


def _available_for_replay(candle: Candle, decision_at: int, timeframe: Timeframe) -> bool:
    closed_at = candle.opened_at + TIMEFRAME_SECONDS[timeframe]
    return (
        candle.first_fetched_at is not None
        and candle.first_observed_values_at is not None
        and candle.first_fetched_at >= closed_at
        and candle.first_observed_values_at >= closed_at
        and candle.first_fetched_at < decision_at
        and candle.first_observed_values_at < decision_at
    )


def _available_for_forward_signal(candle: Candle, as_of: int, timeframe: Timeframe) -> bool:
    # Forward evaluation runs after its fetch, even when both timestamps fall
    # within the same recorded second. Historical replay cannot establish that
    # ordering, so its gate above remains strictly earlier than the decision.
    return (
        candle.first_fetched_at is not None
        and candle.first_observed_values_at is not None
        and candle.first_fetched_at >= candle.opened_at + TIMEFRAME_SECONDS[timeframe]
        and candle.first_observed_values_at >= candle.opened_at + TIMEFRAME_SECONDS[timeframe]
        and candle.first_fetched_at <= as_of
        and candle.first_observed_values_at <= as_of
    )


def _timely_exit_observation(candle: Candle, timeframe: Timeframe) -> bool:
    """Do not assign an intrabar fill from a bar discovered long after its close."""
    closed_at = candle.opened_at + TIMEFRAME_SECONDS[timeframe]
    return (
        candle.first_fetched_at is not None
        and candle.first_observed_values_at is not None
        and closed_at <= candle.first_fetched_at <= closed_at + 120
        and closed_at <= candle.first_observed_values_at <= closed_at + 120
    )


@dataclass(frozen=True, slots=True)
class EntrySignal:
    status: str
    confirmed: bool
    score: int
    trend: str
    momentum_pct: float | None
    volume_ratio: float | None
    liquidity_usd: float | None
    reasons: tuple[str, ...]

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _number(value: object) -> float | None:
    if value in (None, ""):
        return None
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return float(parsed) if parsed.is_finite() else None


def _historical_market_block(
    snapshot: dict[str, object] | None,
    *,
    decision_at: int,
    expected_pool: str,
    minimum_liquidity_usd: float,
    minimum_volume_24h_usd: float,
) -> str | None:
    """Reject an entry without contemporaneous evidence for the paper hard gates."""
    if snapshot is None:
        return "market_snapshot_missing"
    captured_at = snapshot.get("captured_at")
    if type(captured_at) is not int or not 0 <= decision_at - captured_at <= 180:
        return "market_snapshot_stale"
    if snapshot.get("pool") != expected_pool:
        return "market_pool_mismatch"
    liquidity = _number(snapshot.get("liquidity_usd"))
    if liquidity is None or liquidity < 0:
        return "market_liquidity_unavailable"
    if liquidity < minimum_liquidity_usd:
        return "market_liquidity_below_minimum"
    volume = _number(snapshot.get("volume_24h_usd"))
    if volume is None or volume < 0:
        return "market_volume_unavailable"
    if volume < minimum_volume_24h_usd:
        return "market_volume_below_minimum"
    change = _number(snapshot.get("price_change_24h"))
    if change is not None and abs(change) > MAX_ABSOLUTE_24H_CHANGE_PCT:
        return "market_move_exceeds_guard"
    return None


def _complete_timeframe_rows(
    rows: list[dict[str, object]], timeframe: Timeframe
) -> list[dict[str, object]]:
    """Aggregate only complete groups of real 1m pool-trade bars."""
    if timeframe == "1m":
        raise StrategyLabError("Derived candles require a higher timeframe")
    period = TIMEFRAME_SECONDS[timeframe]
    required_minutes = period // 60
    buckets: dict[int, list[dict[str, object]]] = {}
    for row in rows:
        opened_at = row.get("opened_at")
        if type(opened_at) is not int or opened_at < 0 or opened_at % 60:
            continue
        bucket = opened_at - opened_at % period
        buckets.setdefault(bucket, []).append(row)
    complete: list[dict[str, object]] = []
    for bucket, minute_rows in sorted(buckets.items()):
        minute_rows.sort(key=lambda row: int(str(row["opened_at"])))
        if [row["opened_at"] for row in minute_rows] != list(range(bucket, bucket + period, 60)):
            continue
        try:
            opens = [Decimal(str(row["open"])) for row in minute_rows]
            highs = [Decimal(str(row["high"])) for row in minute_rows]
            lows = [Decimal(str(row["low"])) for row in minute_rows]
            closes = [Decimal(str(row["close"])) for row in minute_rows]
            volumes = [Decimal(str(row["volume_usd"])) for row in minute_rows]
        except (InvalidOperation, ValueError, TypeError, KeyError):
            continue
        if (
            not all(value.is_finite() for value in [*opens, *highs, *lows, *closes, *volumes])
            or any(value <= 0 for value in [*opens, *highs, *lows, *closes])
            or any(value < 0 for value in volumes)
            or any(
                high < max(opened, closed, low) or low > min(opened, closed, high)
                for opened, high, low, closed in zip(opens, highs, lows, closes, strict=True)
            )
        ):
            continue
        complete.append(
            {
                "opened_at": bucket,
                "open": str(opens[0]),
                "high": str(max(highs)),
                "low": str(min(lows)),
                "close": str(closes[-1]),
                "volume_usd": str(sum(volumes)),
                "samples": required_minutes,
                "fetched_at": (
                    max(int(str(row["fetched_at"])) for row in minute_rows)
                    if all(type(row.get("fetched_at")) is int for row in minute_rows)
                    else None
                ),
                "first_fetched_at": (
                    max(int(str(row["first_fetched_at"])) for row in minute_rows)
                    if all(type(row.get("first_fetched_at")) is int for row in minute_rows)
                    else None
                ),
                "first_observed_values_at": (
                    max(int(str(row["first_observed_values_at"])) for row in minute_rows)
                    if all(type(row.get("first_observed_values_at")) is int for row in minute_rows)
                    else None
                ),
                "revision_detected_at": min(
                    (
                        value
                        for row in minute_rows
                        if type(value := row.get("revision_detected_at")) is int
                    ),
                    default=None,
                ),
            }
        )
    return complete


def timeframe_overlap_diagnostic(
    journal: TradeJournal, mint: str, pool: str, timeframe: Timeframe
) -> dict[str, int | float]:
    """Compare direct provider bars with complete 1m aggregates from one pool."""
    if timeframe == "1m":
        raise StrategyLabError("Overlap comparison requires a higher timeframe")
    derived = {
        int(str(row["opened_at"])): row
        for row in _complete_timeframe_rows(journal.pool_candles(mint, pool, "1m"), timeframe)
    }
    compared = 0
    bars_over_one_pct = 0
    max_price_difference_pct = Decimal(0)
    max_volume_difference_usd = Decimal(0)
    for direct in journal.pool_candles(mint, pool, timeframe):
        other = derived.get(int(str(direct["opened_at"])))
        if other is None:
            continue
        try:
            price_difference_pct = max(
                abs(Decimal(str(other[field])) - Decimal(str(direct[field])))
                / Decimal(str(direct[field]))
                * 100
                for field in ("open", "high", "low", "close")
            )
            volume_difference_usd = abs(
                Decimal(str(other["volume_usd"])) - Decimal(str(direct["volume_usd"]))
            )
        except (InvalidOperation, ValueError, TypeError, KeyError, ZeroDivisionError):
            continue
        compared += 1
        bars_over_one_pct += price_difference_pct > 1
        max_price_difference_pct = max(max_price_difference_pct, price_difference_pct)
        max_volume_difference_usd = max(max_volume_difference_usd, volume_difference_usd)
    return {
        "compared": compared,
        "bars_over_1pct": bars_over_one_pct,
        "max_price_difference_pct": round(float(max_price_difference_pct), 4),
        "max_volume_difference_usd": round(float(max_volume_difference_usd), 6),
    }


def five_minute_overlap_diagnostic(
    journal: TradeJournal, mint: str, pool: str
) -> dict[str, int | float]:
    """Measure provider 5m bars against complete 1m bars for the same pool."""
    return timeframe_overlap_diagnostic(journal, mint, pool, "5m")


def aggregate_candles(snapshots: list[dict[str, object]], timeframe: Timeframe) -> list[Candle]:
    """Legacy diagnostic reconstruction; never use these for trading or replay."""
    seconds = TIMEFRAME_SECONDS[timeframe]
    ordered = sorted(snapshots, key=lambda row: str(row["observed_at"]))
    buckets: dict[int, list[tuple[float, float, float]]] = {}
    previous_volume: float | None = None
    for row in ordered:
        observed_raw = row.get("observed_at")
        if not isinstance(observed_raw, int):
            continue
        observed_at = observed_raw
        price = _number(row.get("price_usd"))
        liquidity = _number(row.get("liquidity_usd"))
        volume = _number(row.get("volume_24h_usd"))
        if price is None or price <= 0 or liquidity is None or liquidity < 0:
            continue
        volume_delta = 0.0
        if volume is not None and previous_volume is not None:
            # DEX Screener exposes rolling 24h volume, not per-candle volume. Positive
            # deltas are a conservative activity proxy; negative window roll-off is ignored.
            volume_delta = max(0.0, volume - previous_volume)
        if volume is not None:
            previous_volume = volume
        bucket = observed_at - observed_at % seconds
        buckets.setdefault(bucket, []).append((price, liquidity, volume_delta))

    candles: list[Candle] = []
    for opened_at, samples in sorted(buckets.items()):
        prices = [sample[0] for sample in samples]
        candles.append(
            Candle(
                opened_at=opened_at,
                open=prices[0],
                high=max(prices),
                low=min(prices),
                close=prices[-1],
                volume_delta_usd=sum(sample[2] for sample in samples),
                liquidity_usd=samples[-1][1],
                samples=len(samples),
            )
        )
    return candles


def load_verified_candles(
    journal: TradeJournal,
    mint: str,
    pool: str,
    timeframe: Timeframe,
    *,
    now: int | None = None,
) -> list[Candle]:
    """Load authentic pool candles with only contemporaneous liquidity evidence."""
    current = now if now is not None else int(time.time())
    period = TIMEFRAME_SECONDS[timeframe]
    snapshots = [
        row for row in journal.market_snapshots(mint) if isinstance(row.get("captured_at"), int)
    ]

    def observed_by(row: dict[str, object]) -> bool:
        # A direct provider bar fetched later must not replace a derived bar
        # that was available at the historical decision time.
        return all(
            value is None or (type(value) is int and value <= current)
            for key in ("first_fetched_at", "first_observed_values_at")
            if (value := row.get(key)) is not None
        )

    rows = [row for row in journal.pool_candles(mint, pool, timeframe) if observed_by(row)]
    if timeframe != "1m":
        direct = {int(str(row["opened_at"])): row for row in rows}
        minute_rows = [row for row in journal.pool_candles(mint, pool, "1m") if observed_by(row)]
        for derived in _complete_timeframe_rows(minute_rows, timeframe):
            opened_at = int(str(derived["opened_at"]))
            existing = direct.get(opened_at)
            if existing is None:
                direct[opened_at] = derived
                continue
            try:
                direct_close = Decimal(str(existing["close"]))
                close_disagreement = (
                    abs(Decimal(str(derived["close"])) - direct_close) / direct_close * 100
                )
                ohlc_disagreement = max(
                    abs(Decimal(str(derived[field])) - Decimal(str(existing[field])))
                    / Decimal(str(existing[field]))
                    * 100
                    for field in ("open", "high", "low", "close")
                )
            except (InvalidOperation, ValueError, TypeError, KeyError, ZeroDivisionError):
                continue
            observed_times = [
                value
                for row in (existing, derived)
                for key in ("first_fetched_at", "first_observed_values_at")
                if type(value := row.get(key)) is int
            ]
            latest_fetches = [
                value for row in (existing, derived) if type(value := row.get("fetched_at")) is int
            ]
            direct[opened_at] = {
                **existing,
                "close_disagreement_pct": float(close_disagreement),
                "ohlc_disagreement_pct": float(ohlc_disagreement),
                "overlap_observed_at": (
                    max(observed_times)
                    if len(observed_times) == 4
                    else max(latest_fetches)
                    if len(latest_fetches) == 2
                    else None
                ),
                "revision_detected_at": min(
                    (
                        value
                        for row in (existing, derived)
                        if type(value := row.get("revision_detected_at")) is int
                    ),
                    default=None,
                ),
            }
        rows = [direct[opened_at] for opened_at in sorted(direct)]
    output: list[Candle] = []
    snapshot_index = 0
    for row in rows:
        opened_at = int(str(row["opened_at"]))
        if opened_at + period > current:
            continue
        while (
            snapshot_index < len(snapshots)
            and int(str(snapshots[snapshot_index]["captured_at"])) <= opened_at + period
        ):
            snapshot_index += 1
        prior = snapshots[snapshot_index - 1] if snapshot_index else None
        liquidity = 0.0
        if (
            prior
            and prior.get("pool") == pool
            and opened_at + period - int(str(prior["captured_at"])) <= period + 120
        ):
            liquidity = _number(prior.get("liquidity_usd")) or 0.0
        first_fetched_at = row.get("first_fetched_at")
        first_observed_values_at = row.get("first_observed_values_at")
        output.append(
            Candle(
                opened_at=opened_at,
                open=float(str(row["open"])),
                high=float(str(row["high"])),
                low=float(str(row["low"])),
                close=float(str(row["close"])),
                volume_delta_usd=float(str(row["volume_usd"])),
                liquidity_usd=liquidity,
                samples=int(str(row.get("samples", 1))),
                source="geckoterminal_pool_trades",
                close_disagreement_pct=_number(row.get("close_disagreement_pct")),
                ohlc_disagreement_pct=_number(row.get("ohlc_disagreement_pct")),
                overlap_observed_at=(
                    value if type(value := row.get("overlap_observed_at")) is int else None
                ),
                revision_detected_at=(
                    value if type(value := row.get("revision_detected_at")) is int else None
                ),
                first_fetched_at=(
                    first_fetched_at
                    if isinstance(first_fetched_at, int) and not isinstance(first_fetched_at, bool)
                    else None
                ),
                first_observed_values_at=(
                    first_observed_values_at
                    if isinstance(first_observed_values_at, int)
                    and not isinstance(first_observed_values_at, bool)
                    else None
                ),
            )
        )
    return output


def analyze_entry(
    candles: list[Candle],
    *,
    minimum_liquidity_usd: float,
    timeframe: Timeframe = "1m",
    minimum_candles: int = 8,
    signal_threshold: int = DEFAULT_SIGNAL_THRESHOLD,
    disabled_factors: frozenset[FactorName] = frozenset(),
    as_of: int | None = None,
) -> EntrySignal:
    if not 1 <= signal_threshold <= 100:
        raise StrategyLabError("Signal threshold must be between 1 and 100")
    if len(candles) < minimum_candles:
        return EntrySignal(
            status="collecting",
            confirmed=False,
            score=0,
            trend="unavailable",
            momentum_pct=None,
            volume_ratio=None,
            liquidity_usd=candles[-1].liquidity_usd if candles else None,
            reasons=(f"Need {minimum_candles} closed candles; {len(candles)} collected",),
        )
    recent_candles = candles[-minimum_candles:]
    if (
        as_of is not None
        and recent_candles[-1].source == "geckoterminal_pool_trades"
        and as_of - recent_candles[-1].opened_at > 3 * TIMEFRAME_SECONDS[timeframe]
    ):
        return EntrySignal(
            "stale",
            False,
            0,
            "unavailable",
            None,
            None,
            recent_candles[-1].liquidity_usd,
            ("Latest closed trade candle is too old",),
        )
    if any(c.source != "legacy_snapshot_proxy" for c in recent_candles) and any(
        current.opened_at - previous.opened_at != TIMEFRAME_SECONDS[timeframe]
        for previous, current in pairwise(recent_candles)
    ):
        return EntrySignal(
            "gap",
            False,
            0,
            "unavailable",
            None,
            None,
            recent_candles[-1].liquidity_usd,
            ("Recent trade candles have a missing interval",),
        )
    if any(
        candle.revision_detected_at is not None
        and (as_of is None or candle.revision_detected_at <= as_of)
        for candle in recent_candles
    ):
        return EntrySignal(
            "revised_values",
            False,
            0,
            "unavailable",
            None,
            None,
            recent_candles[-1].liquidity_usd,
            ("Provider changed a previously closed candle",),
        )
    if timeframe != "1m" and len({candle.samples for candle in recent_candles}) > 1:
        return EntrySignal(
            "mixed_provenance",
            False,
            0,
            "unavailable",
            None,
            None,
            recent_candles[-1].liquidity_usd,
            (f"Recent {timeframe} bars mix direct and minute-aggregated prices",),
        )
    if timeframe != "1m" and any(_has_conflicting_ohlc(candle, as_of) for candle in recent_candles):
        return EntrySignal(
            "conflicting_prices",
            False,
            0,
            "unavailable",
            None,
            None,
            recent_candles[-1].liquidity_usd,
            (f"Direct and minute-derived {timeframe} OHLC prices disagree by over 1%",),
        )
    if as_of is not None and any(
        candle.source == "geckoterminal_pool_trades"
        and not _available_for_forward_signal(candle, as_of, timeframe)
        for candle in recent_candles
    ):
        return EntrySignal(
            "unverified_values",
            False,
            0,
            "unavailable",
            None,
            None,
            recent_candles[-1].liquidity_usd,
            ("Recent candle values lack trusted first-observation evidence",),
        )

    recent = candles[-minimum_candles:]
    closes = [candle.close for candle in recent]
    fast = mean(closes[-3:])
    slow = mean(closes)
    trend = "up" if fast > slow else "down"
    momentum_pct = (closes[-1] / closes[-4] - 1) * 100 if closes[-4] > 0 else 0.0
    prior_volumes = [candle.volume_delta_usd for candle in recent[:-1]]
    average_volume = mean(prior_volumes) if any(prior_volumes) else 0.0
    volume_ratio = recent[-1].volume_delta_usd / average_volume if average_volume > 0 else 0.0
    liquidity = recent[-1].liquidity_usd

    score = 0
    reasons: list[str] = []
    enabled_weight = 0
    if "trend" in disabled_factors:
        reasons.append("trend factor disabled for ablation")
    elif trend == "up":
        enabled_weight += 30
        score += 30
        reasons.append("3-candle mean is above the 8-candle trend (+30)")
    else:
        enabled_weight += 30
        reasons.append("short trend is not above the baseline (+0)")
    if "momentum" in disabled_factors:
        reasons.append("momentum factor disabled for ablation")
    elif 0.5 <= momentum_pct <= 35:
        enabled_weight += 25
        score += 25
        reasons.append(f"3-candle momentum is {momentum_pct:+.2f}% (+25)")
    elif momentum_pct > 35:
        enabled_weight += 25
        reasons.append(f"momentum is overheated at {momentum_pct:+.2f}% (+0)")
    else:
        enabled_weight += 25
        reasons.append(f"momentum is weak at {momentum_pct:+.2f}% (+0)")
    if "volume" in disabled_factors:
        reasons.append("volume factor disabled for ablation")
    elif volume_ratio >= 1.2:
        enabled_weight += 20
        score += 20
        reasons.append(f"trade volume is {volume_ratio:.2f}x baseline (+20)")
    else:
        enabled_weight += 20
        reasons.append(f"trade volume is {volume_ratio:.2f}x baseline (+0)")
    if "liquidity" in disabled_factors:
        reasons.append("liquidity scoring disabled for ablation; hard floor remains")
    elif liquidity >= minimum_liquidity_usd:
        enabled_weight += 25
        score += 25
        reasons.append(f"liquidity is ${liquidity:,.0f} (+25)")
    else:
        enabled_weight += 25
        reasons.append(f"liquidity ${liquidity:,.0f} is below ${minimum_liquidity_usd:,.0f} (+0)")
    normalized_score = round(score * 100 / enabled_weight) if enabled_weight else 0
    confirmed = (
        normalized_score >= signal_threshold
        and ("trend" in disabled_factors or trend == "up")
        and liquidity >= minimum_liquidity_usd
    )
    return EntrySignal(
        status="ready",
        confirmed=confirmed,
        score=normalized_score,
        trend=trend,
        momentum_pct=round(momentum_pct, 4),
        volume_ratio=round(volume_ratio, 4),
        liquidity_usd=liquidity,
        reasons=tuple(reasons),
    )


def multi_frame_entry_verdict(signals: Mapping[Timeframe, EntrySignal]) -> tuple[bool, str]:
    """Shared closed-candle gate for paper decisions and multi-frame replay."""
    required = (signals["1m"], signals["5m"])
    if any(signal.status != "ready" for signal in required):
        return False, "Authentic 1m and 5m candles are missing, stale, or incomplete"
    if not all(signal.confirmed for signal in required):
        return False, "1m/5m trend, momentum, volume, and liquidity did not align"
    if any(signals[frame].status in HIGHER_FRAME_INTEGRITY_FAILURES for frame in ("15m", "1h")):
        return False, "A higher timeframe has conflicting or unverified candle data"
    if any(
        signals[frame].status == "ready" and signals[frame].trend == "down"
        for frame in ("15m", "1h")
    ):
        return False, "a ready higher timeframe is trending down"
    return True, "1m and 5m multi-factor candle confirmation passed"


def _first_entry_block(signals: Mapping[Timeframe, EntrySignal]) -> str:
    """Name the first blocking candle gate without changing the shared verdict."""
    required: tuple[Timeframe, ...] = ("1m", "5m") if "5m" in signals else ("1m",)
    for frame in required:
        if signals[frame].status != "ready":
            return f"{frame}_{signals[frame].status}"
    for frame in required:
        if not signals[frame].confirmed:
            return f"{frame}_unconfirmed"
    for frame in ("15m", "1h"):
        if frame in signals and signals[frame].status in HIGHER_FRAME_INTEGRITY_FAILURES:
            return f"{frame}_{signals[frame].status}"
    for frame in ("15m", "1h"):
        if frame in signals and signals[frame].status == "ready" and signals[frame].trend == "down":
            return f"{frame}_downtrend"
    return "entry_gate_unclassified"


@dataclass(frozen=True, slots=True)
class _ReplayExit:
    index: int
    reason: str
    proceeds: float
    remaining_bps: int
    legs: int
    tp_stage: int


def _paper_polling_exit(
    candles: list[Candle],
    *,
    entry_index: int,
    entry_price: float,
    timeframe: Timeframe,
    policy: ExitPolicy,
    poll_seconds: int,
    minimum_exit_liquidity_usd: float,
) -> _ReplayExit:
    """Approximate quote polling with observed closes and later market-order opens."""
    period = TIMEFRAME_SECONDS[timeframe]
    entry_at = candles[entry_index].opened_at
    next_poll_at = entry_at + poll_seconds
    opens = [candle.opened_at for candle in candles]
    mark_index = entry_index - 1
    remaining_bps = 10_000
    proceeds = 0.0
    legs = 0
    tp_stage = 0
    peak_value = 1_000_000_000
    while next_poll_at <= opens[-1] + period:
        latest_closed = bisect_right(opens, next_poll_at - period) - 1
        candidate_index = latest_closed
        found_mark = False
        while candidate_index > mark_index:
            candidate = candles[candidate_index]
            closed_at = candidate.opened_at + period
            if closed_at < next_poll_at - poll_seconds:
                break
            if (
                _timely_exit_observation(candidate, timeframe)
                and candidate.first_fetched_at is not None
                and candidate.first_observed_values_at is not None
                and candidate.first_fetched_at <= next_poll_at
                and candidate.first_observed_values_at <= next_poll_at
            ):
                found_mark = True
                break
            candidate_index -= 1
        if not found_mark:
            return _ReplayExit(
                max(entry_index, latest_closed),
                "exit_candle_unobserved",
                proceeds,
                remaining_bps,
                legs,
                tp_stage,
            )
        if any(
            opens[i] - opens[i - 1] != period for i in range(mark_index + 1, candidate_index + 1)
        ):
            return _ReplayExit(candidate_index, "data_gap", proceeds, remaining_bps, legs, tp_stage)
        mark_index = candidate_index
        candle = candles[mark_index]
        if candle.liquidity_usd <= 0:
            return _ReplayExit(
                mark_index, "liquidity_unknown", proceeds, remaining_bps, legs, tp_stage
            )
        if candle.liquidity_usd < minimum_exit_liquidity_usd:
            return _ReplayExit(
                mark_index, "thin_exit_liquidity", proceeds, remaining_bps, legs, tp_stage
            )
        if candle.close <= 0:
            return _ReplayExit(mark_index, "invalid_mark", proceeds, remaining_bps, legs, tp_stage)
        current_value = max(1, round(1_000_000_000 * candle.close / entry_price))
        peak_value = max(peak_value, current_value)
        rule = evaluate_exit_rule(
            entry_value=1_000_000_000,
            current_value=current_value,
            peak_value=peak_value,
            tp_stage=tp_stage,
            age_seconds=next_poll_at - entry_at,
            policy=policy,
        )
        if rule is None:
            next_poll_at += poll_seconds
            continue
        fill_index = bisect_right(opens, next_poll_at)
        if fill_index >= len(candles):
            return _ReplayExit(
                len(candles) - 1, "series_end", proceeds, remaining_bps, legs, tp_stage
            )
        if any(opens[i] - opens[i - 1] != period for i in range(mark_index + 1, fill_index + 1)):
            return _ReplayExit(fill_index, "data_gap", proceeds, remaining_bps, legs, tp_stage)
        fill = candles[fill_index]
        # The fill is priced at the next candle's open. That candle's attached
        # liquidity may be a snapshot from later in the candle; use the last
        # observed mark instead of looking ahead to authorize the fill.
        if fill.open <= 0 or candle.liquidity_usd < minimum_exit_liquidity_usd:
            return _ReplayExit(
                fill_index, "unexecutable_fill", proceeds, remaining_bps, legs, tp_stage
            )
        sold_bps = (
            min(rule.sell_bps or 0, remaining_bps) if rule.action == "partial" else remaining_bps
        )
        if sold_bps <= 0:
            return _ReplayExit(
                fill_index, "unexecutable_fill", proceeds, remaining_bps, legs, tp_stage
            )
        proceeds += sold_bps / 10_000 * fill.open / entry_price
        remaining_bps -= sold_bps
        legs += 1
        tp_stage = rule.tp_stage if rule.tp_stage is not None else tp_stage
        if remaining_bps == 0 or rule.action == "close":
            return _ReplayExit(fill_index, rule.reason, proceeds, remaining_bps, legs, tp_stage)
        next_poll_at = fill.opened_at + poll_seconds
    return _ReplayExit(len(candles) - 1, "series_end", proceeds, remaining_bps, legs, tp_stage)


def run_backtest(
    candles: list[Candle],
    *,
    minimum_liquidity_usd: float,
    tp_levels_bps: tuple[int, ...],
    tp_sell_bps: tuple[int, ...],
    stop_loss_bps: int,
    signal_threshold: int = DEFAULT_SIGNAL_THRESHOLD,
    execution_cost_bps: int = 0,
    fixed_cost_bps: int = 0,
    refundable_ata_reserve_bps: int = 0,
    ata_already_reserved: bool = False,
    additional_exit_fee_bps: int = 0,
    disabled_factors: frozenset[FactorName] = frozenset(),
    entry_start_index: int = 8,
    timeframe: Timeframe = "1m",
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
    confirmation_frames: Mapping[Timeframe, list[Candle]] | None = None,
    audit_observations: list[dict[str, object]] | None = None,
    market_snapshots: list[dict[str, object]] | None = None,
    minimum_volume_24h_usd: float = 0,
    expected_pool: str | None = None,
    market_score_replayer: (
        Callable[[dict[str, object], int], tuple[str, int | None] | None] | None
    ) = None,
    exit_model: Literal["intrabar_ohlc", "paper_polling_proxy"] = "intrabar_ohlc",
    paper_poll_seconds: int = 300,
) -> dict[str, Any]:
    if exit_model not in {"intrabar_ohlc", "paper_polling_proxy"}:
        raise StrategyLabError("Exit execution model is invalid")
    if paper_poll_seconds < TIMEFRAME_SECONDS[timeframe]:
        raise StrategyLabError("Paper polling interval is shorter than the candle timeframe")
    if len(tp_levels_bps) != len(tp_sell_bps) or not tp_levels_bps:
        raise StrategyLabError("Take-profit targets and sale portions must have equal length")
    if sum(tp_sell_bps) > 10_000:
        raise StrategyLabError("Take-profit portions cannot exceed 100%")
    if any(level <= 0 for level in tp_levels_bps) or any(portion <= 0 for portion in tp_sell_bps):
        raise StrategyLabError("Take-profit targets and portions must be positive")
    if any(current <= previous for previous, current in pairwise(tp_levels_bps)):
        raise StrategyLabError("Take-profit targets must be strictly increasing")
    if not 0 <= execution_cost_bps <= 5_000:
        raise StrategyLabError("Execution cost must be between 0 and 5000 bps")
    if not 0 <= refundable_ata_reserve_bps <= fixed_cost_bps <= execution_cost_bps:
        raise StrategyLabError("Replay fixed costs and refundable reserve are inconsistent")
    if not 0 <= additional_exit_fee_bps <= 5_000:
        raise StrategyLabError("Additional exit fee must be between 0 and 5000 bps")
    if not 1 <= signal_threshold <= 100:
        raise StrategyLabError("Signal threshold must be between 1 and 100")
    if not 1 <= position_fraction_bps <= 10_000:
        raise StrategyLabError("Position fraction must be between 1 and 10000 bps")
    if not 0 <= trailing_stop_bps < trailing_activation_bps <= 10_000:
        raise StrategyLabError("Trailing stop parameters are invalid")
    if max_hold_seconds < TIMEFRAME_SECONDS[timeframe] or minimum_exit_liquidity_usd < 0:
        raise StrategyLabError("Holding time or exit liquidity limit is invalid")
    if not 0 < stop_loss_bps < 10_000 or max_consecutive_losses < 1:
        raise StrategyLabError("Stop loss or consecutive-loss limit is invalid")
    if not 0 < max_drawdown_bps < 10_000 or not 0 < daily_loss_bps < 10_000:
        raise StrategyLabError("Portfolio breaker limits are invalid")
    if entry_windows is not None and any(start >= end for start, end in entry_windows):
        raise StrategyLabError("Research entry windows are invalid")
    if confirmation_frames is not None and timeframe != "1m":
        raise StrategyLabError("Multi-frame replay requires 1m entry candles")
    if market_snapshots is not None and (not expected_pool or minimum_volume_24h_usd <= 0):
        raise StrategyLabError("Historical market gates require a pool and volume policy")
    if market_score_replayer is not None and (
        market_snapshots is None or audit_observations is None
    ):
        raise StrategyLabError("Historical market scoring requires market and audit history")
    entry_model = (
        "multi_timeframe_candle_gate"
        if confirmation_frames is not None
        else "single_frame_research"
    )
    exit_execution_model = (
        "closed_candle_poll_next_open_proxy"
        if exit_model == "paper_polling_proxy"
        else "intrabar_ohlc_resting_order_hypothesis"
    )
    metadata = {
        "available_candles": len(candles),
        "execution_cost_bps": execution_cost_bps,
        "fixed_cost_bps": fixed_cost_bps,
        "refundable_ata_reserve_bps": refundable_ata_reserve_bps,
        "ata_already_reserved": ata_already_reserved,
        "position_fraction_bps": position_fraction_bps,
        "entry_model": entry_model,
        "point_in_time_candles": True,
        "exit_execution_model": exit_execution_model,
        "paper_poll_seconds": paper_poll_seconds if exit_model == "paper_polling_proxy" else None,
        "paper_exit_rule_parity": exit_model == "paper_polling_proxy",
        "exit_observation_bound_seconds": 120,
        "paper_exit_model_parity": False,
        "historical_market_gate_parity": False,
        "historical_market_hard_gates": market_snapshots is not None,
        "historical_market_score_gate_enabled": market_score_replayer is not None,
        "historical_market_score_evaluations": 0,
        "paper_entry_schedule_parity": False,
    }
    if entry_windows == ():
        return {
            **metadata,
            "status": "outside_research_cohort",
            "reason": "No point-in-time membership in the selected research cohort",
            "trades": 0,
            "win_rate_pct": 0.0,
            "return_pct": 0.0,
            "max_drawdown_pct": 0.0,
            "trade_log": [],
            "censored_trades": 0,
            "censored_log": [],
        }
    if len(candles) < 10:
        return {
            **metadata,
            "status": "insufficient_history",
            "minimum_candles": 10,
            "trades": 0,
            "win_rate_pct": 0.0,
            "return_pct": 0.0,
            "max_drawdown_pct": 0.0,
            "trade_log": [],
            "censored_trades": 0,
            "censored_log": [],
        }
    if any(candle.source != "geckoterminal_pool_trades" for candle in candles):
        return {
            **metadata,
            "status": "unverified_data",
            "trades": 0,
            "return_pct": 0.0,
            "reason": "Backtests require authentic pool OHLCV",
            "trade_log": [],
            "censored_trades": 0,
            "censored_log": [],
        }

    audit_times: list[int] = []
    if audit_observations is not None:
        for observation in audit_observations:
            observed_at_us = observation.get("observed_at_us")
            status = observation.get("audit_status")
            if (
                not isinstance(observed_at_us, int)
                or isinstance(observed_at_us, bool)
                or observed_at_us < 0
                or not isinstance(status, str)
                or status not in {"safe", "blocked", "error"}
            ):
                raise StrategyLabError("Historical audit observation is invalid")
            audit_times.append(observed_at_us)
        if any(right < left for left, right in pairwise(audit_times)):
            raise StrategyLabError("Historical audit observations must be ordered")
        if (
            not audit_times
            or audit_times[0] > (candles[-2].opened_at + TIMEFRAME_SECONDS[timeframe]) * 1_000_000
        ):
            return {
                **metadata,
                "status": "insufficient_audit_history",
                "trades": 0,
                "return_pct": 0.0,
                "reason": "No audit outcome was recorded before the replay entry window",
                "trade_log": [],
                "censored_trades": 0,
                "censored_log": [],
                "historical_safety_audit": "point_in_time_required",
            }

    recorded_markets: list[dict[str, object]] = []
    market_times: list[int] = []
    if market_snapshots is not None:
        recorded_markets = sorted(
            (
                snapshot
                for snapshot in market_snapshots
                if type(snapshot.get("captured_at")) is int
                and int(str(snapshot["captured_at"])) >= 0
            ),
            key=lambda snapshot: int(str(snapshot["captured_at"])),
        )
        market_times = [int(str(snapshot["captured_at"])) for snapshot in recorded_markets]
        if (
            not market_times
            or market_times[0] > candles[-2].opened_at + TIMEFRAME_SECONDS[timeframe]
        ):
            return {
                **metadata,
                "status": "insufficient_market_history",
                "trades": 0,
                "return_pct": 0.0,
                "reason": "No market snapshot was captured before the replay entry window",
                "trade_log": [],
                "censored_trades": 0,
                "censored_log": [],
            }

    frame_closes: dict[Timeframe, list[int]] = {}
    confirmation_names: tuple[Timeframe, ...] = ("5m", "15m", "1h")
    if confirmation_frames is not None:
        for frame in confirmation_names:
            rows = confirmation_frames.get(frame, [])
            if any(candle.source != "geckoterminal_pool_trades" for candle in rows):
                return {
                    **metadata,
                    "status": "unverified_data",
                    "trades": 0,
                    "return_pct": 0.0,
                    "reason": "Multi-frame replay requires authentic pool OHLCV",
                    "trade_log": [],
                    "censored_trades": 0,
                    "censored_log": [],
                }
            if any(right.opened_at <= left.opened_at for left, right in pairwise(rows)):
                raise StrategyLabError("Confirmation candles must be chronologically ordered")
            frame_closes[frame] = [candle.opened_at + TIMEFRAME_SECONDS[frame] for candle in rows]
        if len(confirmation_frames.get("5m", [])) < 8:
            return {
                **metadata,
                "status": "insufficient_confirmation_history",
                "trades": 0,
                "return_pct": 0.0,
                "reason": "Need eight authentic closed 5m candles",
                "trade_log": [],
                "censored_trades": 0,
                "censored_log": [],
            }

    final_decision_at = candles[-2].opened_at + TIMEFRAME_SECONDS[timeframe]
    observed_entry_bars = sum(
        _available_for_replay(candle, final_decision_at, timeframe) for candle in candles[:-1]
    )
    metadata["observed_entry_candles_by_final_decision"] = observed_entry_bars
    if observed_entry_bars < 8:
        return {
            **metadata,
            "status": "insufficient_candle_availability",
            "trades": 0,
            "return_pct": 0.0,
            "max_drawdown_pct": 0.0,
            "reason": "Fewer than eight entry candles were observed before replay ended",
            "trade_log": [],
            "censored_trades": 0,
            "censored_log": [],
        }
    if confirmation_frames is not None:
        observed_five_minute_bars = sum(
            candle.opened_at + TIMEFRAME_SECONDS["5m"] <= final_decision_at
            and _available_for_replay(candle, final_decision_at, "5m")
            for candle in confirmation_frames.get("5m", [])
        )
        metadata["observed_confirmation_candles_by_final_decision"] = observed_five_minute_bars
        if observed_five_minute_bars < 8:
            return {
                **metadata,
                "status": "insufficient_confirmation_history",
                "trades": 0,
                "return_pct": 0.0,
                "max_drawdown_pct": 0.0,
                "reason": "Fewer than eight 5m candles were observed before the replay ended",
                "trade_log": [],
                "censored_trades": 0,
                "censored_log": [],
            }

    cash = 1.0
    peak_equity = 1.0
    max_drawdown = 0.0
    trade_log: list[dict[str, Any]] = []
    rent_reclaimed_returns: list[float] = []
    censored_log: list[dict[str, Any]] = []
    consecutive_losses = 0
    breaker_reason: str | None = None
    trading_day: int | None = None
    day_start_cash = cash
    entry_decisions_in_window = 0
    market_score_evaluations = 0
    entry_rejection_counts: Counter[str] = Counter()
    index = max(7, entry_start_index - 1)
    while index < len(candles) - 1:
        day = candles[index].opened_at // 86_400
        if day != trading_day:
            trading_day = day
            day_start_cash = cash
        decision_at = candles[index].opened_at + TIMEFRAME_SECONDS[timeframe]
        candidate_entry_at = candles[index + 1].opened_at
        in_entry_window = (entry_not_before is None or candidate_entry_at >= entry_not_before) and (
            entry_windows is None
            or any(start <= candidate_entry_at < end for start, end in entry_windows)
        )
        if in_entry_window:
            entry_decisions_in_window += 1
        # A backfilled candle is not usable until it was first observed locally.
        # Require observation before the decision second, then fill no earlier
        # than the following bar's open.
        visible = [
            candle
            for candle in candles[: index + 1]
            if _available_for_replay(candle, decision_at, timeframe)
        ]
        signal = analyze_entry(
            visible[-8:],
            minimum_liquidity_usd=minimum_liquidity_usd,
            timeframe=timeframe,
            signal_threshold=signal_threshold,
            disabled_factors=disabled_factors,
            as_of=decision_at,
        )
        eligible = signal.confirmed
        signals: dict[Timeframe, EntrySignal] = {"1m": signal}
        if confirmation_frames is not None:
            for frame in confirmation_names:
                rows = confirmation_frames.get(frame, [])
                end = bisect_right(frame_closes[frame], decision_at)
                visible_frame = [
                    candle
                    for candle in rows[:end]
                    if _available_for_replay(candle, decision_at, frame)
                ]
                signals[frame] = analyze_entry(
                    visible_frame[-8:],
                    minimum_liquidity_usd=minimum_liquidity_usd,
                    timeframe=frame,
                    signal_threshold=signal_threshold,
                    disabled_factors=disabled_factors,
                    as_of=decision_at,
                )
            eligible, _ = multi_frame_entry_verdict(signals)
        if not eligible:
            if in_entry_window:
                entry_rejection_counts[_first_entry_block(signals)] += 1
            index += 1
            continue
        if audit_observations is not None:
            audit_index = bisect_right(audit_times, decision_at * 1_000_000) - 1
            if audit_index < 0:
                if in_entry_window:
                    entry_rejection_counts["audit_missing"] += 1
                index += 1
                continue
            audit = audit_observations[audit_index]
            observed_at_us = audit_times[audit_index]
            audit_pool = audit.get("pair_address")
            if (
                decision_at * 1_000_000 - observed_at_us > 600_000_000
                or audit.get("audit_status") != "safe"
                or (expected_pool is not None and audit_pool != expected_pool)
            ):
                if in_entry_window:
                    if decision_at * 1_000_000 - observed_at_us > 600_000_000:
                        entry_rejection_counts["audit_stale"] += 1
                    elif audit.get("audit_status") != "safe":
                        entry_rejection_counts["audit_not_safe"] += 1
                    else:
                        entry_rejection_counts["audit_pool_mismatch"] += 1
                index += 1
                continue
        market_score_value: int | None = None
        if market_snapshots is not None:
            market_index = bisect_right(market_times, decision_at) - 1
            market_snapshot = recorded_markets[market_index] if market_index >= 0 else None
            market_block = _historical_market_block(
                market_snapshot,
                decision_at=decision_at,
                expected_pool=expected_pool or "",
                minimum_liquidity_usd=minimum_liquidity_usd,
                minimum_volume_24h_usd=minimum_volume_24h_usd,
            )
            if market_block is not None:
                if in_entry_window:
                    entry_rejection_counts[market_block] += 1
                index += 1
                continue
            if market_score_replayer is not None and market_snapshot is not None:
                market_score_evaluations += 1
                replayed = market_score_replayer(market_snapshot, decision_at * 1_000)
                if replayed is None:
                    market_block = "narrative_history_missing"
                elif replayed[0] in {
                    "market_policy_missing",
                    "market_policy_stale",
                    "market_policy_mismatch",
                    "market_policy_invalid",
                }:
                    market_block = replayed[0]
                elif (
                    replayed[0] not in {"eligible", "observe", "reject"}
                    or type(replayed[1]) is not int
                    or not 0 <= replayed[1] <= 100
                ):
                    market_block = "narrative_history_invalid"
                else:
                    market_score_value = replayed[1]
                    if replayed[0] != "eligible":
                        market_block = f"market_score_{replayed[0]}"
                if market_block is not None:
                    if in_entry_window:
                        entry_rejection_counts[market_block] += 1
                    index += 1
                    continue
        # The decision is known only after this candle closes. The earliest
        # possible fill is the following candle's open.
        entry_index = index + 1
        if entry_not_before is not None and candles[entry_index].opened_at < entry_not_before:
            index += 1
            continue
        if entry_windows is not None and not any(
            start <= candles[entry_index].opened_at < end for start, end in entry_windows
        ):
            index += 1
            continue
        if (
            candles[entry_index].opened_at - candles[index].opened_at
            != TIMEFRAME_SECONDS[timeframe]
        ):
            if in_entry_window:
                entry_rejection_counts["entry_candle_gap"] += 1
            index += 1
            continue
        entry = candles[entry_index].open
        remaining_bps = 10_000
        proceeds = 0.0
        exit_legs = 0
        next_target = 0
        exit_index = entry_index
        exit_reason = "series_end"
        peak_price = entry
        if exit_model == "paper_polling_proxy":
            outcome = _paper_polling_exit(
                candles,
                entry_index=entry_index,
                entry_price=entry,
                timeframe=timeframe,
                policy=ExitPolicy(
                    tp_levels_bps=tp_levels_bps,
                    tp_sell_bps=tp_sell_bps,
                    stop_loss_bps=stop_loss_bps,
                    trailing_activation_bps=trailing_activation_bps,
                    trailing_stop_bps=trailing_stop_bps,
                    max_hold_seconds=max_hold_seconds,
                ),
                poll_seconds=paper_poll_seconds,
                minimum_exit_liquidity_usd=minimum_exit_liquidity_usd,
            )
            exit_index = outcome.index
            exit_reason = outcome.reason
            proceeds = outcome.proceeds
            remaining_bps = outcome.remaining_bps
            exit_legs = outcome.legs
            next_target = outcome.tp_stage
        while exit_model == "intrabar_ohlc" and exit_index < len(candles):
            candle = candles[exit_index]
            if (
                exit_index > entry_index
                and candle.opened_at - candles[exit_index - 1].opened_at
                != TIMEFRAME_SECONDS[timeframe]
            ):
                exit_reason = "data_gap"
                break
            if not _timely_exit_observation(candle, timeframe):
                exit_reason = "exit_candle_unobserved"
                break
            # A candle's attached liquidity may come from a snapshot captured
            # after its open. Intrabar orders can use only prior bars whose
            # values were already observed before this bar opened.
            known_liquidity_bar = next(
                (
                    prior
                    for prior in reversed(candles[:exit_index])
                    if _available_for_replay(prior, candle.opened_at, timeframe)
                ),
                None,
            )
            known_liquidity = (
                known_liquidity_bar.liquidity_usd
                if known_liquidity_bar is not None
                and candle.opened_at
                - (known_liquidity_bar.opened_at + TIMEFRAME_SECONDS[timeframe])
                <= TIMEFRAME_SECONDS[timeframe] + 120
                else 0.0
            )
            if known_liquidity <= 0:
                exit_reason = "liquidity_unknown"
                break
            stop_price = entry * (1 - stop_loss_bps / 10_000)
            if peak_price >= entry * (1 + trailing_activation_bps / 10_000):
                stop_price = max(stop_price, peak_price * (1 - trailing_stop_bps / 10_000))
            # Candle paths are unknowable from OHLC. Check the stop first so replay
            # cannot claim an optimistic TP when both stop and target were touched.
            if candle.open <= stop_price:
                proceeds += remaining_bps / 10_000 * (candle.open / entry)
                remaining_bps = 0
                exit_legs += 1
                exit_reason = (
                    "trailing_gap"
                    if stop_price > entry * (1 - stop_loss_bps / 10_000)
                    else "stop_gap"
                )
                break
            if candle.low <= stop_price:
                proceeds += remaining_bps / 10_000 * (stop_price / entry)
                remaining_bps = 0
                exit_legs += 1
                exit_reason = (
                    "trailing_stop"
                    if stop_price > entry * (1 - stop_loss_bps / 10_000)
                    else "stop_loss"
                )
                break
            while next_target < len(tp_levels_bps):
                target_price = entry * (1 + tp_levels_bps[next_target] / 10_000)
                if candle.high < target_price:
                    break
                sale_bps = min(tp_sell_bps[next_target], remaining_bps)
                if sale_bps:
                    proceeds += sale_bps / 10_000 * (target_price / entry)
                    remaining_bps -= sale_bps
                    exit_legs += 1
                next_target += 1
            if remaining_bps == 0:
                exit_reason = "tp_ladder_complete"
                break
            if known_liquidity < minimum_exit_liquidity_usd:
                proceeds += remaining_bps / 10_000 * (candle.close / entry)
                remaining_bps = 0
                exit_legs += 1
                exit_reason = "liquidity_breaker"
                break
            if candle.opened_at - candles[entry_index].opened_at >= max_hold_seconds:
                proceeds += remaining_bps / 10_000 * (candle.close / entry)
                remaining_bps = 0
                exit_legs += 1
                exit_reason = "max_hold_time"
                break
            peak_price = max(peak_price, candle.high)
            exit_index += 1
        if exit_reason in {
            "series_end",
            "data_gap",
            "liquidity_unknown",
            "exit_candle_unobserved",
            "thin_exit_liquidity",
            "invalid_mark",
            "unexecutable_fill",
        }:
            censored_log.append(
                {
                    "signal_at": visible[-1].opened_at,
                    "decision_at": decision_at,
                    "entry_at": candles[entry_index].opened_at,
                    "last_observed_at": candles[min(exit_index, len(candles) - 1)].opened_at,
                    "entry_price": entry,
                    "reason": exit_reason,
                    "tp_stages": next_target,
                    "signal_score": signal.score,
                    "market_score": market_score_value,
                }
            )
            # An unclosed position cannot be assigned a realized return or a
            # reliable next entry. Stop this replay until more evidence exists.
            break
        if remaining_bps:
            final_close = candles[min(exit_index, len(candles) - 1)].close
            proceeds += remaining_bps / 10_000 * (final_close / entry)
            exit_legs += 1
        gross_proceeds = proceeds
        # Price-dependent slippage scales with proceeds. Network fees and the
        # token-account reserve are fixed cash outlays relative to swap input.
        # A simulated ATA remains open, so the same mint reserves rent once.
        rent_charge_bps = (
            refundable_ata_reserve_bps if not ata_already_reserved and not trade_log else 0
        )
        charged_fixed_bps = fixed_cost_bps - refundable_ata_reserve_bps + rent_charge_bps
        proceeds *= 1 - (execution_cost_bps - fixed_cost_bps) / 10_000
        proceeds -= charged_fixed_bps / 10_000
        extra_exit_cost_bps = max(0, exit_legs - 1) * additional_exit_fee_bps
        proceeds -= extra_exit_cost_bps / 10_000
        trade_return = proceeds - 1.0
        return_if_rent_reclaimed = trade_return + rent_charge_bps / 10_000
        rent_reclaimed_returns.append(return_if_rent_reclaimed * 100)
        cash *= 1 + position_fraction_bps / 10_000 * trade_return
        consecutive_losses = consecutive_losses + 1 if trade_return < 0 else 0
        peak_equity = max(peak_equity, cash)
        max_drawdown = max(max_drawdown, (peak_equity - cash) / peak_equity)
        trade_log.append(
            {
                "signal_at": visible[-1].opened_at,
                "decision_at": decision_at,
                "entry_at": candles[entry_index].opened_at,
                "exit_at": candles[min(exit_index, len(candles) - 1)].opened_at,
                "entry_price": entry,
                "return_pct": round(trade_return * 100, 4),
                "gross_return_pct": round((gross_proceeds - 1.0) * 100, 4),
                "return_if_full_rent_reclaimed_pct": round(return_if_rent_reclaimed * 100, 4),
                "rent_reserved_bps": rent_charge_bps,
                "fixed_cost_bps": charged_fixed_bps,
                "exit_legs": exit_legs,
                "additional_exit_cost_bps": extra_exit_cost_bps,
                "exit_reason": exit_reason,
                "tp_stages": next_target,
                "signal_score": signal.score,
                "market_score": market_score_value,
                "holding_seconds": (
                    candles[min(exit_index, len(candles) - 1)].opened_at
                    - candles[entry_index].opened_at
                ),
                "regime": classify_regime(visible[-8:]),
            }
        )
        if consecutive_losses >= max_consecutive_losses:
            breaker_reason = "consecutive_loss_limit"
        elif max_drawdown * 10_000 >= max_drawdown_bps:
            breaker_reason = "portfolio_drawdown_limit"
        elif (day_start_cash - cash) / day_start_cash * 10_000 >= daily_loss_bps:
            breaker_reason = "daily_loss_limit"
        if breaker_reason:
            break
        index = max(index + 1, exit_index + 1)

    wins = sum(1 for trade in trade_log if float(trade["return_pct"]) > 0)
    returns = [float(trade["return_pct"]) for trade in trade_log]
    gross_profit = sum(value for value in returns if value > 0)
    gross_loss = abs(sum(value for value in returns if value < 0))
    wins_only = [value for value in returns if value > 0]
    losses_only = [value for value in returns if value < 0]
    rent_reclaimed_cash = 1.0
    rent_reclaimed_peak = 1.0
    rent_reclaimed_drawdown = 0.0
    for value in rent_reclaimed_returns:
        rent_reclaimed_cash *= 1 + position_fraction_bps / 10_000 * value / 100
        rent_reclaimed_peak = max(rent_reclaimed_peak, rent_reclaimed_cash)
        rent_reclaimed_drawdown = max(
            rent_reclaimed_drawdown,
            (rent_reclaimed_peak - rent_reclaimed_cash) / rent_reclaimed_peak,
        )
    rent_reclaimed_profits = sum(value for value in rent_reclaimed_returns if value > 0)
    rent_reclaimed_losses = abs(sum(value for value in rent_reclaimed_returns if value < 0))
    return {
        "status": "complete",
        "minimum_candles": 10,
        "available_candles": len(candles),
        "trades": len(trade_log),
        "win_rate_pct": round(wins / len(trade_log) * 100, 2) if trade_log else 0.0,
        "return_pct": round((cash - 1) * 100, 4),
        "max_drawdown_pct": round(max_drawdown * 100, 4),
        "expectancy_pct": round(mean(returns), 4) if returns else 0.0,
        "average_win_pct": round(mean(wins_only), 4) if wins_only else 0.0,
        "average_loss_pct": round(mean(losses_only), 4) if losses_only else 0.0,
        "average_holding_seconds": (
            round(mean(float(trade["holding_seconds"]) for trade in trade_log)) if trade_log else 0
        ),
        "profit_factor": round(gross_profit / gross_loss, 4) if gross_loss > 0 else None,
        "signal_threshold": signal_threshold,
        "execution_cost_bps": execution_cost_bps,
        "fixed_cost_bps": fixed_cost_bps,
        "refundable_ata_reserve_bps": refundable_ata_reserve_bps,
        "ata_already_reserved": ata_already_reserved,
        "full_rent_recovery_sensitivity": {
            "status": (
                "same_cash_trade_path_hypothetical_refund" if trade_log else "no_closed_trades"
            ),
            "assumption": (
                "Full refund of the first simulated ATA reserve after closure; "
                "entry, exit, and cash-based breakers keep the original path"
            ),
            "trades": len(trade_log),
            "win_rate_pct": (
                round(sum(value > 0 for value in rent_reclaimed_returns) / len(trade_log) * 100, 2)
                if trade_log
                else None
            ),
            "profit_factor": (
                round(rent_reclaimed_profits / rent_reclaimed_losses, 4)
                if rent_reclaimed_losses
                else None
            ),
            "return_pct": round((rent_reclaimed_cash - 1) * 100, 4) if trade_log else None,
            "max_drawdown_pct": round(rent_reclaimed_drawdown * 100, 4) if trade_log else None,
            "expectancy_pct": round(mean(rent_reclaimed_returns), 4) if trade_log else None,
        },
        "additional_exit_fee_bps": additional_exit_fee_bps,
        "position_fraction_bps": position_fraction_bps,
        "drawdown_method": "closed_trade_equity_only",
        "performance_scope": "closed_trades_only",
        "entry_not_before": entry_not_before,
        "research_entry_window_count": len(entry_windows) if entry_windows is not None else None,
        "entry_decisions_in_window": entry_decisions_in_window,
        "entry_rejection_counts": dict(sorted(entry_rejection_counts.items())),
        "breaker_reason": breaker_reason,
        "disabled_factors": sorted(disabled_factors),
        "entry_model": entry_model,
        "point_in_time_candles": True,
        "exit_execution_model": exit_execution_model,
        "paper_poll_seconds": paper_poll_seconds if exit_model == "paper_polling_proxy" else None,
        "paper_exit_rule_parity": exit_model == "paper_polling_proxy",
        "exit_observation_bound_seconds": 120,
        "paper_exit_model_parity": False,
        "historical_market_gate_parity": False,
        "historical_market_hard_gates": market_snapshots is not None,
        "historical_market_score_gate_enabled": market_score_replayer is not None,
        "historical_market_score_evaluations": market_score_evaluations,
        "paper_entry_schedule_parity": False,
        "historical_safety_audit": (
            "point_in_time_required" if audit_observations is not None else "not_evaluated"
        ),
        "trade_log": trade_log,
        "censored_trades": len(censored_log),
        "censored_log": censored_log,
    }


def classify_regime(candles: list[Candle]) -> str:
    if len(candles) < 4:
        return "unknown"
    closes = [candle.close for candle in candles]
    returns = [
        abs(current / previous - 1) * 100 for previous, current in pairwise(closes) if previous
    ]
    average_move = mean(returns) if returns else 0.0
    momentum = (closes[-1] / closes[0] - 1) * 100 if closes[0] else 0.0
    if average_move >= 8 or abs(momentum) >= 35:
        return "volatile"
    fast = mean(closes[-3:])
    slow = mean(closes)
    if momentum >= 2 and fast > slow:
        return "trending_up"
    if momentum <= -2 and fast < slow:
        return "trending_down"
    return "range"
