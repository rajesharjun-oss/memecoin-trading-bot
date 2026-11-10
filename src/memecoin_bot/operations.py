from __future__ import annotations

import hashlib
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from .journal import TradeJournal


class OperationsError(RuntimeError):
    """Raised when a bounded local maintenance operation fails."""


def summarize_ohlcv_collection_capacity(journal: TradeJournal) -> dict[str, object]:
    """Report observed public-feed throughput beside the 1m entry demand."""
    state = journal.runtime_state("ohlcv_request_budget") or {}
    raw = state.get("recent_cycles")
    now = int(datetime.now(UTC).timestamp())
    cycles = (
        [
            row
            for row in raw
            if isinstance(row, dict)
            and type(row.get("started_at")) is int
            and type(row.get("at")) is int
            and now - 3_600 <= row["started_at"] <= row["at"] <= now
            and type(row.get("attempted")) is int
            and type(row.get("succeeded")) is int
            and 0 <= row["succeeded"] <= row["attempted"]
            and type(row.get("cache_hits")) is int
            and 0 <= row["cache_hits"] <= row["succeeded"]
            and type(row.get("rate_limited")) is bool
        ]
        if isinstance(raw, list)
        else []
    )
    cycles.sort(key=lambda row: row["at"])
    elapsed = cycles[-1]["at"] - cycles[0]["started_at"] if cycles else 0
    latest_ready = cycles[-1].get("market_ready") if cycles else None
    market_ready = latest_ready if type(latest_ready) is int and latest_ready >= 0 else None
    cache_hits = sum(row["cache_hits"] for row in cycles)
    succeeded = sum(row["succeeded"] for row in cycles) - cache_hits
    return {
        "scope": "observed_last_hour_public_ohlcv_network_success_cycles",
        "cycles": len(cycles),
        "window_observed_seconds": elapsed,
        "requests_attempted": sum(row["attempted"] for row in cycles),
        "requests_succeeded": succeeded,
        "cache_hits": cache_hits,
        "rate_limited_cycles": sum(row["rate_limited"] for row in cycles),
        "zero_request_cycles": sum(row["attempted"] == row["cache_hits"] for row in cycles),
        "observed_successful_requests_per_minute": (
            round(succeeded * 60 / elapsed, 2) if elapsed >= 120 else None
        ),
        "latest_market_ready_mints": market_ready,
        "minimum_calls_per_minute_to_refresh_each_ready_mint_every_120s": (
            round(market_ready / 2, 2) if market_ready is not None else None
        ),
        "capacity_note": (
            "The minimum excludes research, follow-up, and higher-timeframe requests; "
            "successful calls exclude local cache hits; attempted calls may undercount HTTP "
            "retries. Observed throughput is not a guaranteed future quota."
        ),
    }


def paper_exit_monitor_issues(
    status: dict[str, object], interval_seconds: int, *, now: datetime | None = None
) -> list[str]:
    """Use the same fail-closed monitor gate for entries and operations health."""
    if status.get("state") != "running" or status.get("last_error"):
        return ["exit_monitor_unavailable"]
    run_id = status.get("run_id")
    if not isinstance(run_id, str) or not run_id or status.get("last_completed_run_id") != run_id:
        return ["exit_monitor_unavailable"]
    completed_raw = status.get("last_completed_at")
    try:
        completed_at = datetime.fromisoformat(str(completed_raw))
    except ValueError:
        return ["exit_monitor_unavailable"]
    current = now or datetime.now(UTC)
    age = (current - completed_at).total_seconds() if completed_at.tzinfo else None
    if age is None or not -30 <= age <= 2 * interval_seconds + 60:
        return ["exit_monitor_stale"]
    result = status.get("last_result")
    if not isinstance(result, dict):
        return ["exit_monitor_unavailable"]
    reasons: list[str] = []
    if result.get("exit_failures"):
        reasons.append("exit_execution_failed")
    if result.get("quote_failures"):
        reasons.append("exit_quote_unavailable")
    return reasons


def paper_watchdog_issues(
    status: dict[str, object], interval_seconds: int = 30, *, now: datetime | None = None
) -> list[str]:
    """Require a recent completion from the current watchdog process."""
    if (
        status.get("state") != "running"
        or not status.get("running")
        or status.get("last_error")
        or not isinstance(status.get("run_id"), str)
        or status.get("last_completed_run_id") != status.get("run_id")
    ):
        return ["paper_watchdog_unavailable"]
    try:
        completed = datetime.fromisoformat(str(status.get("last_completed_at")))
    except ValueError:
        return ["paper_watchdog_unavailable"]
    current = now or datetime.now(UTC)
    age = (current - completed).total_seconds() if completed.tzinfo else None
    if age is None or not -30 <= age <= 2 * interval_seconds + 60:
        return ["paper_watchdog_stale"]
    return []


def summarize_paper_continuity(
    journal: TradeJournal,
    workers: dict[str, dict[str, object]],
    *,
    now: datetime | None = None,
) -> dict[str, object]:
    """Measure paper observation only from prospectively recorded worker cycles."""
    current = now or datetime.now(UTC)
    campaign = journal.runtime_state("paper_campaign") or {}
    started_raw = campaign.get("started_at")
    try:
        campaign_started = datetime.fromisoformat(str(started_raw))
    except ValueError:
        campaign_started = None
    if campaign_started is None or campaign_started.tzinfo is None:
        return {"state": "no_campaign", "workers": {}, "current_streak_seconds": 0}

    # A 31-day read still proves a 30-day streak, with one day of margin for
    # the first recorded completion. Keep dashboard reads bounded as the
    # campaign journal grows beyond its validation window.
    history_start = max(campaign_started, current - timedelta(days=31))
    required = ("paper_automation", "paper_exit_monitor", "market_collector")
    summaries: dict[str, dict[str, object]] = {}
    starts: list[datetime] = []
    for name in required:
        status = workers.get(name) or {}
        raw_interval = status.get("interval_seconds")
        interval = raw_interval if type(raw_interval) is int and raw_interval > 0 else 300
        allowed_gap = 2 * interval + 60
        rows = journal.worker_cycles_since(name, history_start.isoformat())
        streak_start: datetime | None = None
        previous_success: datetime | None = None
        latest_success: datetime | None = None
        latest_run_id: str | None = None
        gaps = 0
        errors = 0
        restarts = 0
        for row in rows:
            completed = datetime.fromisoformat(str(row["completed_at"]))
            if row["outcome"] != "completed":
                errors += int(row["outcome"] == "error")
                streak_start = None
                previous_success = None
                continue
            run_id = row["run_id"]
            if previous_success is not None and run_id != latest_run_id:
                restarts += 1
                streak_start = completed
            elif (
                previous_success is not None
                and (completed - previous_success).total_seconds() > allowed_gap
            ):
                gaps += 1
                streak_start = completed
            elif streak_start is None:
                streak_start = completed
            previous_success = completed
            latest_success = completed
            latest_run_id = run_id
        fresh = (
            latest_success is not None
            and -30 <= (current - latest_success).total_seconds() <= allowed_gap
        )
        active = (
            status.get("running") is True
            and not status.get("last_error")
            and status.get("run_id") == latest_run_id
        )
        if not fresh or not active:
            streak_start = None
        if streak_start is not None:
            starts.append(streak_start)
        summaries[name] = {
            "recorded_cycles": len(rows),
            "error_cycles": errors,
            "heartbeat_gaps": gaps,
            "worker_restarts": restarts,
            "allowed_gap_seconds": allowed_gap,
            "latest_success_at": latest_success.isoformat() if latest_success else None,
            "current_streak_started_at": streak_start.isoformat() if streak_start else None,
            "current_streak_seconds": (
                max(0, int((current - streak_start).total_seconds())) if streak_start else 0
            ),
        }
    all_current = len(starts) == len(required)
    common_start = max(starts) if all_current else None
    return {
        "state": "current" if all_current else "unverified",
        "campaign_started_at": campaign_started.isoformat(),
        "history_window_started_at": history_start.isoformat(),
        "current_streak_started_at": common_start.isoformat() if common_start else None,
        "current_streak_seconds": (
            max(0, int((current - common_start).total_seconds())) if common_start else 0
        ),
        "workers": summaries,
        "definition": (
            "Prospective successful cycle completions in the latest 31 days for paper decisions, "
            "exit monitoring, and market collection. Counts are limited to this window. A gap "
            "over twice the configured interval plus 60 seconds "
            "or a worker restart resets the streak; errors, stops, and stale heartbeats also "
            "invalidate it. "
            "This measures process continuity, not market data quality or strategy performance."
        ),
    }


class StateBackupManager:
    def __init__(
        self,
        *,
        state_path: Path,
        backup_directory: Path,
        interval_seconds: int,
        enabled: bool,
    ) -> None:
        self.state_path = state_path.resolve()
        self.backup_directory = backup_directory.resolve()
        self.interval_seconds = interval_seconds
        self.enabled = enabled
        self._lock = threading.Lock()
        self._last_error: str | None = None

    def _persisted(self) -> dict[str, object]:
        with TradeJournal(self.state_path) as journal:
            return journal.runtime_state("state_backup") or {}

    def status(self) -> dict[str, object]:
        persisted = self._persisted()
        return {
            "enabled": self.enabled,
            "interval_seconds": self.interval_seconds,
            "backup_directory": str(self.backup_directory),
            "last_backup_at": persisted.get("last_backup_at"),
            "last_backup_path": persisted.get("last_backup_path"),
            "last_backup_sha256": persisted.get("last_backup_sha256"),
            "last_error": self._last_error,
        }

    def run_if_due(self, *, force: bool = False) -> dict[str, object]:
        if not self.enabled and not force:
            return {**self.status(), "created": False, "reason": "disabled"}
        if not self._lock.acquire(blocking=False):
            raise OperationsError("A state backup is already running")
        try:
            persisted = self._persisted()
            last_raw = persisted.get("last_backup_at")
            if not force and isinstance(last_raw, str):
                try:
                    last = datetime.fromisoformat(last_raw)
                except ValueError:
                    last = None
                if last is not None and datetime.now(UTC) - last < timedelta(
                    seconds=self.interval_seconds
                ):
                    return {**self.status(), "created": False, "reason": "not_due"}

            self.backup_directory.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
            destination = (self.backup_directory / f"trade-state-{stamp}.sqlite3").resolve()
            if destination == self.state_path:
                raise OperationsError("Backup destination must differ from the live state database")
            with TradeJournal(self.state_path) as journal:
                journal.backup_to(destination)
            digest = hashlib.sha256(destination.read_bytes()).hexdigest()
            payload: dict[str, object] = {
                "last_backup_at": datetime.now(UTC).isoformat(),
                "last_backup_path": str(destination),
                "last_backup_sha256": digest,
                "last_backup_bytes": destination.stat().st_size,
            }
            with TradeJournal(self.state_path) as journal:
                journal.set_runtime_state("state_backup", payload)
            self._last_error = None
            return {**payload, "created": True}
        except Exception as exc:
            self._last_error = f"{type(exc).__name__}: {str(exc)[:300]}"
            raise
        finally:
            self._lock.release()


def operational_health(
    *,
    state_path: Path,
    workers: dict[str, dict[str, object]],
    backup: dict[str, object],
) -> dict[str, Any]:
    def worker_issues(name: str, status: dict[str, object]) -> list[str]:
        issues: list[str] = []
        if status.get("state") == "degraded":
            issues.append("worker_degraded")
        if status.get("last_error"):
            issues.append("worker_error")
        result = status.get("last_result")
        if not isinstance(result, dict):
            return issues
        if name != "market_collector":
            for key in ("quote_failures", "exit_failures"):
                if type(result.get(key)) is int and result[key] > 0:
                    issues.append(key)
        if name == "market_collector":
            collection = result.get("market_collection")
            if not isinstance(collection, dict):
                return issues
            if collection.get("rate_limited") is True:
                issues.append("ohlcv_rate_limited")
            dex_attempted = collection.get("dex_requests_attempted")
            dex_succeeded = collection.get("dex_requests_succeeded")
            if (
                type(dex_attempted) is int
                and type(dex_succeeded) is int
                and dex_attempted > 0
                and 0 <= dex_succeeded < dex_attempted
            ):
                issues.append("dex_requests_failed")
            ohlcv_attempted = collection.get("ohlcv_requests_attempted")
            ohlcv_succeeded = collection.get("ohlcv_requests_succeeded")
            provider_errors = collection.get("provider_error_count")
            if (
                type(ohlcv_attempted) is int
                and type(ohlcv_succeeded) is int
                and type(provider_errors) is int
                and ohlcv_attempted > 0
                and ohlcv_succeeded == 0
                and provider_errors > 0
            ):
                issues.append("ohlcv_requests_failed")
        return issues

    with TradeJournal(state_path) as journal:
        database_ok = journal.quick_check()
        ohlcv_capacity = summarize_ohlcv_collection_capacity(journal)
        paper_continuity = summarize_paper_continuity(journal, workers)
    degradation_reasons = {
        name: issues for name, status in workers.items() if (issues := worker_issues(name, status))
    }
    observed_rate = ohlcv_capacity["observed_successful_requests_per_minute"]
    required_rate = ohlcv_capacity["minimum_calls_per_minute_to_refresh_each_ready_mint_every_120s"]
    if (
        "market_collector" in workers
        and isinstance(observed_rate, (int, float))
        and isinstance(required_rate, (int, float))
        and observed_rate < required_rate
    ):
        degradation_reasons.setdefault("market_collector", []).append("ohlcv_capacity_below_demand")
    paper_worker = workers.get("paper_automation") or {}
    if paper_worker.get("running"):
        if "paper_watchdog" in workers:
            watchdog = workers["paper_watchdog"]
            interval = watchdog.get("interval_seconds")
            watchdog_issues = paper_watchdog_issues(
                watchdog, interval if type(interval) is int and interval > 0 else 30
            )
            if watchdog_issues:
                degradation_reasons.setdefault("paper_watchdog", []).extend(watchdog_issues)
        exit_worker = workers.get("paper_exit_monitor") or {}
        raw_interval = exit_worker.get("interval_seconds")
        interval = (
            raw_interval if type(raw_interval) is int and 30 <= raw_interval <= 86_400 else 120
        )
        monitor_issues = paper_exit_monitor_issues(exit_worker, interval)
        if monitor_issues:
            current_issues = degradation_reasons.setdefault("paper_exit_monitor", [])
            for issue in monitor_issues:
                if issue not in current_issues:
                    current_issues.append(issue)
    degraded_workers = list(degradation_reasons)
    state = "healthy"
    if not database_ok:
        state = "unhealthy"
    elif degraded_workers or backup.get("last_error"):
        state = "degraded"
    return {
        "state": state,
        "database": {"quick_check": "ok" if database_ok else "failed"},
        "ohlcv_capacity": ohlcv_capacity,
        "paper_continuity": paper_continuity,
        "workers": workers,
        "degraded_workers": degraded_workers,
        "degradation_reasons": degradation_reasons,
        "backup": backup,
        "checked_at": datetime.now(UTC).isoformat(),
        "live_trading_enabled": False,
    }
