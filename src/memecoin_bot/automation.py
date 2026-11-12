from __future__ import annotations

import logging
import sqlite3
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from typing import Any

from .config import Settings
from .exit_policy import ExitPolicy, evaluate_exit_rule
from .journal import TradeJournal
from .operations import paper_exit_monitor_issues
from .paper import PaperTradingService, scan_candidates
from .rpc import RpcClient
from .service import TradingService
from .strategy import (
    evaluate_candidate,
    open_position_liquidity_status,
    paper_entry_breakers,
    run_paper_decision_cycle,
)
from .telemetry import record_exit_marks

LOGGER = logging.getLogger(__name__)


class PaperAutomationError(RuntimeError):
    """Raised when the autonomous paper worker cannot accept an operation."""


@dataclass(frozen=True, slots=True)
class ExitSignal:
    position_id: str
    reason: str
    action: str
    pnl_bps: int
    current_value_lamports: int
    peak_value_lamports: int
    sell_bps: int | None = None
    tp_stage: int | None = None

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


def _positive_int(value: object, label: str) -> int:
    if not isinstance(value, int) or value <= 0:
        raise PaperAutomationError(f"Paper position {label} is invalid")
    return value


def _optional_text(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def record_paper_quote_health(
    journal: TradeJournal, positions: list[dict[str, object]]
) -> list[dict[str, object]]:
    """Surface unquotable exits once per outage, with a later recovery event."""
    state = journal.runtime_state("paper_quote_health") or {}
    prior_raw = state.get("unavailable")
    prior = prior_raw if isinstance(prior_raw, dict) else {}
    unavailable: dict[str, dict[str, object]] = {}
    failures: list[dict[str, object]] = []
    for position in positions:
        if position.get("status") != "open":
            continue
        position_id = position.get("position_id")
        if not isinstance(position_id, str) or not position_id:
            continue
        previous = prior.get(position_id)
        old = previous if isinstance(previous, dict) else {}
        if position.get("quote_state") == "live":
            if old:
                journal.record_automation_event(
                    event_type="paper_quote_recovered",
                    message="Paper exit quote recovered; exit rules are active again",
                    metadata={"position_id": position_id, "mint": position.get("mint")},
                )
            continue
        previous_count = old.get("failure_cycles")
        count = (
            previous_count + 1 if type(previous_count) is int and 0 < previous_count < 1_000 else 1
        )
        error_type = _optional_text(position.get("quote_error")) or "QuoteUnavailable"
        error_reason = _optional_text(position.get("quote_error_reason"))
        detail: dict[str, object] = {
            "position_id": position_id,
            "mint": position.get("mint"),
            "error_type": error_type,
            "error_reason": error_reason,
            "quote_gross_out_lamports": position.get("quote_gross_out_lamports"),
            "failure_cycles": count,
            "last_quoted_at": position.get("last_quoted_at"),
        }
        unavailable[position_id] = detail
        failures.append(detail)
        if count in (1, 3) or old.get("error_reason") != error_reason:
            journal.record_automation_event(
                event_type=(
                    "paper_exit_uneconomic"
                    if error_reason == "below_exit_fees"
                    else "paper_quote_unavailable"
                ),
                level="warning",
                message=(
                    "Paper exit route is below modeled fees; exit rules cannot execute"
                    if error_reason == "below_exit_fees"
                    else "Paper exit quote unavailable; stop and profit rules cannot be evaluated"
                ),
                metadata=detail,
            )
    journal.set_runtime_state("paper_quote_health", {"unavailable": unavailable})
    return failures


def evaluate_exit_signal(
    position: dict[str, object],
    settings: Settings,
    *,
    now: datetime | None = None,
) -> ExitSignal | None:
    """Evaluate deterministic exit rules against a freshly marked paper position."""
    if position.get("status") != "open" or position.get("quote_state") != "live":
        return None
    position_id = str(position.get("position_id") or "")
    if not position_id:
        raise PaperAutomationError("Paper position ID is invalid")
    entry = _positive_int(position.get("entry_lamports"), "entry")
    current = _positive_int(position.get("performance_value_lamports"), "performance value")
    peak_raw = position.get("peak_value_lamports")
    peak = max(entry, current, peak_raw if isinstance(peak_raw, int) else 0)
    pnl_bps = (current - entry) * 10_000 // entry
    tp_stage_raw = position.get("tp_stage", 0)
    opened_at = position.get("opened_at")
    age_seconds: int | None = None
    if isinstance(opened_at, str):
        try:
            opened = datetime.fromisoformat(opened_at)
        except ValueError:
            pass
        else:
            if opened.tzinfo is None:
                opened = opened.replace(tzinfo=UTC)
            age_seconds = max(0, int(((now or datetime.now(UTC)) - opened).total_seconds()))
    policy = ExitPolicy(
        settings.paper_tp_levels_bps,
        settings.paper_tp_sell_bps,
        settings.paper_stop_loss_bps,
        settings.paper_trailing_activation_bps,
        settings.paper_trailing_stop_bps,
        settings.paper_max_hold_seconds,
    )
    try:
        rule = evaluate_exit_rule(
            entry_value=entry,
            current_value=current,
            peak_value=peak,
            tp_stage=tp_stage_raw if type(tp_stage_raw) is int else -1,
            age_seconds=age_seconds,
            policy=policy,
        )
    except ValueError as exc:
        if str(exc) == "Position age is invalid":
            raise PaperAutomationError("Paper position open time is invalid") from exc
        raise PaperAutomationError(str(exc)) from exc
    if rule is None:
        return None
    return ExitSignal(
        position_id=position_id,
        reason=rule.reason,
        action=rule.action,
        pnl_bps=pnl_bps,
        current_value_lamports=current,
        peak_value_lamports=peak,
        sell_bps=rule.sell_bps,
        tp_stage=rule.tp_stage,
    )


def run_paper_exit_cycle(
    *, settings: Settings, paper: PaperTradingService, journal: TradeJournal
) -> dict[str, Any]:
    """Refresh executable quotes and apply paper exits independently of discovery."""
    portfolio = paper.portfolio(refresh=True)
    record_exit_marks(portfolio["positions"])
    quote_failures = record_paper_quote_health(journal, portfolio["positions"])
    exit_results: list[dict[str, object]] = []
    current_positions = {
        str(position.get("position_id")): position for position in portfolio["positions"]
    }
    exit_failures: list[dict[str, object]] = []
    evaluation_failed = False
    execution_failed = False
    for initial_position in portfolio["positions"]:
        position = current_positions.get(str(initial_position.get("position_id")))
        if position is None:
            continue
        try:
            signal = evaluate_exit_signal(position, settings)
        except Exception as exc:
            evaluation_failed = True
            failure: dict[str, object] = {
                "position_id": str(position.get("position_id") or ""),
                "error_type": type(exc).__name__,
            }
            exit_failures.append(failure)
            journal.record_automation_event(
                event_type="paper_exit_evaluation_failed",
                level="error",
                message=f"Paper exit evaluation failed ({type(exc).__name__})",
                metadata=failure,
            )
            continue
        while signal is not None:
            active_signal = signal
            try:
                if active_signal.action == "partial":
                    if active_signal.sell_bps is None:
                        raise PaperAutomationError(
                            "Partial exit signal is missing its sell percentage"
                        )
                    result = paper.take_partial(
                        position_id=active_signal.position_id,
                        sell_bps_of_original=active_signal.sell_bps,
                        slippage_bps=100,
                        exit_reason=active_signal.reason,
                    )
                    event_type = "paper_partial_take_profit"
                    status = "partial"
                    message = (
                        f"TP{active_signal.tp_stage} filled: sold "
                        f"{active_signal.sell_bps / 100:.0f}% "
                        "of the original paper position"
                    )
                else:
                    result = paper.close(
                        position_id=active_signal.position_id,
                        slippage_bps=100,
                        exit_reason=active_signal.reason,
                    )
                    event_type = "paper_exit"
                    status = "closed"
                    message = f"Closed paper position: {active_signal.reason.replace('_', ' ')}"
                exit_result = {**active_signal.as_dict(), "status": status}
                exit_results.append(exit_result)
                journal.record_automation_event(
                    event_type=event_type,
                    message=message,
                    metadata={
                        **exit_result,
                        "exit_quote_lamports": result["exit_quote_lamports"],
                    },
                )
                if active_signal.action != "partial":
                    break
                # A partial sale changes the remaining size and can change route
                # impact. Do not cascade TP stages from the pre-sale full-bag mark.
                refreshed = paper.portfolio(refresh=True)
                record_exit_marks(refreshed["positions"])
                quote_failures = record_paper_quote_health(journal, refreshed["positions"])
                current_positions = {
                    str(current.get("position_id")): current for current in refreshed["positions"]
                }
                position = current_positions.get(active_signal.position_id)
                signal = evaluate_exit_signal(position, settings) if position is not None else None
            except Exception as exc:
                execution_failed = True
                exit_failures.append(
                    {"position_id": active_signal.position_id, "error_type": type(exc).__name__}
                )
                journal.record_automation_event(
                    event_type="paper_exit_failed",
                    level="error",
                    message=f"Paper exit failed ({type(exc).__name__})",
                    metadata=active_signal.as_dict(),
                )
                break

    return {
        "paused": False,
        "portfolio": paper.portfolio(refresh=False),
        "exits": exit_results,
        "exit_failures": exit_failures,
        "quote_failures": quote_failures,
        "evaluation_failed": evaluation_failed,
        "execution_failed": execution_failed,
    }


def _exit_monitor_breakers(settings: Settings, journal: TradeJournal) -> list[str]:
    """Fail closed on entries when the independent exit lane is not healthy."""
    return paper_exit_monitor_issues(
        journal.runtime_state("paper_exit_monitor") or {}, settings.paper_exit_interval_seconds
    )


def run_autonomous_paper_cycle(
    *,
    settings: Settings,
    paper: PaperTradingService,
    rpc: RpcClient,
    trading: TradingService,
    journal: TradeJournal,
    manage_exits: bool = True,
    exit_monitor_status: Callable[[], dict[str, object]] | None = None,
) -> dict[str, Any]:
    """Discover and consider one entry, with exit management when requested."""
    exit_cycle: dict[str, Any] = (
        run_paper_exit_cycle(settings=settings, paper=paper, journal=journal)
        if manage_exits
        else {
            "portfolio": paper.portfolio(refresh=False),
            "exits": [],
            "exit_failures": [],
            "quote_failures": [],
        }
    )
    portfolio: dict[str, Any] = exit_cycle["portfolio"]
    exit_results = exit_cycle["exits"]
    exit_failures = exit_cycle["exit_failures"]
    quote_failures = exit_cycle["quote_failures"]
    scan = scan_candidates(
        settings=settings,
        rpc=rpc,
        trading=trading,
        journal=journal,
        reaudit_priority=lambda candidate: (
            evaluate_candidate(candidate, settings).verdict == "eligible"
        ),
    )
    # The exit worker can update positions while discovery is network-bound.
    # Check current portfolio and monitor health immediately before entries.
    if not manage_exits:
        portfolio = paper.portfolio(refresh=False)
    drawdown_lamports = settings.paper_starting_lamports - int(portfolio["equity_lamports"])
    max_drawdown = settings.paper_starting_lamports * settings.paper_max_drawdown_bps // 10_000
    breaker_reasons: list[str] = []
    if exit_cycle.get("evaluation_failed"):
        breaker_reasons.append("exit_evaluation_failed")
    if exit_cycle.get("execution_failed"):
        breaker_reasons.append("exit_execution_failed")
    if quote_failures:
        breaker_reasons.append("exit_quote_unavailable")
    if not manage_exits:
        breaker_reasons.extend(
            paper_exit_monitor_issues(exit_monitor_status(), settings.paper_exit_interval_seconds)
            if exit_monitor_status is not None
            else _exit_monitor_breakers(settings, journal)
        )
    if drawdown_lamports >= max_drawdown:
        breaker_reasons.append("portfolio_drawdown_limit")
    risk_metrics = journal.paper_risk_metrics()
    daily_loss_limit = settings.paper_starting_lamports * settings.paper_daily_loss_bps // 10_000
    if int(risk_metrics["daily_realized_pnl_lamports"]) <= -daily_loss_limit:
        breaker_reasons.append("daily_loss_limit")
    if int(risk_metrics["consecutive_losses"]) >= settings.paper_max_consecutive_losses:
        breaker_reasons.append("consecutive_loss_limit")
    open_positions = [
        position for position in portfolio["positions"] if position.get("status") == "open"
    ]
    open_mints = {str(position["mint"]) for position in open_positions}
    thin_open_positions, unverified_open_positions = open_position_liquidity_status(
        settings, journal, open_mints
    )
    for reason in paper_entry_breakers(settings, journal):
        if reason not in breaker_reasons:
            breaker_reasons.append(reason)

    if breaker_reasons:
        journal.record_automation_event(
            event_type="circuit_breaker",
            level="warning",
            message="Circuit breaker blocked new paper entries; exit management remains active",
            metadata={
                "drawdown_lamports": drawdown_lamports,
                "limit_lamports": max_drawdown,
                "daily_realized_pnl_lamports": risk_metrics["daily_realized_pnl_lamports"],
                "daily_loss_limit_lamports": daily_loss_limit,
                "consecutive_losses": risk_metrics["consecutive_losses"],
                "thin_open_positions": thin_open_positions,
                "unverified_open_positions": unverified_open_positions,
                "reasons": breaker_reasons,
            },
        )
        decision = run_paper_decision_cycle(
            settings=settings,
            paper=paper,
            journal=journal,
            forced_breaker_reasons=breaker_reasons,
        )
        return {
            "paused": False,
            "entry_paused": True,
            "pause_reason": breaker_reasons[0],
            "breaker_reasons": breaker_reasons,
            "exits": exit_results,
            "exit_failures": exit_failures,
            "quote_failures": quote_failures,
            "portfolio": portfolio,
            "scan": scan,
            "decision": decision,
        }

    decision = run_paper_decision_cycle(settings=settings, paper=paper, journal=journal)
    opened = decision.get("opened")
    decision_breakers = decision.get("breaker_reasons")
    entry_breakers = (
        [reason for reason in decision_breakers if isinstance(reason, str)]
        if isinstance(decision_breakers, list)
        else []
    )
    if entry_breakers:
        journal.record_automation_event(
            event_type="circuit_breaker",
            level="warning",
            message="Circuit breaker blocked new paper entries; exit management remains active",
            metadata={"reasons": entry_breakers, "evaluated": decision["evaluated"]},
        )
    journal.record_automation_event(
        event_type="cycle_completed",
        message=(
            "Autonomous cycle opened a paper position"
            if opened
            else "Autonomous cycle completed without an entry"
        ),
        metadata={
            "scanned": scan["scanned"],
            "safe": scan["safe"],
            "evaluated": decision["evaluated"],
            "exits": len(exit_results),
            "opened": bool(opened),
        },
    )
    return {
        "paused": False,
        "entry_paused": bool(entry_breakers),
        "pause_reason": entry_breakers[0] if entry_breakers else None,
        "breaker_reasons": entry_breakers,
        "exits": exit_results,
        "exit_failures": exit_failures,
        "quote_failures": quote_failures,
        "portfolio": portfolio,
        "scan": scan,
        "decision": decision,
    }


class PaperAutomationWorker:
    """Single-process scheduler for autonomous paper-only decision cycles."""

    def __init__(
        self,
        *,
        interval_seconds: int,
        cycle: Callable[[], dict[str, Any]],
        interval_from_start: bool = False,
        state_loader: Callable[[], dict[str, object] | None] | None = None,
        state_saver: Callable[[dict[str, object]], None] | None = None,
    ) -> None:
        if not 30 <= interval_seconds <= 86_400:
            raise PaperAutomationError("Automation interval must be 30-86400 seconds")
        self.interval_seconds = interval_seconds
        self.interval_from_start = interval_from_start
        self._cycle = cycle
        self._state_saver = state_saver
        self._state_lock = threading.Lock()
        self._cycle_lock = threading.Lock()
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._state = "stopped"
        self._last_started_at: str | None = None
        self._last_completed_at: str | None = None
        self._last_completed_run_id: str | None = None
        self._next_cycle_at: str | None = None
        self._last_error: str | None = None
        self._last_result: dict[str, object] | None = None
        self._cycles_completed = 0
        self._run_id = str(uuid.uuid4())
        restored = state_loader() if state_loader else None
        if restored:
            state = str(restored.get("state") or "stopped")
            self._state = "paused" if state == "paused" else "stopped"
            self._last_started_at = _optional_text(restored.get("last_started_at"))
            self._last_completed_at = _optional_text(restored.get("last_completed_at"))
            self._last_error = _optional_text(restored.get("last_error"))
            last_result = restored.get("last_result")
            self._last_result = last_result if isinstance(last_result, dict) else None
            cycles = restored.get("cycles_completed")
            self._cycles_completed = cycles if isinstance(cycles, int) and cycles >= 0 else 0

    @staticmethod
    def _now_iso() -> str:
        return datetime.now(UTC).isoformat()

    def status(self) -> dict[str, object]:
        with self._state_lock:
            return {
                "state": self._state,
                "running": bool(self._thread and self._thread.is_alive()),
                "interval_seconds": self.interval_seconds,
                "interval_from_start": self.interval_from_start,
                "last_started_at": self._last_started_at,
                "last_completed_at": self._last_completed_at,
                "last_completed_run_id": self._last_completed_run_id,
                "next_cycle_at": self._next_cycle_at,
                "last_error": self._last_error,
                "last_result": self._last_result,
                "cycles_completed": self._cycles_completed,
                "run_id": self._run_id,
                "durable": self._state_saver is not None,
            }

    def _persist(self) -> None:
        if self._state_saver is None:
            return
        for attempt in range(3):
            try:
                self._state_saver(self.status())
                return
            except sqlite3.OperationalError as exc:
                if attempt < 2:
                    time.sleep(0.05 * (attempt + 1))
                    continue
                LOGGER.warning("Automation state write failed (%s)", type(exc).__name__)
            except Exception as exc:
                # Persistence must not crash exit management. Log the error type
                # without exposing state payloads or exception text.
                LOGGER.warning("Automation state write failed (%s)", type(exc).__name__)
                return

    def start(self) -> dict[str, object]:
        with self._state_lock:
            if self._thread and self._thread.is_alive():
                return self.status_unlocked()
            self._stop_event.clear()
            self._run_id = str(uuid.uuid4())
            self._state = "running"
            self._last_error = None
            self._thread = threading.Thread(
                target=self._loop,
                name="paper-automation",
                daemon=True,
            )
            self._thread.start()
            result = self.status_unlocked()
        # run_once persists its start before the cycle. A separate startup
        # write after launching the thread can race and overwrite that newer
        # start with a pre-cycle snapshot.
        return result

    def status_unlocked(self) -> dict[str, object]:
        return {
            "state": self._state,
            "running": bool(self._thread and self._thread.is_alive()),
            "interval_seconds": self.interval_seconds,
            "interval_from_start": self.interval_from_start,
            "last_started_at": self._last_started_at,
            "last_completed_at": self._last_completed_at,
            "last_completed_run_id": self._last_completed_run_id,
            "next_cycle_at": self._next_cycle_at,
            "last_error": self._last_error,
            "last_result": self._last_result,
            "cycles_completed": self._cycles_completed,
            "run_id": self._run_id,
            "durable": self._state_saver is not None,
        }

    def stop(self, *, state: str = "stopped") -> dict[str, object]:
        self._stop_event.set()
        thread = self._thread
        if thread and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=5)
        with self._state_lock:
            self._state = "stopping" if thread and thread.is_alive() else state
            self._next_cycle_at = None
            result = self.status_unlocked()
        self._persist()
        return result

    def shutdown(self) -> None:
        self.stop()

    def run_once(self) -> dict[str, Any]:
        if not self._cycle_lock.acquire(blocking=False):
            raise PaperAutomationError("An autonomous paper cycle is already running")
        with self._state_lock:
            self._last_started_at = self._now_iso()
            if self._state not in {"running", "paused"}:
                self._state = "working"
            self._last_error = None
        # A network-bound scan can run for minutes. Persist the start before
        # entering it so durable health does not display an old completed cycle.
        self._persist()
        try:
            result = self._cycle()
            summary = self._summarize(result)
            with self._state_lock:
                self._last_completed_at = self._now_iso()
                self._last_completed_run_id = self._run_id
                self._last_result = summary
                self._cycles_completed += 1
                if result.get("paused"):
                    self._state = "paused"
                    self._next_cycle_at = None
                    self._stop_event.set()
                elif self._thread and self._thread.is_alive():
                    self._state = "running"
                else:
                    self._state = "stopped"
            self._persist()
            return result
        except Exception as exc:
            with self._state_lock:
                self._last_completed_at = self._now_iso()
                self._last_completed_run_id = self._run_id
                self._last_error = f"{type(exc).__name__}: {str(exc)[:300]}"
                self._state = "degraded" if self._thread and self._thread.is_alive() else "stopped"
            self._persist()
            raise
        finally:
            self._cycle_lock.release()

    @staticmethod
    def _summarize(result: dict[str, Any]) -> dict[str, object]:
        raw_scan = result.get("scan")
        raw_decision = result.get("decision")
        scan: dict[str, Any] = raw_scan if isinstance(raw_scan, dict) else {}
        decision: dict[str, Any] = raw_decision if isinstance(raw_decision, dict) else {}
        opened = decision.get("opened") if isinstance(decision, dict) else None
        position = opened.get("position") if isinstance(opened, dict) else None
        summary: dict[str, object] = {
            "paused": bool(result.get("paused")),
            "entry_paused": bool(result.get("entry_paused")),
            "pause_reason": result.get("pause_reason"),
            "scanned": scan.get("scanned", 0),
            "batched_mint_accounts": scan.get("batched_mint_accounts", 0),
            "safe": scan.get("safe", 0),
            "new_discoveries_scanned": scan.get("new_discoveries_scanned", 0),
            "cohort_reaudits": scan.get("cohort_reaudits", 0),
            "watchlist_reaudits": scan.get("watchlist_reaudits", 0),
            "priority_watchlist_reaudits": scan.get("priority_watchlist_reaudits", 0),
            "evaluated": decision.get("evaluated", 0),
            "exits": len(result.get("exits") or []),
            "exit_failures": len(result.get("exit_failures") or []),
            "quote_failures": len(result.get("quote_failures") or []),
            "opened_position_id": (
                position.get("position_id") if isinstance(position, dict) else None
            ),
        }
        request_counts = result.get("rpc_request_counts")
        if isinstance(request_counts, dict):
            summary["rpc_request_counts"] = {
                str(method): count
                for method, count in request_counts.items()
                if isinstance(method, str) and type(count) is int and count >= 0
            }
        narrative = result.get("narrative")
        if isinstance(narrative, dict):
            narrative_summary: dict[str, object] = {
                "updated_at": narrative.get("updated_at"),
                "providers": narrative.get("providers"),
                "collected": narrative.get("collected", 0),
                "signals": narrative.get("signals", 0),
            }
            model_research = narrative.get("openai_research")
            if isinstance(model_research, dict):
                narrative_summary["openai_research"] = {
                    "status": model_research.get("status"),
                    "reason": model_research.get("reason"),
                    "error_code": model_research.get("error_code"),
                    "item_id": model_research.get("item_id"),
                    "model": model_research.get("model"),
                }
            summary["narrative"] = narrative_summary
        if "authentic_candles_refreshed" in scan:
            errors = scan.get("ohlcv_errors")
            raw_codes = scan.get("ohlcv_error_codes")
            safe_codes = (
                {
                    code: count
                    for code, count in raw_codes.items()
                    if code
                    in {
                        "rate_limit",
                        "pool_not_found",
                        "provider_http_error",
                        "pool_identity_mismatch",
                        "provider_unavailable",
                        "invalid_ohlcv",
                    }
                    and isinstance(count, int)
                    and not isinstance(count, bool)
                    and 0 <= count <= 8
                }
                if isinstance(raw_codes, dict)
                else {}
            )
            summary["market_collection"] = {
                "tracked": scan.get("tracked", 0),
                "dex_requests_attempted": scan.get("dex_requests_attempted", 0),
                "dex_requests_succeeded": scan.get("dex_requests_succeeded", 0),
                "research_cohort_size": scan.get("research_cohort_size", 0),
                "research_cohort_tracked": scan.get("research_cohort_tracked", 0),
                "research_focus_size": scan.get("research_focus_size", 0),
                "inactive_5m_mints_deferred": scan.get("inactive_5m_mints_deferred", 0),
                "research_active_5m_size": scan.get("research_active_5m_size", 0),
                "research_active_5m_eligible": scan.get("research_active_5m_eligible", 0),
                "research_active_5m_scheduled": scan.get("research_active_5m_scheduled", 0),
                "research_active_focus_size": scan.get("research_active_focus_size", 0),
                "research_active_focus_scheduled": scan.get("research_active_focus_scheduled", 0),
                "strategy_focus_size": scan.get("strategy_focus_size", 0),
                "strategy_focus_scheduled": scan.get("strategy_focus_scheduled", 0),
                "strategy_focus_revision_excluded": scan.get("strategy_focus_revision_excluded", 0),
                "strategy_focus_low_activity_excluded": scan.get(
                    "strategy_focus_low_activity_excluded", 0
                ),
                "strategy_focus_stale_audit_excluded": scan.get(
                    "strategy_focus_stale_audit_excluded", 0
                ),
                "decision_followup_size": scan.get("decision_followup_size", 0),
                "decision_followup_due": scan.get("decision_followup_due", False),
                "decision_followup_attempted": scan.get("decision_followup_attempted", 0),
                "decision_followup_succeeded": scan.get("decision_followup_succeeded", 0),
                "collection_lane": scan.get("collection_lane"),
                "safe_watchlist_tracked": scan.get("safe_watchlist_tracked", 0),
                "priority_safe_tracked": scan.get("priority_safe_tracked", 0),
                "active_market_ready_size": scan.get("active_market_ready_size", 0),
                "authentic_candles_refreshed": scan.get("authentic_candles_refreshed", 0),
                "ohlcv_requests_attempted": scan.get("ohlcv_requests_attempted", 0),
                "ohlcv_requests_succeeded": scan.get("ohlcv_requests_succeeded", 0),
                "ohlcv_cache_hits": scan.get("ohlcv_cache_hits", 0),
                "ohlcv_request_budget_next": scan.get("ohlcv_request_budget_next", 4),
                "ohlcv_higher_budget_retry_after": scan.get("ohlcv_higher_budget_retry_after"),
                "research_secondary_attempted": scan.get("research_secondary_attempted", 0),
                "research_secondary_succeeded": scan.get("research_secondary_succeeded", 0),
                "ohlcv_empty_responses": scan.get("ohlcv_empty_responses", 0),
                "ohlcv_stale_responses": scan.get("ohlcv_stale_responses", 0),
                "ohlcv_retry_cooldown_size": scan.get("ohlcv_retry_cooldown_size", 0),
                "rate_limited": scan.get("ohlcv_rate_limited", False),
                "provider_error_count": len(errors) if isinstance(errors, list) else 0,
                "provider_error_codes": safe_codes,
            }
        return summary

    def _loop(self) -> None:
        try:
            while not self._stop_event.is_set():
                cycle_started = time.monotonic()
                try:
                    result = self.run_once()
                except Exception:
                    result = {}
                if result.get("paused") or self._stop_event.is_set():
                    break
                elapsed = time.monotonic() - cycle_started
                delay = (
                    max(1.0, self.interval_seconds - elapsed)
                    if self.interval_from_start
                    else float(self.interval_seconds)
                )
                next_epoch = time.time() + delay
                with self._state_lock:
                    self._next_cycle_at = datetime.fromtimestamp(next_epoch, UTC).isoformat()
                self._persist()
                if self._stop_event.wait(delay):
                    break
        finally:
            with self._state_lock:
                if self._state != "paused":
                    self._state = "stopped"
                self._next_cycle_at = None
            self._persist()
