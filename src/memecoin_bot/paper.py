from __future__ import annotations

import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import TYPE_CHECKING, Any, Literal

from solders.pubkey import Pubkey

from .audit import blocking_messages
from .config import Settings
from .constants import WSOL_MINT
from .discovery import discover_recent_mints
from .execution import (
    PaperExecutionError,
    PaperExecutionModel,
    UneconomicPaperExitError,
    paper_execution_failure_reason,
    quote_swap_usd,
)
from .forward_performance import has_signal_evidence, summarize_forward_paper
from .hot_radar import evaluate_hot_market
from .journal import TradeJournal
from .jupiter import JupiterClient, JupiterError
from .market_data import DexScreenerClient, MarketDataError
from .pump_fun import PumpFunClient, PumpFunError
from .rpc import RpcClient, RpcError
from .service import TradingService, audit_as_dict
from .strategy_lab import RESEARCH_COHORT_KEY, research_cohort_mints
from .telemetry import record_mark_quote_attempt, record_mark_quote_success

if TYPE_CHECKING:
    from .execution import PaperFillEstimate
    from .models import RouteQuote
    from .strategy import StrategyDecision


class PaperTradingError(RuntimeError):
    """Raised when a paper-trading operation violates portfolio rules."""


class StalePaperDecisionError(PaperTradingError):
    """An automated entry lost its eligible signal before the paper fill committed."""


MAX_PAPER_DECISION_AGE_SECONDS = 120
MAX_SOL_PRICE_SLOT_LAG = 150
MINT_ACCOUNT_BATCH_SIZE = 8


def _decision_is_recent(evidence: dict[str, object]) -> bool:
    as_of = evidence.get("decision_as_of")
    market_at = evidence.get("market_evaluated_at_ms")
    now_ms = int(datetime.now(UTC).timestamp() * 1_000)
    return (
        type(as_of) is int
        and type(market_at) is int
        and -30_000 <= now_ms - as_of * 1_000 <= MAX_PAPER_DECISION_AGE_SECONDS * 1_000
        and -30_000 <= now_ms - market_at <= MAX_PAPER_DECISION_AGE_SECONDS * 1_000
    )


def _stored_int(value: object, label: str) -> int:
    if not isinstance(value, int):
        raise PaperTradingError(f"Stored paper {label} is invalid")
    return value


def _recent_paper_mark(position: dict[str, object], now: datetime) -> bool:
    marked_at = position.get("last_quoted_at")
    value = position.get("last_value_lamports")
    if not isinstance(marked_at, str) or not isinstance(value, int) or value <= 0:
        return False
    try:
        quoted = datetime.fromisoformat(marked_at)
    except ValueError:
        return False
    if quoted.tzinfo is None:
        return False
    return -30 <= (now - quoted).total_seconds() <= 180


def scan_candidates(
    *,
    settings: Settings,
    rpc: RpcClient,
    trading: TradingService,
    journal: TradeJournal,
    reaudit_priority: Callable[[dict[str, object]], bool] | None = None,
) -> dict[str, Any]:
    source_lists: list[tuple[str, list[str]]] = []
    warnings: list[str] = []
    pump_api_status = "not_configured"
    if settings.pump_api_key:
        try:
            with PumpFunClient(settings.pump_api_key) as pump_client:
                pump_mints = pump_client.new_callout_mints(limit=settings.discovery_candidate_limit)
            source_lists.append(("pumpfun_api", pump_mints))
            pump_api_status = "connected"
        except PumpFunError:
            pump_api_status = "degraded"
            warnings.append("pump.fun API unavailable; using verified on-chain fallback")

    for program_id in settings.discovery_program_ids:
        mints = discover_recent_mints(
            rpc,
            [program_id],
            signatures_per_program=settings.discovery_signatures_per_program,
            max_transactions=settings.discovery_max_transactions,
        )
        source_lists.append((program_id, mints))

    dex_feed_status: dict[str, str] = {
        "profiles": "disabled",
        "latest_boosts": "disabled",
        "top_boosts": "disabled",
        "community_takeovers": "disabled",
    }
    if settings.dexscreener_discovery_enabled:
        feeds = (
            ("profiles", "dexscreener_profile", "latest_solana_profiles"),
            ("latest_boosts", "dexscreener_boost", "latest_solana_boosts"),
            ("top_boosts", "dexscreener_top_boost", "top_solana_boosts"),
            (
                "community_takeovers",
                "dexscreener_takeover",
                "latest_solana_takeovers",
            ),
        )
        with DexScreenerClient() as discovery_client:
            for status_key, source_name, method_name in feeds:
                try:
                    method = getattr(discovery_client, method_name)
                    mints = method(limit=settings.discovery_candidate_limit)
                    source_lists.append((source_name, mints))
                    dex_feed_status[status_key] = "connected"
                except MarketDataError:
                    dex_feed_status[status_key] = "degraded"
                    warnings.append(f"DEX Screener {status_key} feed is temporarily unavailable")

    # Round-robin sources so one busy provider cannot crowd every other provider
    # out of the bounded audit budget.
    source_by_mint: dict[str, str] = {}
    max_source_length = max((len(mints) for _source, mints in source_lists), default=0)
    for index in range(max_source_length):
        for source, mints in source_lists:
            if index >= len(mints):
                continue
            source_by_mint.setdefault(mints[index], source)
            if len(source_by_mint) >= settings.discovery_candidate_limit:
                break
        if len(source_by_mint) >= settings.discovery_candidate_limit:
            break

    # Keep point-in-time audit evidence for the fixed research sample even after
    # its mints fall outside the latest discovery view. Missing observations go
    # first; older observations follow. The 10-slot cap bounds RPC use per cycle.
    now = datetime.now(UTC)
    cohort_reaudits: set[str] = set()
    cohort_prior_status: dict[str, str] = {}
    cohort_state = journal.runtime_state(RESEARCH_COHORT_KEY) or {}
    expires_at = cohort_state.get("expires_at")
    if isinstance(expires_at, int) and now.timestamp() < expires_at:
        cohort_candidates: list[tuple[int, str, dict[str, object]]] = []
        for mint in research_cohort_mints(journal):
            if mint in source_by_mint:
                continue
            candidate = journal.discovery_candidate(mint)
            if candidate is None:
                continue
            observations = journal.audit_observations(mint, limit=1)
            raw_observed = observations[0]["observed_at_us"] if observations else None
            observed_us = raw_observed if isinstance(raw_observed, int) else -1
            if observed_us >= 0 and now.timestamp() * 1_000_000 - observed_us < 120_000_000:
                continue
            cohort_candidates.append((observed_us, mint, candidate))
        cohort_budget = min(10, 40 - len(source_by_mint))
        for _observed_us, mint, candidate in sorted(cohort_candidates)[:cohort_budget]:
            source_by_mint[mint] = str(candidate["source_program"])
            cohort_prior_status[mint] = str(candidate["audit_status"])
            cohort_reaudits.add(mint)

    # New discoveries keep their full audit budget. Use remaining slots for
    # older safe candidates. Freshly quoted market-eligible candidates go first,
    # then the oldest other audits; no cached audit is treated as a new pass.
    watchlist_reaudits: set[str] = set()
    priority_watchlist_reaudits = 0
    watchlist_budget = min(8, 40 - len(source_by_mint))
    focus_state = journal.runtime_state("ohlcv_active_focus") or {}
    focus_selected_at = focus_state.get("selected_at")
    raw_focus_mints = focus_state.get("mints")
    focused_mints = (
        {mint for mint in raw_focus_mints if isinstance(mint, str)}
        if isinstance(raw_focus_mints, list)
        and type(focus_selected_at) is int
        and 0 <= now.timestamp() - focus_selected_at < 3_600
        else set()
    )
    watchlist_candidates: list[tuple[bool, datetime, dict[str, object]]] = []
    for candidate in journal.discovery_candidates(limit=100):
        mint = str(candidate["mint"])
        if (
            mint in source_by_mint
            or candidate.get("audit_status") != "safe"
            or not candidate.get("pair_address")
        ):
            continue
        try:
            audited_at = datetime.fromisoformat(str(candidate["last_seen"]))
        except ValueError:
            continue
        age = (now - audited_at).total_seconds() if audited_at.tzinfo else None
        if age is None or age < 120:
            continue
        priority = False
        if reaudit_priority is not None:
            snapshots = journal.market_snapshots(mint, limit=1)
            snapshot = snapshots[-1] if snapshots else None
            captured_at = snapshot.get("captured_at") if snapshot else None
            market_age = now.timestamp() - captured_at if isinstance(captured_at, int) else None
            if (
                snapshot is not None
                and snapshot.get("pool") == candidate.get("pair_address")
                and market_age is not None
                and -30 <= market_age <= 180
            ):
                try:
                    priority = bool(reaudit_priority(candidate))
                except Exception:
                    priority = False
                    warning = "Watchlist priority check failed; using audit age order"
                    if warning not in warnings:
                        warnings.append(warning)
        watchlist_candidates.append((priority, audited_at, candidate))
    # Keep the two continuously observed paper subjects audit-fresh when they
    # also have current market evidence. Other priority subjects retain their
    # oldest-audit order inside the unchanged eight-slot watchlist budget.
    watchlist_candidates.sort(
        key=lambda item: (
            not (item[0] and str(item[2]["mint"]) in focused_mints),
            not item[0],
            item[1],
            str(item[2]["mint"]),
        )
    )
    for priority, _audited_at, candidate in watchlist_candidates[:watchlist_budget]:
        mint = str(candidate["mint"])
        source_by_mint[mint] = str(candidate["source_program"])
        watchlist_reaudits.add(mint)
        priority_watchlist_reaudits += int(priority)

    selected = list(source_by_mint)
    market_by_mint: dict[str, Any] = {}
    market_status = "idle"
    if selected:
        try:
            with DexScreenerClient() as market_client:
                for start in range(0, len(selected), 30):
                    market_by_mint.update(market_client.tokens(selected[start : start + 30]))
            market_status = "connected"
        except MarketDataError:
            market_status = "degraded"
            warnings.append("DEX Screener market enrichment is temporarily unavailable")

    safe_count = 0
    blocked_count = 0
    batched_mint_accounts = 0
    for start in range(0, len(selected), MINT_ACCOUNT_BATCH_SIZE):
        batch = selected[start : start + MINT_ACCOUNT_BATCH_SIZE]
        mint_accounts: dict[str, dict[str, Any]] = {}
        if isinstance(trading, TradingService):
            try:
                result = rpc.call(
                    "getMultipleAccounts",
                    [batch, {"encoding": "jsonParsed", "commitment": "confirmed"}],
                    attempts=1,
                )
                values = result.get("value") if isinstance(result, dict) else None
                if isinstance(values, list) and len(values) == len(batch):
                    mint_accounts = {
                        mint: account
                        for mint, account in zip(batch, values, strict=True)
                        if isinstance(account, dict)
                    }
                    batched_mint_accounts += len(mint_accounts)
            except RpcError:
                # A failed batch must never turn into a passed audit. The normal
                # per-mint lookup remains the fallback below.
                pass
        for mint in batch:
            market = market_by_mint.get(mint)
            market_payload = market.as_dict() if market else None
            try:
                if isinstance(trading, TradingService):
                    origin_trace_enabled = False
                    if market_payload is not None:
                        try:
                            origin_trace_enabled = (
                                Decimal(str(market_payload.get("liquidity_usd")))
                                >= settings.strategy_min_liquidity_usd
                                and Decimal(str(market_payload.get("volume_24h_usd")))
                                >= settings.strategy_min_volume_24h_usd
                            )
                        except (InvalidOperation, TypeError):
                            origin_trace_enabled = False
                    audit = trading.audit(
                        mint,
                        mint_account=mint_accounts.get(mint),
                        origin_trace_enabled=origin_trace_enabled,
                    )
                else:
                    audit = trading.audit(mint)
                audit_status = "safe" if audit.passed else "blocked"
                if audit.passed:
                    effective = (
                        audit.effective_top10_concentration_bps
                        if audit.effective_top10_concentration_bps is not None
                        else audit.top10_concentration_bps
                    )
                    summary = "No blocking findings"
                    if effective != audit.top10_concentration_bps:
                        summary += (
                            f"; exact vault evidence adjusted top concentration from "
                            f"{audit.top10_concentration_bps / 100:.2f}% to {effective / 100:.2f}%"
                        )
                else:
                    summary = "; ".join(blocking_messages(audit.findings))
                top10 = (
                    audit.effective_top10_concentration_bps
                    if audit.effective_top10_concentration_bps is not None
                    else audit.top10_concentration_bps
                )
                decimals = audit.decimals
                safe_count += int(audit.passed)
                blocked_count += int(not audit.passed)
            except Exception as exc:  # Keep one malformed mint from aborting the bounded scan.
                if mint in watchlist_reaudits or cohort_prior_status.get(mint) == "safe":
                    warnings.append(
                        "A safe-candidate re-audit failed; its previous audit remains stale"
                    )
                    continue
                audit_status = "error"
                summary = f"Audit unavailable ({type(exc).__name__})"
                top10 = None
                decimals = None
            hot_signal = evaluate_hot_market(market_payload).as_dict() if market_payload else None
            journal.upsert_discovery_candidate(
                mint=mint,
                source_program=source_by_mint[mint],
                audit_status=audit_status,
                audit_summary=summary,
                top10_concentration_bps=top10,
                decimals=decimals,
                market=market_payload,
                hot_signal=hot_signal,
            )

    return {
        "scanned": len(selected),
        "batched_mint_accounts": batched_mint_accounts,
        "new_discoveries_scanned": len(selected) - len(watchlist_reaudits) - len(cohort_reaudits),
        "cohort_reaudits": len(cohort_reaudits),
        "watchlist_reaudits": len(watchlist_reaudits),
        "priority_watchlist_reaudits": priority_watchlist_reaudits,
        "safe": safe_count,
        "blocked": blocked_count,
        "warning": "; ".join(warnings) if warnings else None,
        "providers": {
            "pumpfun": {
                "api": pump_api_status,
                "onchain": "connected",
            },
            "dexscreener": {
                **dex_feed_status,
                "market": market_status,
            },
        },
        "candidates": journal.discovery_candidates(limit=100),
    }


class PaperTradingService:
    def __init__(
        self,
        *,
        settings: Settings,
        trading: TradingService,
        jupiter: JupiterClient,
        journal: TradeJournal,
        taker_pubkey: str,
    ) -> None:
        try:
            Pubkey.from_string(taker_pubkey)
        except ValueError as exc:
            raise PaperTradingError("Paper quote taker must be a valid public key") from exc
        self.settings = settings
        self.trading = trading
        self.jupiter = jupiter
        self.journal = journal
        self.taker_pubkey = taker_pubkey
        self.execution = PaperExecutionModel(settings)

    def _liquidity_usd(self, mint: str) -> Decimal | None:
        candidate = self.journal.discovery_candidate(mint)
        snapshots = self.journal.market_snapshots(mint, limit=1)
        if not candidate or not snapshots:
            return None
        snapshot = snapshots[-1]
        captured_at = snapshot.get("captured_at")
        age_seconds = (
            datetime.now(UTC).timestamp() - captured_at if type(captured_at) is int else None
        )
        if (
            not isinstance(candidate.get("pair_address"), str)
            or snapshot.get("pool") != candidate["pair_address"]
            or age_seconds is None
            or not -30 <= age_seconds <= 180
        ):
            return None
        try:
            value = Decimal(str(snapshot["liquidity_usd"]))
        except (InvalidOperation, ValueError):
            return None
        return value if value.is_finite() and value > 0 else None

    def _exit_quote(self, position: dict[str, Any], amount: int, slippage_bps: int) -> RouteQuote:
        entry_execution = position.get("entry_execution")
        if isinstance(entry_execution, dict) and entry_execution.get("quote_source") == "v2-build":
            return self.jupiter.router_quote(
                str(position["mint"]),
                WSOL_MINT,
                amount,
                taker=self.taker_pubkey,
                slippage_bps=slippage_bps,
            )
        # Preserve the original quote model for positions opened before route
        # parity was recorded; mixing quote paths would distort their P&L.
        return self.jupiter.quote(str(position["mint"]), WSOL_MINT, amount, slippage_bps)

    def _sell_fill_with_stale_retry(
        self, position: dict[str, Any], amount: int, slippage_bps: int
    ) -> tuple[RouteQuote, PaperFillEstimate, int]:
        """Price a paper exit from a fresh route, retrying one stale build."""
        attempts = 0
        while True:
            attempts += 1
            quoted_at = datetime.now(UTC)
            quote = self._exit_quote(position, amount, slippage_bps)
            try:
                fill = self.execution.estimate(
                    quote=quote,
                    side="sell",
                    slippage_bps=slippage_bps,
                    quoted_at=quoted_at,
                    liquidity_usd=self._liquidity_usd(str(position["mint"])),
                )
            except PaperExecutionError as exc:
                if attempts == 1 and paper_execution_failure_reason(exc) == "stale_route":
                    continue
                raise
            return quote, fill, attempts

    def _require_active_campaign(self) -> None:
        campaign = self.journal.runtime_state("paper_campaign") or {}
        if campaign.get("mode") == "forward_paper_only" and campaign.get("status") == "interrupted":
            raise PaperTradingError("Paper campaign was interrupted; start a clean run")

    def open(
        self,
        *,
        mint: str,
        lamports: int,
        slippage_bps: int,
        decision: StrategyDecision | None = None,
        entry_source: Literal["autonomous", "call_inbox", "manual"] = "manual",
    ) -> dict[str, Any]:
        self._require_active_campaign()
        if lamports > self.settings.max_trade_lamports:
            raise PaperTradingError("Paper entry exceeds MAX_TRADE_LAMPORTS")
        if entry_source not in ("autonomous", "call_inbox", "manual"):
            raise PaperTradingError("Paper entry source is invalid")
        if entry_source != "manual" and (decision is None or decision.verdict != "eligible"):
            raise PaperTradingError("Strategy paper entry requires an eligible recorded decision")
        if entry_source != "manual" and not has_signal_evidence(
            decision.evidence if decision else None
        ):
            raise PaperTradingError("Strategy paper entry requires complete decision evidence")
        if (
            entry_source != "manual"
            and decision is not None
            and not _decision_is_recent(decision.evidence)
        ):
            raise StalePaperDecisionError("Paper entry decision is too old")
        if entry_source != "manual" and decision is not None:
            market_inputs = decision.evidence.get("market_gate_inputs")
            if not isinstance(market_inputs, dict) or market_inputs.get("mint") != mint:
                raise PaperTradingError("Strategy paper entry market evidence token mismatch")
        if decision is not None and decision.mint != mint:
            raise PaperTradingError("Paper decision token does not match the entry")
        quote_started_at = datetime.now(UTC)
        report = self.trading.buy(
            mint=mint,
            lamports=lamports,
            slippage_bps=slippage_bps,
            capture_output_ata_rent=True,
        )
        campaign = self.journal.runtime_state("paper_campaign") or {}
        if (
            entry_source != "manual"
            and campaign.get("mode") == "forward_paper_only"
            and report.quote.api_version != "v2-build"
        ):
            raise PaperTradingError("Forward paper entry requires a fee-free built router quote")
        if (
            entry_source != "manual"
            and campaign.get("mode") == "forward_paper_only"
            and report.result.output_ata_rent_lamports is None
        ):
            raise PaperTradingError("Forward paper entry requires a measured token-account reserve")
        measured_ata_rent = report.result.output_ata_rent_lamports
        if measured_ata_rent is not None and self.journal.paper_mint_has_position(mint):
            measured_ata_rent = 0
        sol_usd_price: Decimal | None = None
        sol_price_provenance: dict[str, object] = {}
        if report.quote.input_mint == WSOL_MINT and quote_swap_usd(report.quote) is None:
            try:
                sol_usd_price, block_id = self.jupiter.sol_usd_reference()
                current_slot = self.trading.rpc.call("getSlot", [{"commitment": "processed"}])
            except (JupiterError, RpcError) as exc:
                raise PaperExecutionError("SOL USD reference is unavailable") from exc
            if (
                type(current_slot) is not int
                or not 0 <= current_slot - block_id <= MAX_SOL_PRICE_SLOT_LAG
            ):
                raise PaperExecutionError("SOL USD reference is stale")
            sol_price_provenance = {
                "sol_usd_price_source": "jupiter_price_v3",
                "sol_usd_price": str(sol_usd_price),
                "sol_price_block_id": block_id,
                "sol_price_checked_slot": current_slot,
            }
        fill = self.execution.estimate(
            quote=report.quote,
            side="buy",
            slippage_bps=slippage_bps,
            quoted_at=report.quote_requested_at or quote_started_at,
            liquidity_usd=self._liquidity_usd(mint),
            sol_usd_price=sol_usd_price,
            ata_rent_override_lamports=measured_ata_rent,
        )
        entry_execution = {
            **fill.as_dict(),
            **sol_price_provenance,
            "swap_usd_source": (
                "jupiter_price_v3_sol_input" if sol_price_provenance else "router_quote"
            ),
            "quote_source": report.quote.api_version,
            "quote_fee_bps": report.quote.fee_bps,
        }
        audit = audit_as_dict(report.audit) if report.audit else None
        if entry_source == "manual":
            from .strategy import StrategyDecision

            snapshots = self.journal.market_snapshots(mint, limit=1)
            decision = StrategyDecision(
                mint=mint,
                score=0,
                verdict="observe",
                reasons=(
                    "User requested a manual paper entry; strategy signals were not evaluated",
                ),
                evidence={
                    "strategy": "manual_paper_entry",
                    "signal_status": "not_evaluated_manual",
                    "requested_lamports": lamports,
                    "slippage_bps": slippage_bps,
                    "market_snapshot": snapshots[-1] if snapshots else None,
                    "audit": audit,
                    "execution": entry_execution,
                },
            )
        if decision is None:
            raise PaperTradingError("Paper entry decision is missing")
        decision_action = {
            "autonomous": "paper_opened",
            "call_inbox": "call_paper_opened",
            "manual": "manual_paper_opened",
        }[entry_source]

        def verify_entry_risk() -> None:
            from .strategy import (
                confirm_candidate_candles,
                evaluate_candidate,
                paper_entry_breakers,
            )

            reasons = paper_entry_breakers(self.settings, self.journal)
            if reasons:
                raise PaperTradingError(f"Paper entry paused: {reasons[0]}")
            if entry_source != "manual":
                if decision is None or not _decision_is_recent(decision.evidence):
                    raise StalePaperDecisionError("Paper entry decision expired during preview")
                current_candidate = next(
                    (
                        candidate
                        for candidate in self.journal.discovery_candidates(limit=100)
                        if candidate.get("mint") == mint
                    ),
                    None,
                )
                if current_candidate is None:
                    raise StalePaperDecisionError("Paper entry candidate is no longer available")
                refreshed = confirm_candidate_candles(
                    current_candidate,
                    evaluate_candidate(current_candidate, self.settings),
                    self.settings,
                    self.journal,
                )
                if refreshed.verdict != "eligible":
                    raise StalePaperDecisionError("Paper entry signal is no longer eligible")
                entry_execution["entry_revalidation"] = {
                    "score": refreshed.score,
                    "verdict": refreshed.verdict,
                    "reasons": list(refreshed.reasons),
                    "market_policy_version": refreshed.evidence.get("market_policy_version"),
                    "market_evaluated_at_ms": refreshed.evidence.get("market_evaluated_at_ms"),
                    "market_gate_verdict": refreshed.evidence.get("market_gate_verdict"),
                    "market_gate_score": refreshed.evidence.get("market_gate_score"),
                    "market_policy": refreshed.evidence.get("market_policy"),
                    "market_gate_inputs": refreshed.evidence.get("market_gate_inputs"),
                    "decision_as_of": refreshed.evidence.get("decision_as_of"),
                    "audit_last_seen": refreshed.evidence.get("audit_last_seen"),
                    "market_captured_at": refreshed.evidence.get("market_captured_at"),
                    "candle_policy": refreshed.evidence.get("candle_policy"),
                    "latest_closed_candle_at": refreshed.evidence.get("latest_closed_candle_at"),
                    "candle_signals": refreshed.evidence.get("candle_signals"),
                }
            if entry_source != "manual" and campaign.get("mode") == "forward_paper_only":
                cash = self.journal.paper_cash_lamports(self.settings.paper_starting_lamports)
                if fill.cash_debit_lamports > (
                    cash * self.settings.paper_risk_per_trade_bps // 10_000
                ):
                    raise PaperTradingError("Paper entry exceeds the measured risk budget")

        position_id = f"paper-{uuid.uuid4()}"
        position = self.journal.open_paper_position(
            position_id=position_id,
            mint=mint,
            entry_lamports=fill.cash_debit_lamports,
            token_amount=fill.executed_out_amount,
            simulated_units=report.result.simulated_units,
            starting_lamports=self.settings.paper_starting_lamports,
            execution=entry_execution,
            decision=decision,
            decision_action=decision_action,
            max_open_positions=self.settings.strategy_max_open_positions,
            entry_guard=verify_entry_risk,
        )
        return {
            "position": position,
            "audit": audit,
            "execution": entry_execution,
        }

    def close(
        self, *, position_id: str, slippage_bps: int, exit_reason: str = "manual"
    ) -> dict[str, Any]:
        self._require_active_campaign()
        position = self.journal.paper_position(position_id)
        if position["status"] != "open":
            raise PaperTradingError("Paper position is already closed")
        # The paper tokens do not exist in the wallet, so an exit transaction cannot
        # truthfully pass an on-chain balance simulation. Close at a fresh executable
        # route quote instead; live sells still use the full build/simulate pipeline.
        quote, fill, attempts = self._sell_fill_with_stale_retry(
            position,
            _stored_int(position["remaining_token_amount"], "remaining token amount"),
            slippage_bps,
        )
        exit_execution = {
            **fill.as_dict(),
            "quote_source": quote.api_version,
            "quote_fee_bps": quote.fee_bps,
            "route_quote_attempts": attempts,
        }
        closed = self.journal.close_paper_position(
            position_id,
            fill.cash_credit_lamports,
            exit_reason=exit_reason,
            execution=exit_execution,
        )
        return {
            "position": closed,
            "exit_quote_lamports": quote.out_amount,
            "net_exit_lamports": fill.cash_credit_lamports,
            "execution": exit_execution,
        }

    def take_partial(
        self,
        *,
        position_id: str,
        sell_bps_of_original: int,
        slippage_bps: int,
        exit_reason: str = "manual_partial",
    ) -> dict[str, Any]:
        self._require_active_campaign()
        if not 1 <= sell_bps_of_original <= 10_000:
            raise PaperTradingError("Paper partial sale must be between 1 and 10000 bps")
        position = self.journal.paper_position(position_id)
        if position["status"] != "open":
            raise PaperTradingError("Paper position is already closed")
        original_tokens = _stored_int(position["token_amount"], "original token amount")
        remaining_tokens = _stored_int(position["remaining_token_amount"], "remaining token amount")
        target_tokens = max(1, original_tokens * sell_bps_of_original // 10_000)
        sold_tokens = min(target_tokens, remaining_tokens)
        quote, fill, attempts = self._sell_fill_with_stale_retry(
            position, sold_tokens, slippage_bps
        )
        exit_execution = {
            **fill.as_dict(),
            "quote_source": quote.api_version,
            "quote_fee_bps": quote.fee_bps,
            "route_quote_attempts": attempts,
        }
        updated = self.journal.partial_close_paper_position(
            position_id,
            sold_token_amount=sold_tokens,
            exit_lamports=fill.cash_credit_lamports,
            advance_tp_stage=True,
            exit_reason=exit_reason,
            execution=exit_execution,
        )
        return {
            "position": updated,
            "sold_token_amount": sold_tokens,
            "exit_quote_lamports": quote.out_amount,
            "net_exit_lamports": fill.cash_credit_lamports,
            "execution": exit_execution,
        }

    def portfolio(self, *, refresh: bool) -> dict[str, Any]:
        if refresh:
            self._require_active_campaign()
        positions = self.journal.paper_positions()
        now = datetime.now(UTC)
        candidate_by_mint = {
            str(item["mint"]): item for item in self.journal.discovery_candidates(limit=100)
        }
        quote_health = self.journal.runtime_state("paper_quote_health") or {}
        unavailable_raw = quote_health.get("unavailable")
        unavailable = unavailable_raw if isinstance(unavailable_raw, dict) else {}
        open_value = 0
        unvalued_position_count = 0
        stale_mark_value = 0
        stale_mark_count = 0
        realized = 0
        output: list[dict[str, Any]] = []
        for position in positions:
            item = dict(position)
            status = str(position["status"])
            entry = _stored_int(position["entry_lamports"], "entry")
            original_tokens = _stored_int(position["token_amount"], "original token amount")
            remaining_tokens = _stored_int(
                position["remaining_token_amount"],
                "remaining token amount",
            )
            realized_proceeds = _stored_int(position["realized_lamports"], "realized proceeds")
            realized_cost = _stored_int(position["realized_cost_lamports"], "realized cost")
            quote_state = (
                "closed"
                if status == "closed"
                else "recent_mark"
                if not refresh and _recent_paper_mark(position, now)
                else "stale"
            )
            remaining_value: int | None
            if status == "closed":
                remaining_value = 0
            elif position["last_value_lamports"] is None:
                remaining_value = None
            else:
                remaining_value = _stored_int(position["last_value_lamports"], "mark")
            if status == "open" and refresh:
                quote = None
                try:
                    mark_slippage_bps = min(100, self.settings.max_slippage_bps)
                    quote_attempts = 0
                    while True:
                        quote_attempts += 1
                        quote_started_at = datetime.now(UTC)
                        record_mark_quote_attempt()
                        quote = self._exit_quote(position, remaining_tokens, mark_slippage_bps)
                        record_mark_quote_success()
                        try:
                            mark = self.execution.estimate(
                                quote=quote,
                                side="mark",
                                slippage_bps=mark_slippage_bps,
                                quoted_at=quote_started_at,
                                liquidity_usd=self._liquidity_usd(str(position["mint"])),
                            )
                        except PaperExecutionError as exc:
                            if (
                                quote_attempts == 1
                                and paper_execution_failure_reason(exc) == "stale_route"
                            ):
                                continue
                            raise
                        break
                    mark_execution = {
                        **mark.as_dict(),
                        "quote_source": quote.api_version,
                        "quote_fee_bps": quote.fee_bps,
                        "route_quote_attempts": quote_attempts,
                    }
                    remaining_value = mark.cash_credit_lamports
                    fresh_performance_value = remaining_value * original_tokens // remaining_tokens
                    self.journal.mark_paper_position(
                        str(position["position_id"]),
                        remaining_value,
                        peak_reference_lamports=fresh_performance_value,
                        execution=mark_execution,
                    )
                    item["last_value_lamports"] = remaining_value
                    stored_peak = position.get("peak_value_lamports")
                    item["peak_value_lamports"] = max(
                        fresh_performance_value,
                        stored_peak if isinstance(stored_peak, int) else 0,
                    )
                    quote_state = "live"
                    item["last_mark_execution"] = mark_execution
                except (JupiterError, PaperExecutionError) as exc:
                    quote_state = "stale"
                    item["quote_error"] = type(exc).__name__
                    if isinstance(exc, PaperExecutionError):
                        item["quote_error_reason"] = paper_execution_failure_reason(exc)
                    if isinstance(exc, UneconomicPaperExitError) and quote is not None:
                        item["quote_gross_out_lamports"] = quote.out_amount
            performance_value = (
                remaining_value * original_tokens // remaining_tokens
                if remaining_value is not None and remaining_tokens > 0
                else None
            )
            total_value: int | None = realized_proceeds if status == "closed" else None
            if remaining_value is not None:
                total_value = realized_proceeds + remaining_value
            pnl = total_value - entry if total_value is not None else None
            pnl_bps = pnl * 10_000 // entry if pnl is not None else None
            price_pnl_bps = (
                (performance_value - entry) * 10_000 // entry
                if performance_value is not None
                else None
            )
            item.update(
                {
                    "current_value_lamports": total_value,
                    "remaining_value_lamports": remaining_value,
                    "performance_value_lamports": performance_value,
                    "pnl_lamports": pnl,
                    "pnl_bps": pnl_bps,
                    "price_pnl_bps": price_pnl_bps,
                    "remaining_bps": remaining_tokens * 10_000 // original_tokens,
                    "quote_state": quote_state,
                    "market": candidate_by_mint.get(str(position["mint"])),
                }
            )
            if status == "open" and not refresh:
                previous_failure = unavailable.get(position.get("position_id"))
                if isinstance(previous_failure, dict) and previous_failure.get(
                    "last_quoted_at"
                ) == position.get("last_quoted_at"):
                    quote_state = "stale"
                    item["quote_state"] = "stale"
                    error_type = previous_failure.get("error_type")
                    if isinstance(error_type, str):
                        item["quote_error"] = error_type
                    error_reason = previous_failure.get("error_reason")
                    if isinstance(error_reason, str):
                        item["quote_error_reason"] = error_reason
                    if error_reason == "below_exit_fees":
                        gross_out = previous_failure.get("quote_gross_out_lamports")
                        if type(gross_out) is int and gross_out >= 0:
                            item["quote_gross_out_lamports"] = gross_out
            if status == "open":
                if remaining_value is None:
                    unvalued_position_count += 1
                open_value += remaining_value or 0
                if quote_state == "stale":
                    stale_mark_value += remaining_value or 0
                    stale_mark_count += 1
            realized += realized_proceeds - realized_cost
            output.append(item)

        cash = self.journal.paper_cash_lamports(self.settings.paper_starting_lamports)
        equity = cash + open_value
        fills = self.journal.paper_fills(limit=1_000)
        execution_summary = {
            "fill_count": len(fills),
            "network_fee_lamports": 0,
            "priority_fee_lamports": 0,
            "ata_rent_lamports": 0,
            "modeled_slippage_lamports": 0,
            "explicit_cost_lamports": 0,
        }
        for fill in fills:
            execution = fill.get("execution")
            if not isinstance(execution, dict):
                continue
            for key in (
                "network_fee_lamports",
                "priority_fee_lamports",
                "ata_rent_lamports",
                "modeled_slippage_lamports",
                "explicit_cost_lamports",
            ):
                value = execution.get(key)
                if isinstance(value, int) and value >= 0:
                    execution_summary[key] += value
        return {
            "paper_campaign": self.journal.runtime_state("paper_campaign"),
            "starting_lamports": self.settings.paper_starting_lamports,
            "cash_lamports": cash,
            "open_value_lamports": open_value,
            "unvalued_position_count": unvalued_position_count,
            "all_positions_have_marks": unvalued_position_count == 0,
            "stale_mark_value_lamports": stale_mark_value,
            "stale_mark_count": stale_mark_count,
            "equity_lamports": equity,
            "equity_if_stale_zero_lamports": equity - stale_mark_value,
            "total_pnl_lamports": (
                equity - self.settings.paper_starting_lamports
                if unvalued_position_count == 0
                else None
            ),
            "realized_pnl_lamports": realized,
            "forward_performance": summarize_forward_paper(
                self.journal,
                self.settings.paper_starting_lamports,
                paper_poll_seconds=self.settings.paper_exit_interval_seconds,
            ),
            "execution_summary": execution_summary,
            "execution_policy": {
                "base_slippage_bps": self.settings.paper_execution_base_slippage_bps,
                "liquidity_shock_bps": self.settings.paper_execution_liquidity_shock_bps,
                "network_fee_lamports": self.settings.paper_execution_network_fee_lamports,
                "priority_fee_lamports": self.settings.paper_execution_priority_fee_lamports,
                "ata_rent_lamports": self.settings.paper_execution_ata_rent_lamports,
                "max_quote_age_ms": self.settings.paper_execution_max_quote_age_ms,
                "max_trade_liquidity_bps": (self.settings.paper_execution_max_trade_liquidity_bps),
            },
            "positions": output,
        }
