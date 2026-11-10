from __future__ import annotations

import hashlib
import hmac
import logging
import secrets
import sqlite3
import time
import webbrowser
from collections import Counter, defaultdict, deque
from collections.abc import AsyncIterator, Iterator
from contextlib import ExitStack, asynccontextmanager, contextmanager
from dataclasses import asdict, dataclass
from datetime import datetime
from decimal import Decimal
from itertools import pairwise
from math import ceil
from pathlib import Path
from typing import Annotated, Any, Literal, cast

import uvicorn
from fastapi import Cookie, Depends, FastAPI, Header, HTTPException, Request, Response
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field, StrictBool, field_validator
from solders.pubkey import Pubkey

from .automation import (
    PaperAutomationError,
    PaperAutomationWorker,
    run_autonomous_paper_cycle,
    run_paper_exit_cycle,
)
from .calls import process_paper_call
from .config import ConfigurationError, Settings
from .constants import LAMPORTS_PER_SOL
from .dashboard_auth import RememberedSessionStore
from .discovery import DiscoveryError
from .evaluation import run_walk_forward_evaluation
from .execution import (
    PaperExecutionError,
    estimated_additional_exit_fee_bps,
    estimated_paper_swap_fraction_bps,
    estimated_replay_costs,
    estimated_round_trip_cost_bps,
)
from .journal import JournalError, TradeJournal
from .jupiter import JupiterClient, JupiterError
from .market_data import MarketDataError
from .narrative import NarrativeError, refresh_narrative_intelligence
from .ohlcv import TIMEFRAME_SECONDS
from .openai_narrative import assess_one_recent_item
from .operations import (
    OperationsError,
    StateBackupManager,
    operational_health,
    paper_exit_monitor_issues,
)
from .opportunity import summarize_decision_followup_paths
from .paper import PaperTradingError, PaperTradingService, scan_candidates
from .paper_campaign import quarantine_unmonitored_forward_paper_run_locked
from .pump_fun import PumpFunError
from .rpc import RpcClient, RpcError
from .service import RiskRejectedError, TradingService, audit_as_dict
from .strategy import (
    evaluate_candidate,
    make_replay_market_scorer,
    paper_entry_breakers,
    primary_decision_reason,
    refreshed_paper_entry_opportunities,
    run_paper_decision_cycle,
)
from .strategy_lab import (
    CANDLE_PROVENANCE_VERSION,
    RESEARCH_COHORT_KEY,
    RESEARCH_SELECTION_METHOD,
    StrategyLabError,
    analyze_entry,
    collect_market_snapshots,
    load_verified_candles,
    research_cohort_mints,
    research_focus_size,
    run_backtest,
)
from .transaction import SubmissionUncertainError, TransactionError
from .verification import summarize_current_entry_readiness, summarize_current_research_capture
from .wallet import WalletError, load_keypair
from .worker_lock import exclusive_worker_lock

LOGGER = logging.getLogger("memecoin_bot.dashboard")
SESSION_COOKIE = "memeops_session"
SESSION_SECONDS = 8 * 60 * 60
REMEMBERED_SESSION_SECONDS = 14 * 24 * 60 * 60
REMEMBERED_IDLE_SECONDS = 7 * 24 * 60 * 60
SESSION_TOUCH_SECONDS = 5 * 60
PREVIEW_SECONDS = 90
STATIC_DIR = Path(__file__).with_name("static")
EXPECTED_DASHBOARD_ERRORS = (
    ConfigurationError,
    DiscoveryError,
    JournalError,
    JupiterError,
    MarketDataError,
    PaperTradingError,
    PaperAutomationError,
    PumpFunError,
    RiskRejectedError,
    RpcError,
    TransactionError,
    WalletError,
    ValueError,
    StrategyLabError,
    NarrativeError,
    PaperExecutionError,
    OperationsError,
)


class PairRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    code: str = Field(min_length=8, max_length=128)
    remember_browser: StrictBool = False


class AuditRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    mint: str = Field(min_length=32, max_length=64)

    @field_validator("mint")
    @classmethod
    def valid_mint(cls, value: str) -> str:
        try:
            Pubkey.from_string(value)
        except ValueError as exc:
            raise ValueError("Enter a valid Solana token contract address") from exc
        return value


class PreviewRequest(AuditRequest):
    side: Literal["buy", "sell"] = "buy"
    amount_sol: Decimal | None = Field(default=None, gt=0, max_digits=12, decimal_places=9)
    sell_percent: int | None = Field(default=None, ge=1, le=100)
    slippage_bps: int = Field(default=100, ge=1, le=2_000)

    @field_validator("amount_sol")
    @classmethod
    def normalize_amount(cls, value: Decimal | None) -> Decimal | None:
        return value.normalize() if value is not None else None


class ExecuteRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    preview_id: str = Field(min_length=32, max_length=64)
    acknowledgement: str = Field(min_length=1, max_length=100)


class PaperOpenRequest(AuditRequest):
    amount_sol: Decimal = Field(gt=0, max_digits=12, decimal_places=9)
    slippage_bps: int = Field(default=100, ge=1, le=2_000)


class PaperCloseRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    position_id: str = Field(pattern=r"^paper-[0-9a-f-]{36}$", max_length=50)
    slippage_bps: int = Field(default=100, ge=1, le=2_000)


class PaperCallsRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    mints: list[str] = Field(min_length=1, max_length=5)
    source_label: str | None = Field(default=None, max_length=80)

    @field_validator("mints")
    @classmethod
    def valid_mints(cls, values: list[str]) -> list[str]:
        unique = list(dict.fromkeys(value.strip() for value in values if value.strip()))
        if not unique:
            raise ValueError("Paste at least one token contract address")
        for value in unique:
            try:
                Pubkey.from_string(value)
            except ValueError as exc:
                raise ValueError("Every call must be a valid Solana token address") from exc
        return unique


class BacktestRequest(AuditRequest):
    timeframe: Literal["1m", "5m", "15m", "1h"] = "5m"
    tp_levels_bps: list[int] = Field(min_length=1, max_length=5)
    tp_sell_bps: list[int] = Field(min_length=1, max_length=5)
    stop_loss_bps: int = Field(default=3_500, ge=100, le=9_500)

    @field_validator("tp_levels_bps")
    @classmethod
    def valid_tp_levels(cls, values: list[int]) -> list[int]:
        if any(value < 100 or value > 1_000_000 for value in values):
            raise ValueError("TP targets must be between 1% and 10000%")
        if any(current <= previous for previous, current in pairwise(values)):
            raise ValueError("TP targets must be strictly increasing")
        return values

    @field_validator("tp_sell_bps")
    @classmethod
    def valid_tp_sales(cls, values: list[int]) -> list[int]:
        if any(value < 1 or value > 10_000 for value in values):
            raise ValueError("TP sales must each be between 0.01% and 100%")
        if sum(values) > 10_000:
            raise ValueError("TP sales cannot exceed 100% in total")
        return values


@dataclass(slots=True)
class Session:
    csrf_token: str
    expires_at: float
    remembered: bool = False
    last_persisted_at: float = 0


@dataclass(slots=True)
class Preview:
    side: Literal["buy", "sell"]
    mint: str
    amount_atomic: int
    slippage_bps: int
    expires_at: float


class DashboardSecurity:
    def __init__(self, pairing_code: str, remembered_store: RememberedSessionStore) -> None:
        self._code_digest = hashlib.sha256(pairing_code.encode("utf-8")).digest()
        self._remembered_store = remembered_store
        self.sessions: dict[str, Session] = {}
        self.previews: dict[str, Preview] = {}
        self.attempts: defaultdict[str, deque[float]] = defaultdict(deque)
        self.last_discovery_scan = 0.0
        self.last_decision_cycle = 0.0
        self.last_automation_command = 0.0
        self.last_call_submission = 0.0
        self.last_strategy_lab_collect = 0.0
        self.last_backtest = 0.0
        self.last_narrative_refresh = 0.0

    def pair(
        self, code: str, client: str, *, remember_browser: bool = False
    ) -> tuple[str, str, int]:
        now = time.monotonic()
        attempts = self.attempts[client]
        while attempts and attempts[0] < now - 60:
            attempts.popleft()
        if len(attempts) >= 5:
            raise HTTPException(status_code=429, detail="Too many attempts. Wait 1 minute.")
        attempts.append(now)
        candidate = hashlib.sha256(code.encode("utf-8")).digest()
        if not hmac.compare_digest(candidate, self._code_digest):
            raise HTTPException(status_code=401, detail="Pairing code is incorrect")
        session_id = secrets.token_urlsafe(32)
        csrf_token = secrets.token_urlsafe(32)
        wall_now = int(time.time())
        max_age = REMEMBERED_SESSION_SECONDS if remember_browser else SESSION_SECONDS
        if remember_browser:
            try:
                self._remembered_store.save(
                    session_id,
                    now=wall_now,
                    expires_at=wall_now + max_age,
                    idle_seconds=REMEMBERED_IDLE_SECONDS,
                )
            except (OSError, sqlite3.Error) as exc:
                raise HTTPException(
                    status_code=503, detail="Browser pairing is unavailable"
                ) from exc
        self.sessions[session_id] = Session(
            csrf_token,
            wall_now + max_age,
            remembered=remember_browser,
            last_persisted_at=wall_now,
        )
        return session_id, csrf_token, max_age

    def session(self, session_id: str | None) -> Session:
        if not session_id or not 32 <= len(session_id) <= 128:
            raise HTTPException(status_code=401, detail="Pair the dashboard first")
        wall_now = int(time.time())
        session = self.sessions.get(session_id)
        if session is not None and session.expires_at <= wall_now:
            self.sessions.pop(session_id, None)
            raise HTTPException(status_code=401, detail="Session expired; pair again")
        if session is None or session.remembered:
            try:
                remembered = self._remembered_store.get(
                    session_id, now=wall_now, idle_seconds=REMEMBERED_IDLE_SECONDS
                )
            except (OSError, sqlite3.Error) as exc:
                raise HTTPException(status_code=503, detail="Session check is unavailable") from exc
            if remembered is None:
                self.sessions.pop(session_id, None)
                raise HTTPException(status_code=401, detail="Session expired; pair again")
            expires_at, last_seen_at = remembered
            if session is None:
                session = Session(
                    secrets.token_urlsafe(32),
                    expires_at,
                    remembered=True,
                    last_persisted_at=last_seen_at,
                )
                self.sessions[session_id] = session
            if wall_now - session.last_persisted_at >= SESSION_TOUCH_SECONDS:
                try:
                    updated = self._remembered_store.touch(session_id, now=wall_now)
                except (OSError, sqlite3.Error) as exc:
                    raise HTTPException(
                        status_code=503, detail="Session check is unavailable"
                    ) from exc
                if not updated:
                    self.sessions.pop(session_id, None)
                    raise HTTPException(status_code=401, detail="Session expired; pair again")
                session.last_persisted_at = wall_now
        return session

    def revoke(self, session_id: str) -> None:
        session = self.sessions.get(session_id)
        if session is None or session.remembered:
            try:
                self._remembered_store.revoke(session_id)
            except (OSError, sqlite3.Error) as exc:
                raise HTTPException(status_code=503, detail="Sign out is unavailable") from exc
        self.sessions.pop(session_id, None)

    def revoke_all(self) -> None:
        try:
            self._remembered_store.revoke_all()
        except (OSError, sqlite3.Error) as exc:
            raise HTTPException(status_code=503, detail="Sign out is unavailable") from exc
        self.sessions.clear()

    def add_preview(self, preview: Preview) -> str:
        now = time.time()
        self.previews = {
            key: value for key, value in self.previews.items() if value.expires_at > now
        }
        preview_id = secrets.token_urlsafe(24)
        self.previews[preview_id] = preview
        return preview_id

    def take_preview(self, preview_id: str) -> Preview:
        preview = self.previews.pop(preview_id, None)
        if preview is None or preview.expires_at <= time.time():
            raise HTTPException(status_code=409, detail="Preview expired; create a new preview")
        return preview

    def reserve_discovery_scan(self) -> None:
        now = time.monotonic()
        if now - self.last_discovery_scan < 30:
            raise HTTPException(status_code=429, detail="Wait 30 seconds before scanning again")
        self.last_discovery_scan = now

    def reserve_decision_cycle(self) -> None:
        now = time.monotonic()
        if now - self.last_decision_cycle < 10:
            raise HTTPException(status_code=429, detail="Wait 10 seconds before deciding again")
        self.last_decision_cycle = now

    def reserve_automation_command(self) -> None:
        now = time.monotonic()
        if now - self.last_automation_command < 2:
            raise HTTPException(status_code=429, detail="Wait 2 seconds before another command")
        self.last_automation_command = now

    def reserve_call_submission(self) -> None:
        now = time.monotonic()
        if now - self.last_call_submission < 5:
            raise HTTPException(status_code=429, detail="Wait 5 seconds before adding calls")
        self.last_call_submission = now

    def reserve_strategy_lab_collect(self) -> None:
        now = time.monotonic()
        if now - self.last_strategy_lab_collect < 30:
            raise HTTPException(status_code=429, detail="Wait 30 seconds before collecting again")
        self.last_strategy_lab_collect = now

    def reserve_backtest(self) -> None:
        now = time.monotonic()
        if now - self.last_backtest < 2:
            raise HTTPException(status_code=429, detail="Wait 2 seconds before another replay")
        self.last_backtest = now

    def reserve_narrative_refresh(self) -> None:
        now = time.monotonic()
        if now - self.last_narrative_refresh < 30:
            raise HTTPException(
                status_code=429, detail="Wait 30 seconds before refreshing narratives"
            )
        self.last_narrative_refresh = now


def _client_address(request: Request) -> str:
    return request.client.host if request.client else "local"


def _require_session(request: Request) -> Session:
    security: DashboardSecurity = request.app.state.security
    return security.session(request.cookies.get(SESSION_COOKIE))


def _require_csrf(
    request: Request,
    session: Annotated[Session, Depends(_require_session)],
    csrf: Annotated[str | None, Header(alias="X-CSRF-Token")] = None,
) -> Session:
    if not csrf or not hmac.compare_digest(csrf, session.csrf_token):
        raise HTTPException(status_code=403, detail="Security token is missing or invalid")
    return session


def _trade_dict(report: Any) -> dict[str, Any]:
    return {
        "client_order_id": report.client_order_id,
        "mode": "live" if report.result.submitted else "dry_run",
        "audit": audit_as_dict(report.audit) if report.audit else None,
        "quote": {
            "input_mint": report.quote.input_mint,
            "output_mint": report.quote.output_mint,
            "in_amount": report.quote.in_amount,
            "out_amount": report.quote.out_amount,
            "price_impact_bps": report.quote.price_impact_bps,
            "router": report.quote.router,
            "fee_bps": report.quote.fee_bps,
        },
        "result": asdict(report.result),
    }


@contextmanager
def _service(settings: Settings) -> Iterator[TradingService]:
    keypair = load_keypair(settings.keypair_path)
    with ExitStack() as stack:
        rpc = stack.enter_context(RpcClient(settings.rpc_url))
        jupiter = stack.enter_context(JupiterClient(settings.jupiter_api_key))
        journal = stack.enter_context(TradeJournal(settings.state_path))
        yield TradingService(
            settings=settings,
            rpc=rpc,
            jupiter=jupiter,
            keypair=keypair,
            journal=journal,
        )


@contextmanager
def _paper_resources(
    settings: Settings,
) -> Iterator[tuple[PaperTradingService, RpcClient, TradingService, TradeJournal]]:
    keypair = load_keypair(settings.keypair_path)
    with ExitStack() as stack:
        rpc = stack.enter_context(RpcClient(settings.rpc_url))
        jupiter = stack.enter_context(JupiterClient(settings.jupiter_api_key))
        journal = stack.enter_context(TradeJournal(settings.state_path))
        trading = TradingService(
            settings=settings,
            rpc=rpc,
            jupiter=jupiter,
            keypair=keypair,
            journal=journal,
        )
        paper = PaperTradingService(
            settings=settings,
            trading=trading,
            jupiter=jupiter,
            journal=journal,
            taker_pubkey=str(keypair.pubkey()),
        )
        yield paper, rpc, trading, journal


def _sol_lamports(value: Decimal | None) -> int:
    if value is None:
        raise HTTPException(status_code=422, detail="Enter the SOL amount to buy")
    lamports = value * LAMPORTS_PER_SOL
    if lamports != lamports.to_integral_value():
        raise HTTPException(status_code=422, detail="SOL amount supports at most 9 decimals")
    return int(lamports)


def _discovery_entry_cutoff(candidate: dict[str, object] | None) -> int:
    """Never replay an entry before the bot first knew this token existed."""
    if candidate is None:
        return int(time.time())
    try:
        discovered = datetime.fromisoformat(str(candidate["first_seen"]))
    except (KeyError, ValueError, TypeError):
        return int(time.time())
    if discovered.tzinfo is None:
        return int(time.time())
    return ceil(discovered.timestamp())


def _research_entry_cutoff(journal: TradeJournal, candidate: dict[str, object] | None) -> int:
    discovered = _discovery_entry_cutoff(candidate)
    if candidate is None:
        return discovered
    selected = journal.first_research_selected_at(
        str(candidate["mint"]), selection_method=RESEARCH_SELECTION_METHOD
    )
    return max(discovered, selected) if selected is not None else discovered


def create_dashboard_app(settings: Settings, pairing_code: str) -> FastAPI:
    def require_active_paper_campaign() -> None:
        with TradeJournal(settings.state_path, readonly=True) as journal:
            campaign = journal.runtime_state("paper_campaign") or {}
        if campaign.get("mode") == "forward_paper_only" and campaign.get("status") == "interrupted":
            raise PaperAutomationError("Paper campaign was interrupted; start a clean run")

    def load_worker_state(state_key: str) -> dict[str, object] | None:
        with TradeJournal(settings.state_path) as journal:
            return journal.runtime_state(state_key)

    def save_worker_state(state_key: str, payload: dict[str, object]) -> None:
        with TradeJournal(settings.state_path) as journal:
            journal.save_worker_state(state_key, payload)

    def automation_cycle() -> dict[str, Any]:
        with _paper_resources(settings) as resources:
            paper, rpc, trading, journal = resources
            result = run_autonomous_paper_cycle(
                settings=settings,
                paper=paper,
                rpc=rpc,
                trading=trading,
                journal=journal,
                manage_exits=False,
                exit_monitor_status=exit_monitor.status,
            )
            result["rpc_request_counts"] = rpc.request_counts()
            return result

    def exit_cycle() -> dict[str, Any]:
        with _paper_resources(settings) as resources:
            paper, rpc, _, journal = resources
            result = run_paper_exit_cycle(settings=settings, paper=paper, journal=journal)
            result["rpc_request_counts"] = rpc.request_counts()
            return result

    exit_monitor = PaperAutomationWorker(
        interval_seconds=settings.paper_exit_interval_seconds,
        cycle=exit_cycle,
        state_loader=lambda: load_worker_state("paper_exit_monitor"),
        state_saver=lambda payload: save_worker_state("paper_exit_monitor", payload),
    )

    automation = PaperAutomationWorker(
        interval_seconds=settings.paper_automation_interval_seconds,
        cycle=automation_cycle,
        state_loader=lambda: load_worker_state("paper_automation"),
        state_saver=lambda payload: save_worker_state("paper_automation", payload),
    )

    def collection_cycle() -> dict[str, Any]:
        with TradeJournal(settings.state_path) as journal:
            result = collect_market_snapshots(
                journal,
                active_candidate_priority=lambda candidate: (
                    evaluate_candidate(candidate, settings).verdict == "eligible"
                ),
            )
            refreshed = result.get("ohlcv_refreshed_mints")
            opportunities = (
                refreshed_paper_entry_opportunities(settings, journal, refreshed)
                if isinstance(refreshed, list)
                and automation.status().get("state") in ("running", "working")
                else []
            )
        decision = None
        if opportunities and automation.status().get("state") in ("running", "working"):
            with _paper_resources(settings) as (paper, _, _, journal):
                decision = run_paper_decision_cycle(
                    settings=settings,
                    paper=paper,
                    journal=journal,
                    forced_breaker_reasons=paper_exit_monitor_issues(
                        exit_monitor.status(), settings.paper_exit_interval_seconds
                    ),
                )
        breakers = decision.get("breaker_reasons", []) if decision else []
        return {
            "paused": False,
            "entry_paused": bool(breakers),
            "pause_reason": breakers[0] if breakers else None,
            "scan": result,
            "decision": decision,
            "exits": [],
        }

    market_collector = PaperAutomationWorker(
        interval_seconds=settings.strategy_lab_collection_interval_seconds,
        cycle=collection_cycle,
        interval_from_start=True,
        state_loader=lambda: load_worker_state("market_collector"),
        state_saver=lambda payload: save_worker_state("market_collector", payload),
    )

    def narrative_cycle() -> dict[str, Any]:
        with TradeJournal(settings.state_path) as journal:
            result = refresh_narrative_intelligence(
                journal,
                gdelt_enabled=settings.narrative_gdelt_enabled,
                google_news_enabled=settings.narrative_google_news_enabled,
                bluesky_enabled=settings.narrative_bluesky_enabled,
            )
            result["openai_research"] = (
                assess_one_recent_item(
                    journal,
                    api_key=settings.openai_api_key or "",
                    daily_limit=settings.narrative_openai_daily_limit,
                )
                if settings.narrative_openai_enabled
                else {"status": "disabled"}
            )
        return {"paused": False, "narrative": result, "exits": []}

    narrative_collector = PaperAutomationWorker(
        interval_seconds=settings.narrative_refresh_interval_seconds,
        cycle=narrative_cycle,
        state_loader=lambda: load_worker_state("narrative_collector"),
        state_saver=lambda payload: save_worker_state("narrative_collector", payload),
    )

    def watchdog_cycle() -> dict[str, Any]:
        interruption = quarantine_unmonitored_forward_paper_run_locked(
            settings.state_path,
            maximum_gap_seconds=2 * settings.paper_exit_interval_seconds + 60,
        )
        if interruption:
            automation.stop()
            exit_monitor.stop()
            LOGGER.warning("Paper campaign interrupted after a runtime monitoring gap")
        return {
            "paused": bool(interruption),
            "pause_reason": "paper_campaign_interrupted" if interruption else None,
            "exits": [],
        }

    paper_watchdog = PaperAutomationWorker(
        interval_seconds=30,
        cycle=watchdog_cycle,
        state_loader=lambda: load_worker_state("paper_watchdog"),
        state_saver=lambda payload: save_worker_state("paper_watchdog", payload),
    )

    backup_manager = StateBackupManager(
        state_path=settings.state_path,
        backup_directory=(
            settings.operations_backup_directory or (settings.state_path.parent / "state-backups")
        ),
        interval_seconds=settings.operations_backup_interval_seconds,
        enabled=settings.operations_backup_enabled,
    )

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        with exclusive_worker_lock(settings.state_path):
            try:
                interruption = quarantine_unmonitored_forward_paper_run_locked(
                    settings.state_path,
                    maximum_gap_seconds=2 * settings.paper_exit_interval_seconds + 60,
                )
                if interruption:
                    LOGGER.warning("Paper campaign interrupted before worker startup")
                try:
                    backup_manager.run_if_due()
                except Exception:
                    LOGGER.exception("State backup failed during startup")
                if (
                    not interruption
                    and settings.paper_automation_autostart
                    and automation.status()["state"] != "paused"
                ):
                    exit_monitor.start()
                    automation.start()
                if not interruption:
                    with TradeJournal(settings.state_path, readonly=True) as journal:
                        campaign = journal.runtime_state("paper_campaign") or {}
                        open_count = journal._connection.execute(
                            "SELECT COUNT(*) FROM paper_positions WHERE status = 'open'"
                        ).fetchone()[0]
                    if settings.paper_automation_autostart or (
                        campaign.get("mode") == "forward_paper_only" and open_count
                    ):
                        paper_watchdog.start()
                if settings.strategy_lab_collection_enabled:
                    market_collector.start()
                if settings.narrative_intelligence_enabled:
                    narrative_collector.start()
                yield
            finally:
                paper_watchdog.shutdown()
                automation.shutdown()
                exit_monitor.shutdown()
                market_collector.shutdown()
                narrative_collector.shutdown()

    app = FastAPI(
        title="MemeOps Local Console",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
    )
    auth_path = settings.state_path.with_name(f"{settings.state_path.stem}.dashboard-auth.sqlite3")
    security = DashboardSecurity(pairing_code, RememberedSessionStore(auth_path))
    app.state.settings = settings
    app.state.security = security
    app.state.paper_automation = automation
    app.state.paper_exit_monitor = exit_monitor
    app.state.paper_watchdog = paper_watchdog
    app.state.market_collector = market_collector
    app.state.narrative_collector = narrative_collector
    app.state.backup_manager = backup_manager
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost"])
    app.mount("/assets", StaticFiles(directory=STATIC_DIR), name="assets")

    @app.middleware("http")
    async def harden_responses(request: Request, call_next: Any) -> Response:
        try:
            response = await call_next(request)
        except SubmissionUncertainError as exc:
            response = JSONResponse(
                status_code=503,
                content={
                    "error": type(exc).__name__,
                    "message": str(exc),
                    "signature": exc.signature,
                },
            )
        except EXPECTED_DASHBOARD_ERRORS as exc:
            response = JSONResponse(
                status_code=400,
                content={"error": type(exc).__name__, "message": str(exc)},
            )
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
            "connect-src 'self'; object-src 'none'; base-uri 'self'; form-action 'self'; "
            "frame-ancestors 'none'"
        )
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Permissions-Policy"] = (
            "camera=(), microphone=(), geolocation=(), payment=(), usb=()"
        )
        response.headers["Cross-Origin-Opener-Policy"] = "same-origin"
        response.headers["Cross-Origin-Resource-Policy"] = "same-origin"
        response.headers["Cache-Control"] = "no-store"
        return cast(Response, response)

    @app.exception_handler(Exception)
    async def safe_exception_handler(request: Request, exc: Exception) -> JSONResponse:
        if isinstance(exc, SubmissionUncertainError):
            return JSONResponse(
                status_code=503,
                content={
                    "error": type(exc).__name__,
                    "message": str(exc),
                    "signature": exc.signature,
                },
            )
        if isinstance(exc, EXPECTED_DASHBOARD_ERRORS):
            return JSONResponse(
                status_code=400,
                content={"error": type(exc).__name__, "message": str(exc)},
            )
        LOGGER.exception("Unhandled dashboard request failure", extra={"path": request.url.path})
        return JSONResponse(
            status_code=500,
            content={"error": "InternalError", "message": "The request could not be completed"},
        )

    @app.get("/")
    def index() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html")

    @app.get("/api/auth/status")
    def auth_status(
        dashboard_session: Annotated[str | None, Cookie(alias=SESSION_COOKIE)] = None,
    ) -> dict[str, Any]:
        try:
            session = security.session(dashboard_session)
        except HTTPException as exc:
            if exc.status_code == 401:
                return {"authenticated": False}
            raise
        return {"authenticated": True, "csrf_token": session.csrf_token}

    @app.post("/api/auth/pair")
    def pair(payload: PairRequest, request: Request, response: Response) -> dict[str, Any]:
        session_id, csrf_token, max_age = security.pair(
            payload.code, _client_address(request), remember_browser=payload.remember_browser
        )
        response.set_cookie(
            SESSION_COOKIE,
            session_id,
            max_age=max_age,
            httponly=True,
            secure=False,
            samesite="strict",
            path="/",
        )
        return {"authenticated": True, "csrf_token": csrf_token}

    @app.post("/api/auth/logout")
    def logout(
        response: Response,
        session: Annotated[Session, Depends(_require_csrf)],
        dashboard_session: Annotated[str | None, Cookie(alias=SESSION_COOKIE)] = None,
    ) -> dict[str, bool]:
        del session
        if dashboard_session:
            security.revoke(dashboard_session)
        response.delete_cookie(SESSION_COOKIE, path="/")
        return {"ok": True}

    @app.post("/api/auth/logout-all")
    def logout_all(
        response: Response,
        _: Annotated[Session, Depends(_require_csrf)],
    ) -> dict[str, bool]:
        security.revoke_all()
        response.delete_cookie(SESSION_COOKIE, path="/")
        return {"ok": True}

    @app.get("/api/status")
    def status(_: Annotated[Session, Depends(_require_session)]) -> dict[str, Any]:
        keypair = load_keypair(settings.keypair_path)
        wallet = str(keypair.pubkey())
        with RpcClient(settings.rpc_url) as rpc:
            health = rpc.call("getHealth", [], attempts=1)
            balance_result = rpc.call(
                "getBalance", [wallet, {"commitment": "confirmed"}], attempts=1
            )
        balance_value = balance_result.get("value") if isinstance(balance_result, dict) else None
        if not isinstance(balance_value, int) or balance_value < 0:
            raise RpcError("RPC returned an invalid SOL balance")
        with TradeJournal(settings.state_path) as journal:
            daily_exposure = journal.daily_exposure_lamports()
        return {
            "wallet": wallet,
            "balance_lamports": balance_value,
            "rpc_health": health,
            "live_enabled": False,
            "jupiter_configured": bool(settings.jupiter_api_key),
            "discovery_program_ids": list(settings.discovery_program_ids),
            "daily_exposure_lamports": daily_exposure,
            "limits": {
                "max_trade_lamports": settings.max_trade_lamports,
                "max_daily_lamports": settings.max_daily_lamports,
                "max_slippage_bps": settings.max_slippage_bps,
                "max_price_impact_bps": settings.max_price_impact_bps,
                "max_priority_fee_lamports": settings.max_priority_fee_lamports,
            },
            "risk_phrase": None,
            "strategy": {
                "min_liquidity_usd": settings.strategy_min_liquidity_usd,
                "min_volume_24h_usd": settings.strategy_min_volume_24h_usd,
                "min_score": settings.strategy_min_score,
                "paper_risk_per_trade_bps": settings.paper_risk_per_trade_bps,
                "max_open_positions": settings.strategy_max_open_positions,
            },
            "paper_automation": {
                "autostart": settings.paper_automation_autostart,
                "interval_seconds": settings.paper_automation_interval_seconds,
                "tp_levels_bps": list(settings.paper_tp_levels_bps),
                "tp_sell_bps": list(settings.paper_tp_sell_bps),
                "stop_loss_bps": settings.paper_stop_loss_bps,
                "trailing_activation_bps": settings.paper_trailing_activation_bps,
                "trailing_stop_bps": settings.paper_trailing_stop_bps,
                "max_hold_seconds": settings.paper_max_hold_seconds,
                "max_drawdown_bps": settings.paper_max_drawdown_bps,
                "daily_loss_bps": settings.paper_daily_loss_bps,
                "max_consecutive_losses": settings.paper_max_consecutive_losses,
                "liquidity_breaker_usd": settings.paper_liquidity_breaker_usd,
            },
            "paper_execution": {
                "base_slippage_bps": settings.paper_execution_base_slippage_bps,
                "liquidity_shock_bps": settings.paper_execution_liquidity_shock_bps,
                "network_fee_lamports": settings.paper_execution_network_fee_lamports,
                "priority_fee_lamports": settings.paper_execution_priority_fee_lamports,
                "ata_rent_lamports": settings.paper_execution_ata_rent_lamports,
                "max_quote_age_ms": settings.paper_execution_max_quote_age_ms,
                "max_trade_liquidity_bps": settings.paper_execution_max_trade_liquidity_bps,
            },
            "integrations": {
                "pumpfun": {
                    "official_api_configured": bool(settings.pump_api_key),
                    "onchain_fallback_enabled": True,
                },
                "dexscreener": {
                    "profile_discovery_enabled": settings.dexscreener_discovery_enabled,
                    "market_enrichment_enabled": True,
                },
                "narrative": {
                    "enabled": settings.narrative_intelligence_enabled,
                    "gdelt_enabled": settings.narrative_gdelt_enabled,
                    "google_news_enabled": settings.narrative_google_news_enabled,
                    "bluesky_enabled": settings.narrative_bluesky_enabled,
                    "openai_research_enabled": settings.narrative_openai_enabled,
                    "openai_key_configured": bool(settings.openai_api_key),
                    "paper_only": True,
                },
                "forensics": {
                    "enabled": settings.token_forensics_enabled,
                    "origin_trace_enabled": settings.forensics_origin_trace_enabled,
                    "max_single_controller_bps": settings.forensics_max_single_controller_bps,
                    "signature_limit": settings.forensics_signature_limit,
                    "transaction_candidates": settings.forensics_transaction_candidates,
                },
            },
        }

    @app.post("/api/audit")
    def audit(
        payload: AuditRequest,
        _: Annotated[Session, Depends(_require_csrf)],
    ) -> dict[str, Any]:
        with _service(settings) as service:
            return audit_as_dict(service.audit(payload.mint))

    @app.post("/api/trades/preview")
    def preview_trade(
        payload: PreviewRequest,
        _: Annotated[Session, Depends(_require_csrf)],
    ) -> dict[str, Any]:
        with _service(settings) as service:
            if payload.side == "buy":
                amount_atomic = _sol_lamports(payload.amount_sol)
                report = service.buy(
                    mint=payload.mint,
                    lamports=amount_atomic,
                    slippage_bps=payload.slippage_bps,
                )
            else:
                if payload.sell_percent is None:
                    raise HTTPException(status_code=422, detail="Choose a percentage to sell")
                balance = service.wallet_balance(payload.mint)
                amount_atomic = balance * payload.sell_percent // 100
                if amount_atomic <= 0:
                    raise RiskRejectedError("No token balance is available to sell")
                report = service.sell(
                    mint=payload.mint,
                    amount_atomic=amount_atomic,
                    slippage_bps=payload.slippage_bps,
                )
        preview_id = security.add_preview(
            Preview(
                side=payload.side,
                mint=payload.mint,
                amount_atomic=amount_atomic,
                slippage_bps=payload.slippage_bps,
                expires_at=time.time() + PREVIEW_SECONDS,
            )
        )
        return {
            "preview_id": preview_id,
            "expires_in_seconds": PREVIEW_SECONDS,
            "report": _trade_dict(report),
        }

    @app.post("/api/trades/execute")
    def execute_trade(
        payload: ExecuteRequest,
        _: Annotated[Session, Depends(_require_csrf)],
    ) -> dict[str, Any]:
        del payload
        raise HTTPException(
            status_code=409, detail="Live execution is locked during paper strategy validation"
        )

    @app.get("/api/orders")
    def orders(_: Annotated[Session, Depends(_require_session)]) -> dict[str, Any]:
        with TradeJournal(settings.state_path) as journal:
            return {"orders": journal.recent_orders()}

    @app.get("/api/positions")
    def positions(_: Annotated[Session, Depends(_require_session)]) -> dict[str, Any]:
        with TradeJournal(settings.state_path) as journal:
            return {"positions": journal.positions()}

    @app.get("/api/discovery/candidates")
    def discovery_candidates(
        _: Annotated[Session, Depends(_require_session)],
    ) -> dict[str, Any]:
        with TradeJournal(settings.state_path) as journal:
            return {
                "program_ids": list(settings.discovery_program_ids),
                "candidates": journal.discovery_candidates(limit=100),
            }

    @app.post("/api/discovery/scan")
    def discovery_scan(
        _: Annotated[Session, Depends(_require_csrf)],
    ) -> dict[str, Any]:
        security.reserve_discovery_scan()
        with _paper_resources(settings) as resources:
            _paper, rpc, trading, journal = resources
            return scan_candidates(
                settings=settings,
                rpc=rpc,
                trading=trading,
                journal=journal,
                reaudit_priority=lambda candidate: (
                    evaluate_candidate(candidate, settings).verdict == "eligible"
                ),
            )

    def narrative_payload() -> dict[str, Any]:
        with TradeJournal(settings.state_path) as journal:
            items = journal.narrative_items(limit=100)
            signals = journal.narrative_signals(limit=100)
            assessments = journal.narrative_model_assessments(limit=100)
            candidates = journal.discovery_candidates(limit=100)
        assessed_by_item = {
            str(assessment["item_id"]): assessment
            for assessment in assessments
            if assessment.get("status") == "complete"
        }
        candidate_names = {
            str(candidate["mint"]): {
                "symbol": candidate.get("symbol"),
                "name": candidate.get("name"),
            }
            for candidate in candidates
        }
        tags: Counter[str] = Counter()
        for item in items:
            item_tags = item.get("tags")
            if isinstance(item_tags, list):
                tags.update(str(tag) for tag in item_tags)
        return {
            "paper_only": True,
            "live_trading_enabled": False,
            "collector": narrative_collector.status(),
            "sources": {
                "gdelt": settings.narrative_gdelt_enabled,
                "google_news": settings.narrative_google_news_enabled,
                "bluesky": settings.narrative_bluesky_enabled,
            },
            "openai_research": {
                "enabled": settings.narrative_openai_enabled,
                "key_configured": bool(settings.openai_api_key),
                "daily_limit": settings.narrative_openai_daily_limit,
                "trading_signal": False,
            },
            "top_tags": [{"tag": tag, "mentions": count} for tag, count in tags.most_common(8)],
            "signals": [
                {**signal, **candidate_names.get(str(signal["mint"]), {})} for signal in signals
            ],
            "items": [
                {**item, "model_assessment": assessed_by_item.get(str(item["item_id"]))}
                for item in items
            ],
        }

    @app.get("/api/narrative")
    def narrative_overview(
        _: Annotated[Session, Depends(_require_session)],
    ) -> dict[str, Any]:
        return narrative_payload()

    @app.post("/api/narrative/refresh")
    def refresh_narrative(
        _: Annotated[Session, Depends(_require_csrf)],
    ) -> dict[str, Any]:
        security.reserve_narrative_refresh()
        result = narrative_cycle()["narrative"]
        return {"refresh": result, **narrative_payload()}

    @app.get("/api/paper/portfolio")
    def paper_portfolio(
        _: Annotated[Session, Depends(_require_session)],
    ) -> dict[str, Any]:
        with _paper_resources(settings) as resources:
            paper, _rpc, _trading, _journal = resources
            return paper.portfolio(refresh=False)

    @app.post("/api/paper/portfolio/refresh")
    def refresh_paper_portfolio(
        _: Annotated[Session, Depends(_require_csrf)],
    ) -> dict[str, Any]:
        require_active_paper_campaign()
        with _paper_resources(settings) as resources:
            paper, _rpc, _trading, _journal = resources
            return paper.portfolio(refresh=True)

    @app.post("/api/paper/open")
    def open_paper_position(
        payload: PaperOpenRequest,
        _: Annotated[Session, Depends(_require_csrf)],
    ) -> dict[str, Any]:
        require_active_paper_campaign()
        if payload.slippage_bps > settings.max_slippage_bps:
            raise RiskRejectedError("Paper slippage exceeds the configured maximum")
        with TradeJournal(settings.state_path) as journal:
            breakers = paper_entry_breakers(settings, journal)
            if breakers:
                raise RiskRejectedError(f"Paper entry paused: {breakers[0]}")
        with _paper_resources(settings) as resources:
            paper, _rpc, _trading, journal = resources
            breakers = paper_entry_breakers(settings, journal)
            if breakers:
                raise RiskRejectedError(f"Paper entry paused: {breakers[0]}")
            opened = paper.open(
                mint=payload.mint,
                lamports=_sol_lamports(payload.amount_sol),
                slippage_bps=payload.slippage_bps,
            )
            paper_watchdog.start()
            return opened

    @app.post("/api/paper/close")
    def close_paper_position(
        payload: PaperCloseRequest,
        _: Annotated[Session, Depends(_require_csrf)],
    ) -> dict[str, Any]:
        require_active_paper_campaign()
        if payload.slippage_bps > settings.max_slippage_bps:
            raise RiskRejectedError("Paper slippage exceeds the configured maximum")
        with _paper_resources(settings) as resources:
            paper, _rpc, _trading, _journal = resources
            return paper.close(
                position_id=payload.position_id,
                slippage_bps=payload.slippage_bps,
            )

    @app.get("/api/strategy/decisions")
    def strategy_decisions(
        _: Annotated[Session, Depends(_require_session)],
    ) -> dict[str, Any]:
        with TradeJournal(settings.state_path) as journal:
            decisions = journal.strategy_decisions()
        return {
            "decisions": [
                {
                    **decision,
                    "primary_reason": primary_decision_reason(
                        decision.get("verdict"),
                        decision.get("reasons"),
                        decision.get("evidence"),
                    ),
                }
                for decision in decisions
            ]
        }

    @app.post("/api/strategy/cycle")
    def strategy_cycle(
        _: Annotated[Session, Depends(_require_csrf)],
    ) -> dict[str, Any]:
        require_active_paper_campaign()
        security.reserve_decision_cycle()
        with _paper_resources(settings) as resources:
            paper, _rpc, _trading, journal = resources
            return run_paper_decision_cycle(
                settings=settings,
                paper=paper,
                journal=journal,
            )

    @app.get("/api/strategy-lab/summary")
    def strategy_lab_summary(
        _: Annotated[Session, Depends(_require_session)],
    ) -> dict[str, Any]:
        as_of = int(time.time())
        with TradeJournal(settings.state_path) as journal:
            candidates = journal.discovery_candidates(limit=100)
            cohort_mints = research_cohort_mints(journal)
            cohort_state = journal.runtime_state(RESEARCH_COHORT_KEY) or {}
            cohort_focus_size = min(research_focus_size(journal), len(cohort_mints))
            raw_cohort_start = cohort_state.get("selected_at")
            research_capture = summarize_current_research_capture(
                journal,
                cohort_mints,
                cohort_started_at=raw_cohort_start if type(raw_cohort_start) is int else None,
                focus_size=cohort_focus_size,
                as_of=as_of,
            )
            seen_mints = {str(candidate["mint"]) for candidate in candidates}
            for mint in cohort_mints:
                if mint not in seen_mints:
                    candidate_saved = journal.discovery_candidate(mint)
                    if candidate_saved:
                        candidates.append(candidate_saved)
            risk_metrics = journal.paper_risk_metrics()
            entry_readiness = summarize_current_entry_readiness(journal, settings)
            runs = journal.backtest_runs(limit=25)
            followup_rows = summarize_decision_followup_paths(journal, as_of=as_of, limit=1)
            selections = followup_rows["selections"]
            latest_followup = selections[0] if isinstance(selections, list) and selections else None
            for run in runs:
                saved = run.get("result")
                if (
                    not isinstance(saved, dict)
                    or saved.get("data_source") != "geckoterminal_pool_trades"
                ):
                    run["result"] = {
                        "status": "legacy_unverified",
                        "reason": (
                            "Run predates verified pool OHLCV and is excluded from performance"
                        ),
                        "promotion_ready": False,
                    }
                elif saved.get("entry_model") is None:
                    run["result"] = {
                        "status": "legacy_entry_model_unverified",
                        "reason": (
                            "Run predates shared 1m/5m confirmation and is excluded "
                            "from performance"
                        ),
                        "promotion_ready": False,
                    }
                elif saved.get("candle_provenance_version") != CANDLE_PROVENANCE_VERSION:
                    run["result"] = {
                        "status": "legacy_candle_provenance_unverified",
                        "reason": "Run predates the current candle and replay policy",
                        "promotion_ready": False,
                    }
            tracked: list[dict[str, Any]] = []
            for candidate in candidates:
                snapshots = journal.market_snapshots(str(candidate["mint"]), limit=10_000)
                pool = candidate.get("pair_address")
                candle_stats = (
                    journal.pool_candle_stats(str(candidate["mint"]), pool, "1m")
                    if isinstance(pool, str)
                    else {
                        "count": 0,
                        "last_opened_at": None,
                        "trusted_count": 0,
                        "last_trusted_opened_at": None,
                    }
                )
                if snapshots or candle_stats["count"]:
                    last_trusted = candle_stats["last_trusted_opened_at"]
                    trusted_age = (
                        max(0, as_of - last_trusted - 60) if isinstance(last_trusted, int) else None
                    )
                    tracked.append(
                        {
                            "mint": candidate["mint"],
                            "symbol": candidate.get("symbol"),
                            "name": candidate.get("name"),
                            "snapshot_count": len(snapshots),
                            "candle_count_1m": candle_stats["count"],
                            "trusted_candle_count_1m": candle_stats["trusted_count"],
                            "trusted_close_age_seconds_1m": trusted_age,
                            "last_closed_at": candle_stats["last_opened_at"],
                            "pool": pool,
                        }
                    )
        tracked.sort(
            key=lambda item: (int(item["candle_count_1m"]), int(item["snapshot_count"])),
            reverse=True,
        )
        return {
            "live_trading_enabled": False,
            "collector": market_collector.status(),
            "research_cohort": {
                "size": len(cohort_mints),
                "focus_size": cohort_focus_size,
                "population_count": cohort_state.get("population_count"),
                "selected_at": cohort_state.get("selected_at"),
                "expires_at": cohort_state.get("expires_at"),
                "method": cohort_state.get("selection_method"),
            },
            "latest_followup": latest_followup,
            "research_capture": research_capture,
            "entry_readiness": entry_readiness,
            "tracked": tracked[:30],
            "risk": {
                **risk_metrics,
                "daily_loss_limit_lamports": (
                    settings.paper_starting_lamports * settings.paper_daily_loss_bps // 10_000
                ),
                "max_consecutive_losses": settings.paper_max_consecutive_losses,
                "liquidity_breaker_usd": settings.paper_liquidity_breaker_usd,
                "max_drawdown_bps": settings.paper_max_drawdown_bps,
            },
            "defaults": {
                "tp_levels_bps": list(settings.paper_tp_levels_bps),
                "tp_sell_bps": list(settings.paper_tp_sell_bps),
                "stop_loss_bps": settings.paper_stop_loss_bps,
            },
            "runs": runs,
        }

    @app.get("/api/strategy-lab/candles")
    def strategy_lab_candles(
        mint: str,
        timeframe: Literal["1m", "5m", "15m", "1h"],
        _: Annotated[Session, Depends(_require_session)],
    ) -> dict[str, Any]:
        validated = AuditRequest(mint=mint)
        with TradeJournal(settings.state_path) as journal:
            candidate = journal.discovery_candidate(validated.mint)
            pool = candidate.get("pair_address") if candidate else None
            snapshots = journal.market_snapshots(validated.mint, limit=10_000)
            candles = (
                load_verified_candles(journal, validated.mint, pool, timeframe)
                if isinstance(pool, str)
                else []
            )
        as_of = int(time.time())
        signal = analyze_entry(
            candles,
            minimum_liquidity_usd=float(settings.strategy_min_liquidity_usd),
            timeframe=timeframe,
            as_of=as_of,
        )
        latest_trusted = next(
            (candle for candle in reversed(candles) if candle.first_observed_values_at is not None),
            None,
        )
        return {
            "mint": validated.mint,
            "timeframe": timeframe,
            "snapshot_count": len(snapshots),
            "candles": [candle.as_dict() for candle in candles[-500:]],
            "signal": signal.as_dict(),
            "latest_trusted_close_age_seconds": (
                max(0, as_of - latest_trusted.opened_at - TIMEFRAME_SECONDS[timeframe])
                if latest_trusted is not None
                else None
            ),
            "volume_note": (
                "Pool trade volume in USD from GeckoTerminal; incomplete candles excluded."
            ),
            "source": "geckoterminal_pool_trades" if candles else "unavailable",
            "pool": pool,
        }

    @app.post("/api/strategy-lab/collect")
    def strategy_lab_collect(
        _: Annotated[Session, Depends(_require_csrf)],
    ) -> dict[str, object]:
        security.reserve_strategy_lab_collect()
        with TradeJournal(settings.state_path) as journal:
            return collect_market_snapshots(
                journal,
                active_candidate_priority=lambda candidate: (
                    evaluate_candidate(candidate, settings).verdict == "eligible"
                ),
            )

    @app.post("/api/strategy-lab/backtest")
    def strategy_lab_backtest(
        payload: BacktestRequest,
        _: Annotated[Session, Depends(_require_csrf)],
    ) -> dict[str, Any]:
        security.reserve_backtest()
        if len(payload.tp_levels_bps) != len(payload.tp_sell_bps):
            raise HTTPException(
                status_code=422,
                detail="Provide one sale portion for every take-profit target",
            )
        swap_fraction_bps = estimated_paper_swap_fraction_bps(settings)
        replay_costs = estimated_replay_costs(settings)
        if swap_fraction_bps == 0:
            raise HTTPException(
                status_code=422, detail="Paper risk budget is too small for an entry"
            )
        with TradeJournal(settings.state_path) as journal:
            candidate = journal.discovery_candidate(payload.mint)
            pool = candidate.get("pair_address") if candidate else None
            candles = (
                load_verified_candles(journal, payload.mint, pool, payload.timeframe)
                if isinstance(pool, str)
                else []
            )
            result = run_backtest(
                candles,
                exit_model="paper_polling_proxy",
                paper_poll_seconds=settings.paper_exit_interval_seconds,
                timeframe=payload.timeframe,
                minimum_liquidity_usd=float(settings.strategy_min_liquidity_usd),
                tp_levels_bps=tuple(payload.tp_levels_bps),
                tp_sell_bps=tuple(payload.tp_sell_bps),
                stop_loss_bps=payload.stop_loss_bps,
                execution_cost_bps=estimated_round_trip_cost_bps(settings),
                fixed_cost_bps=replay_costs.fixed_cost_bps,
                refundable_ata_reserve_bps=replay_costs.refundable_ata_reserve_bps,
                additional_exit_fee_bps=estimated_additional_exit_fee_bps(settings),
                position_fraction_bps=swap_fraction_bps,
                trailing_activation_bps=settings.paper_trailing_activation_bps,
                trailing_stop_bps=settings.paper_trailing_stop_bps,
                max_hold_seconds=settings.paper_max_hold_seconds,
                minimum_exit_liquidity_usd=float(settings.paper_liquidity_breaker_usd),
                max_consecutive_losses=settings.paper_max_consecutive_losses,
                max_drawdown_bps=settings.paper_max_drawdown_bps,
                daily_loss_bps=settings.paper_daily_loss_bps,
                entry_not_before=_research_entry_cutoff(journal, candidate),
                entry_windows=journal.research_entry_windows(
                    payload.mint, selection_method=RESEARCH_SELECTION_METHOD
                ),
                confirmation_frames=(
                    {
                        frame: load_verified_candles(journal, payload.mint, pool, frame)
                        for frame in ("5m", "15m", "1h")
                    }
                    if payload.timeframe == "1m" and isinstance(pool, str)
                    else None
                ),
                audit_observations=journal.audit_observations(payload.mint),
                market_snapshots=(
                    journal.market_snapshots(payload.mint) if isinstance(pool, str) else None
                ),
                minimum_volume_24h_usd=float(settings.strategy_min_volume_24h_usd),
                expected_pool=pool if isinstance(pool, str) else None,
                market_score_replayer=(
                    make_replay_market_scorer(journal, settings, payload.mint)
                    if isinstance(pool, str)
                    else None
                ),
            )
            result["data_source"] = "geckoterminal_pool_trades"
            if payload.timeframe == "1m":
                result["candle_provenance_version"] = CANDLE_PROVENANCE_VERSION
            result["universe_scope"] = "single_current_discovery_candidate"
            run_id = journal.record_backtest(
                mint=payload.mint,
                timeframe=payload.timeframe,
                strategy_name=(
                    "multi-timeframe-candle-gate-v3"
                    if payload.timeframe == "1m"
                    else "single-frame-research-v1"
                ),
                result=result,
            )
        return {"run_id": run_id, "paper_only": True, "result": result}

    @app.post("/api/strategy-lab/evaluate")
    def strategy_lab_evaluate(
        payload: BacktestRequest,
        _: Annotated[Session, Depends(_require_csrf)],
    ) -> dict[str, Any]:
        security.reserve_backtest()
        if len(payload.tp_levels_bps) != len(payload.tp_sell_bps):
            raise HTTPException(
                status_code=422,
                detail="Provide one sale portion for every take-profit target",
            )
        swap_fraction_bps = estimated_paper_swap_fraction_bps(settings)
        replay_costs = estimated_replay_costs(settings)
        if swap_fraction_bps == 0:
            raise HTTPException(
                status_code=422, detail="Paper risk budget is too small for an entry"
            )
        with TradeJournal(settings.state_path) as journal:
            candidate = journal.discovery_candidate(payload.mint)
            pool = candidate.get("pair_address") if candidate else None
            candles = (
                load_verified_candles(journal, payload.mint, pool, payload.timeframe)
                if isinstance(pool, str)
                else []
            )
            result = run_walk_forward_evaluation(
                candles,
                paper_poll_seconds=settings.paper_exit_interval_seconds,
                timeframe=payload.timeframe,
                minimum_liquidity_usd=float(settings.strategy_min_liquidity_usd),
                tp_levels_bps=tuple(payload.tp_levels_bps),
                tp_sell_bps=tuple(payload.tp_sell_bps),
                stop_loss_bps=payload.stop_loss_bps,
                execution_cost_bps=estimated_round_trip_cost_bps(settings),
                fixed_cost_bps=replay_costs.fixed_cost_bps,
                refundable_ata_reserve_bps=replay_costs.refundable_ata_reserve_bps,
                additional_exit_fee_bps=estimated_additional_exit_fee_bps(settings),
                position_fraction_bps=swap_fraction_bps,
                trailing_activation_bps=settings.paper_trailing_activation_bps,
                trailing_stop_bps=settings.paper_trailing_stop_bps,
                max_hold_seconds=settings.paper_max_hold_seconds,
                minimum_exit_liquidity_usd=float(settings.paper_liquidity_breaker_usd),
                max_consecutive_losses=settings.paper_max_consecutive_losses,
                max_drawdown_bps=settings.paper_max_drawdown_bps,
                daily_loss_bps=settings.paper_daily_loss_bps,
                entry_not_before=_research_entry_cutoff(journal, candidate),
                entry_windows=journal.research_entry_windows(
                    payload.mint, selection_method=RESEARCH_SELECTION_METHOD
                ),
                closed_paper_positions=journal.closed_paper_position_count(),
                confirmation_frames=(
                    {
                        frame: load_verified_candles(journal, payload.mint, pool, frame)
                        for frame in ("5m", "15m", "1h")
                    }
                    if payload.timeframe == "1m" and isinstance(pool, str)
                    else None
                ),
                audit_observations=journal.audit_observations(payload.mint),
                market_snapshots=(
                    journal.market_snapshots(payload.mint) if isinstance(pool, str) else None
                ),
                minimum_volume_24h_usd=float(settings.strategy_min_volume_24h_usd),
                expected_pool=pool if isinstance(pool, str) else None,
                market_score_replayer=(
                    make_replay_market_scorer(journal, settings, payload.mint)
                    if isinstance(pool, str)
                    else None
                ),
            )
            result["data_source"] = "geckoterminal_pool_trades"
            if payload.timeframe == "1m":
                result["candle_provenance_version"] = CANDLE_PROVENANCE_VERSION
            run_id = journal.record_backtest(
                mint=payload.mint,
                timeframe=payload.timeframe,
                strategy_name="walk-forward-evaluation-v1",
                result=result,
            )
        return {"run_id": run_id, "paper_only": True, "result": result}

    def automation_status_payload() -> dict[str, Any]:
        with TradeJournal(settings.state_path) as journal:
            events = journal.automation_events(limit=25)
        automation_state = automation.status()
        exit_state = exit_monitor.status()
        return {
            **automation_state,
            "exit_monitor": exit_state,
            "exit_monitor_issues": (
                paper_exit_monitor_issues(exit_state, settings.paper_exit_interval_seconds)
                if automation_state["running"]
                else []
            ),
            "events": events,
            "policy": {
                "tp_levels_bps": list(settings.paper_tp_levels_bps),
                "tp_sell_bps": list(settings.paper_tp_sell_bps),
                "stop_loss_bps": settings.paper_stop_loss_bps,
                "trailing_activation_bps": settings.paper_trailing_activation_bps,
                "trailing_stop_bps": settings.paper_trailing_stop_bps,
                "max_hold_seconds": settings.paper_max_hold_seconds,
                "max_drawdown_bps": settings.paper_max_drawdown_bps,
                "daily_loss_bps": settings.paper_daily_loss_bps,
                "max_consecutive_losses": settings.paper_max_consecutive_losses,
                "liquidity_breaker_usd": settings.paper_liquidity_breaker_usd,
                "live_trading_enabled": False,
            },
        }

    @app.get("/api/automation/status")
    def automation_status(
        _: Annotated[Session, Depends(_require_session)],
    ) -> dict[str, Any]:
        return automation_status_payload()

    @app.post("/api/automation/start")
    def start_automation(
        _: Annotated[Session, Depends(_require_csrf)],
    ) -> dict[str, Any]:
        require_active_paper_campaign()
        security.reserve_automation_command()
        paper_watchdog.start()
        exit_monitor.start()
        automation.start()
        return automation_status_payload()

    @app.post("/api/automation/stop")
    def stop_automation(
        _: Annotated[Session, Depends(_require_csrf)],
    ) -> dict[str, Any]:
        security.reserve_automation_command()
        automation.stop()
        exit_monitor.stop()
        return automation_status_payload()

    @app.post("/api/automation/run-once")
    def run_automation_once(
        _: Annotated[Session, Depends(_require_csrf)],
    ) -> dict[str, Any]:
        require_active_paper_campaign()
        security.reserve_automation_command()
        paper_watchdog.start()
        automation.run_once()
        return automation_status_payload()

    def operations_payload() -> dict[str, Any]:
        return operational_health(
            state_path=settings.state_path,
            workers={
                "paper_automation": automation.status(),
                "paper_exit_monitor": exit_monitor.status(),
                "paper_watchdog": paper_watchdog.status(),
                "market_collector": market_collector.status(),
                "narrative_collector": narrative_collector.status(),
            },
            backup=backup_manager.status(),
        )

    @app.get("/api/operations/health")
    def operations_health(
        _: Annotated[Session, Depends(_require_session)],
    ) -> dict[str, Any]:
        return operations_payload()

    @app.post("/api/operations/backup")
    def operations_backup(
        _: Annotated[Session, Depends(_require_csrf)],
    ) -> dict[str, Any]:
        security.reserve_automation_command()
        result = backup_manager.run_if_due(force=True)
        return {"backup": result, "health": operations_payload()}

    @app.get("/api/calls")
    def paper_calls(
        _: Annotated[Session, Depends(_require_session)],
    ) -> dict[str, Any]:
        with TradeJournal(settings.state_path) as journal:
            return {"calls": journal.paper_calls(limit=50)}

    @app.post("/api/calls")
    def submit_paper_calls(
        payload: PaperCallsRequest,
        _: Annotated[Session, Depends(_require_csrf)],
    ) -> dict[str, Any]:
        require_active_paper_campaign()
        security.reserve_call_submission()
        results: list[dict[str, Any]] = []
        with _paper_resources(settings) as resources:
            paper, _rpc, trading, journal = resources
            for mint in payload.mints:
                results.append(
                    process_paper_call(
                        settings=settings,
                        mint=mint,
                        source_label=payload.source_label,
                        paper=paper,
                        trading=trading,
                        journal=journal,
                    )
                )
            calls = journal.paper_calls(limit=50)
        return {"results": results, "calls": calls}

    return app


def run_dashboard(settings: Settings, *, port: int, open_browser: bool) -> None:
    if not 1_024 <= port <= 65_535:
        raise ConfigurationError("Dashboard port must be between 1024 and 65535")
    pairing_code = secrets.token_urlsafe(10)
    url = f"http://127.0.0.1:{port}"
    print("\nMemeOps local dashboard")
    print(f"Open: {url}")
    print(f"Startup pairing code: {pairing_code}")
    print("The server only accepts connections from this computer. Press Ctrl+C to stop.\n")
    if open_browser:
        webbrowser.open(url)
    uvicorn.run(
        create_dashboard_app(settings, pairing_code),
        host="127.0.0.1",
        port=port,
        log_level="warning",
        access_log=False,
    )
