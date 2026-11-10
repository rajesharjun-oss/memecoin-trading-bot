from __future__ import annotations

from contextlib import suppress
from typing import Any

from .audit import blocking_messages
from .config import Settings
from .hot_radar import evaluate_hot_market
from .journal import TradeJournal
from .market_data import DexScreenerClient, MarketDataError
from .paper import PaperTradingService
from .service import TradingService
from .strategy import attempt_paper_entry, confirm_candidate_candles, evaluate_candidate


def process_paper_call(
    *,
    settings: Settings,
    mint: str,
    source_label: str | None,
    paper: PaperTradingService,
    trading: TradingService,
    journal: TradeJournal,
) -> dict[str, Any]:
    """Audit, score, and conditionally paper-trade one user-supplied token call."""
    call_id = journal.create_paper_call(mint=mint, source_label=source_label)
    try:
        market = None
        try:
            with DexScreenerClient() as client:
                market = client.tokens([mint]).get(mint)
        except MarketDataError:
            pass

        audit = trading.audit(mint)
        audit_status = "safe" if audit.passed else "blocked"
        audit_summary = (
            "No blocking findings" if audit.passed else "; ".join(blocking_messages(audit.findings))
        )
        market_payload = market.as_dict() if market else None
        journal.upsert_discovery_candidate(
            mint=mint,
            source_program="manual_call",
            audit_status=audit_status,
            audit_summary=audit_summary,
            top10_concentration_bps=audit.top10_concentration_bps,
            decimals=audit.decimals,
            market=market_payload,
            hot_signal=(evaluate_hot_market(market_payload).as_dict() if market_payload else None),
        )
        candidate = next(
            item for item in journal.discovery_candidates(limit=100) if item["mint"] == mint
        )
        decision = confirm_candidate_candles(
            candidate, evaluate_candidate(candidate, settings), settings, journal
        )
        action, opened = attempt_paper_entry(
            settings=settings,
            paper=paper,
            journal=journal,
            decision=decision,
            entry_source="call_inbox",
        )
        position = opened.get("position") if opened else None
        position_id = str(position["position_id"]) if isinstance(position, dict) else None
        if opened is None:
            journal.record_strategy_decision(decision, action=f"call_{action}")
        if action == "paper_opened":
            status = "opened"
        elif action.startswith("simulation_failed"):
            status = "error"
        elif action == "decision_changed_before_entry":
            status = "observed"
        elif decision.verdict == "reject":
            status = "rejected"
        else:
            status = "observed"
        if action == "decision_changed_before_entry":
            reason = "Paper entry signal changed before the simulated fill; no position was opened"
        elif action.startswith("circuit_breaker:"):
            reason = action.replace("circuit_breaker:", "Paper entry paused: ").replace("_", " ")
        else:
            reason = decision.reasons[0] if decision.reasons else action.replace("_", " ")
        journal.update_paper_call(
            call_id,
            status=status,
            score=decision.score,
            verdict=decision.verdict,
            reason=reason,
            position_id=position_id,
        )
        journal.record_automation_event(
            event_type="call_processed",
            level="warning" if status in {"rejected", "error"} else "info",
            message=(
                "Call opened a paper position" if status == "opened" else f"Call {status}: {reason}"
            ),
            metadata={
                "call_id": call_id,
                "mint": mint,
                "score": decision.score,
                "verdict": decision.verdict,
                "action": action,
                "position_id": position_id,
            },
        )
        return {
            "call_id": call_id,
            "mint": mint,
            "status": status,
            "score": decision.score,
            "verdict": decision.verdict,
            "reason": reason,
            "action": action,
            "position_id": position_id,
        }
    except Exception as exc:
        with suppress(Exception):
            journal.update_paper_call(
                call_id,
                status="error",
                reason=f"Processing failed ({type(exc).__name__})",
            )
        raise
