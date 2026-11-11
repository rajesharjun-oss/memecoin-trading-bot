"""Optional, isolated Telegram alerts for paper-worker health."""

from __future__ import annotations

import logging
import re
import time
from collections.abc import Callable
from datetime import UTC, datetime

import httpx

from .config import Settings
from .journal import TradeJournal
from .operations import paper_exit_monitor_issues
from .worker_lock import exclusive_worker_lock

LOGGER = logging.getLogger(__name__)
STATE_KEY = "telegram_alerts_v1"
COOLDOWN_SECONDS = 30 * 60
HEARTBEAT_SECONDS = 24 * 60 * 60
TELEGRAM_ORIGIN = "https://api.telegram.org"
_TOKEN_PATTERN = re.compile(r"[0-9]{5,15}:[A-Za-z0-9_-]{20,}")
_CHAT_PATTERN = re.compile(r"-?[0-9]{1,20}")
_MESSAGES = {
    "exit_monitor_stale": "Paper exit monitor is stale",
    "exit_monitor_unavailable": "Paper exit monitor is unavailable",
    "exit_execution_failed": "Paper exit execution failed",
    "exit_quote_unavailable": "Paper exit quote is unavailable",
    "position_mark_gap": "An open paper position has no fresh mark",
    "paper_campaign_interrupted": "Forward paper campaign was interrupted",
    "circuit_breaker": "Paper entry circuit breaker is active",
    "collector_failure:market_collector": "Market collector has failed or stalled",
    "collector_failure:narrative_collector": "Narrative collector has failed or stalled",
    "worker_crash:paper_automation": "Paper decision worker has failed or stopped",
    "worker_crash:paper_exit_monitor": "Paper exit worker has failed or stopped",
    "worker_crash:paper_watchdog": "Paper watchdog has failed or stopped",
}
_WORKER_NAMES = (
    "paper_automation",
    "paper_exit_monitor",
    "paper_watchdog",
    "market_collector",
    "narrative_collector",
)


def _age_seconds(value: object, now: datetime) -> float | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return (now - parsed).total_seconds() if parsed.tzinfo else None


def _worker_failed(status: dict[str, object], now: datetime) -> bool:
    if status.get("last_error") or status.get("state") == "degraded":
        return True
    if status.get("running") is not True:
        return True
    interval = status.get("interval_seconds")
    maximum_gap = 2 * interval + 60 if type(interval) is int and interval > 0 else 660
    started_age = _age_seconds(status.get("last_started_at"), now)
    if status.get("last_completed_run_id") != status.get("run_id"):
        return started_age is None or started_age > maximum_gap
    completed_age = _age_seconds(status.get("last_completed_at"), now)
    return completed_age is None or not -30 <= completed_age <= maximum_gap


def collect_alert_issues(
    settings: Settings,
    journal: TradeJournal,
    workers: dict[str, dict[str, object]],
    *,
    now: datetime,
) -> tuple[set[str], set[str]]:
    """Derive bounded, non-sensitive issue codes from the existing paper gates."""
    campaign = journal.runtime_state("paper_campaign") or {}
    forward = campaign.get("mode") == "forward_paper_only"
    active = forward and campaign.get("status") in (None, "active")
    open_rows = journal._connection.execute(
        "SELECT opened_at, last_quoted_at FROM paper_positions WHERE status = 'open'"
    ).fetchall()
    paper_expected = bool(active)
    expected: set[str] = set()
    if settings.strategy_lab_collection_enabled:
        expected.add("market_collector")
    if settings.narrative_intelligence_enabled:
        expected.add("narrative_collector")
    if paper_expected:
        expected.update(("paper_exit_monitor", "paper_watchdog"))
    decision_state = workers.get("paper_automation", {}).get("state")
    if active and (
        settings.paper_automation_autostart or decision_state in {"running", "working", "degraded"}
    ):
        expected.add("paper_automation")

    issues: set[str] = set()
    if forward and campaign.get("status") == "interrupted":
        issues.add("paper_campaign_interrupted")
    maximum_mark_gap = 2 * settings.paper_exit_interval_seconds + 60
    if forward and any(
        (age := _age_seconds(quoted_at or opened_at, now)) is None
        or not -30 <= age <= maximum_mark_gap
        for opened_at, quoted_at in open_rows
    ):
        issues.add("position_mark_gap")
    if paper_expected:
        exit_status = workers.get("paper_exit_monitor", {})
        for code in paper_exit_monitor_issues(
            exit_status, settings.paper_exit_interval_seconds, now=now
        ):
            if code == "exit_monitor_unavailable":
                started_age = _age_seconds(exit_status.get("last_started_at"), now)
                if started_age is not None and 0 <= started_age <= maximum_mark_gap:
                    continue
            if code in _MESSAGES:
                issues.add(code)
    for name in expected:
        status = workers.get(name, {})
        if _worker_failed(status, now):
            if name == "paper_exit_monitor" and issues.intersection(
                {"exit_monitor_stale", "exit_monitor_unavailable"}
            ):
                continue
            category = "collector_failure" if name.endswith("collector") else "worker_crash"
            issues.add(f"{category}:{name}")
    market_result = workers.get("market_collector", {}).get("last_result")
    if (
        settings.strategy_lab_collection_enabled
        and workers.get("market_collector", {}).get("last_completed_run_id")
        == workers.get("market_collector", {}).get("run_id")
        and isinstance(market_result, dict)
    ):
        collection = market_result.get("market_collection")
        if isinstance(collection, dict):
            dex_attempted = collection.get("dex_requests_attempted")
            dex_succeeded = collection.get("dex_requests_succeeded")
            ohlcv_attempted = collection.get("ohlcv_requests_attempted")
            ohlcv_succeeded = collection.get("ohlcv_requests_succeeded")
            provider_errors = collection.get("provider_error_count")
            if (
                type(dex_attempted) is int
                and type(dex_succeeded) is int
                and dex_attempted > 0
                and 0 <= dex_succeeded < dex_attempted
            ) or (
                type(ohlcv_attempted) is int
                and type(ohlcv_succeeded) is int
                and type(provider_errors) is int
                and ohlcv_attempted > 0
                and ohlcv_succeeded == 0
                and provider_errors > 0
            ):
                issues.add("collector_failure:market_collector")
    decision_status = workers.get("paper_automation", {})
    decision_result = decision_status.get("last_result")
    if (
        active
        and decision_status.get("last_completed_run_id") == decision_status.get("run_id")
        and isinstance(decision_result, dict)
        and decision_result.get("entry_paused") is True
    ):
        issues.add("circuit_breaker")
    return issues, expected


def _send_message(client: httpx.Client, token: str, chat_id: str, message: str) -> bool:
    try:
        response = client.post(
            f"{TELEGRAM_ORIGIN}/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": f"MemeOps paper: {message}"},
            timeout=5.0,
        )
        if response.status_code != 200:
            LOGGER.warning("Telegram alert rejected (HTTP %d)", response.status_code)
            return False
        body = response.json()
        if not isinstance(body, dict) or body.get("ok") is not True:
            LOGGER.warning("Telegram alert rejected by provider")
            return False
        return True
    except Exception as exc:
        # httpx exceptions can contain the token-bearing request URL.
        LOGGER.warning("Telegram alert delivery failed (%s)", type(exc).__name__)
        return False


def _safe_state_map(value: object) -> dict[str, int]:
    return (
        {
            key: stamp
            for key, stamp in value.items()
            if isinstance(key, str) and type(stamp) is int and stamp >= 0
        }
        if isinstance(value, dict)
        else {}
    )


def _dispatch(
    settings: Settings,
    journal: TradeJournal,
    workers: dict[str, dict[str, object]],
    client: httpx.Client,
    now: datetime,
    startup: bool,
) -> dict[str, object]:
    raw = journal.runtime_state(STATE_KEY) or {}
    active = _safe_state_map(raw.get("active"))
    last_sent = _safe_state_map(raw.get("last_sent"))
    pending = _safe_state_map(raw.get("pending"))
    restart_sent = _safe_state_map(raw.get("restart_sent"))
    seen_raw = raw.get("seen_runs")
    seen_runs = (
        {
            name: run_id
            for name, run_id in seen_raw.items()
            if name in _WORKER_NAMES and isinstance(run_id, str)
        }
        if isinstance(seen_raw, dict)
        else {}
    )
    heartbeat_raw = raw.get("last_heartbeat")
    last_heartbeat = heartbeat_raw if type(heartbeat_raw) is int else 0
    startup_raw = raw.get("last_startup")
    last_startup = startup_raw if type(startup_raw) is int else 0
    current = int(now.timestamp())
    issues, expected = collect_alert_issues(settings, journal, workers, now=now)
    sent = 0

    def persist() -> None:
        journal.set_runtime_state(
            STATE_KEY,
            {
                "active": active,
                "last_sent": last_sent,
                "pending": pending,
                "restart_sent": restart_sent,
                "seen_runs": seen_runs,
                "last_heartbeat": last_heartbeat,
                "last_startup": last_startup,
            },
        )

    def deliver(claim: str, message: str, on_success: Callable[[], None]) -> bool:
        nonlocal sent
        if sent >= 12 or current - pending.get(claim, -COOLDOWN_SECONDS) < COOLDOWN_SECONDS:
            return False
        pending[claim] = current
        persist()  # Reserve before the external side effect; a crash cannot re-spam.
        delivered = _send_message(
            client, settings.telegram_bot_token or "", settings.telegram_chat_id or "", message
        )
        if delivered:
            on_success()
            sent += 1
        pending.pop(claim, None)
        persist()
        return delivered

    startup_done = not startup or current - last_startup < COOLDOWN_SECONDS
    if startup and not startup_done:

        def mark_startup() -> None:
            nonlocal last_startup
            last_startup = current

        startup_done = deliver("startup", "Paper alert monitor started", mark_startup)

    for name in sorted(expected):
        run_id = workers.get(name, {}).get("run_id")
        if not isinstance(run_id, str) or not run_id:
            continue
        previous = seen_runs.get(name)
        if previous is None:
            seen_runs[name] = run_id
        elif previous != run_id:
            if current - restart_sent.get(name, 0) < COOLDOWN_SECONDS:
                seen_runs[name] = run_id
            else:

                def mark_restart(name: str = name, run_id: str = run_id) -> None:
                    seen_runs[name] = run_id
                    restart_sent[name] = current

                deliver(
                    f"restart:{name}",
                    f"{name.replace('_', ' ')} restarted",
                    mark_restart,
                )

    for key in sorted(issues):
        if key not in _MESSAGES:
            continue
        if current - last_sent.get(key, 0) >= COOLDOWN_SECONDS:

            def mark_issue(key: str = key) -> None:
                active[key] = current
                last_sent[key] = current

            deliver(
                f"issue:{key}",
                f"ALERT: {_MESSAGES[key]}",
                mark_issue,
            )
    for key in sorted(set(active) - issues):
        if key in _MESSAGES:

            def mark_recovery(key: str = key) -> None:
                active.pop(key, None)

            deliver(
                f"recovery:{key}",
                f"RECOVERED: {_MESSAGES[key]}",
                mark_recovery,
            )
    if current - last_heartbeat >= HEARTBEAT_SECONDS:

        def mark_heartbeat() -> None:
            nonlocal last_heartbeat
            last_heartbeat = current

        deliver(
            "heartbeat",
            f"Daily heartbeat. Active health issues: {len(issues)}.",
            mark_heartbeat,
        )
    persist()
    return {
        "paused": False,
        "exits": [],
        "alerts_sent": sent,
        "active_issues": len(issues),
        "startup_done": startup_done,
    }


def run_alert_cycle(
    settings: Settings,
    journal: TradeJournal,
    workers: dict[str, dict[str, object]],
    *,
    client: httpx.Client | None = None,
    now: datetime | None = None,
    startup: bool = False,
) -> dict[str, object]:
    """Never raise into the exit monitor or dashboard lifecycle."""
    if not settings.alerts_enabled:
        return {"paused": False, "exits": [], "alerts": "disabled"}
    token = settings.telegram_bot_token or ""
    chat_id = settings.telegram_chat_id or ""
    if not _TOKEN_PATTERN.fullmatch(token) or not _CHAT_PATTERN.fullmatch(chat_id):
        LOGGER.warning("Telegram alerts are enabled but credentials are missing or invalid")
        return {"paused": False, "exits": [], "alerts": "misconfigured"}
    # httpx logs complete request URLs at INFO, including Telegram's path token.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    try:
        if client is not None:
            return _dispatch(settings, journal, workers, client, now or datetime.now(UTC), startup)
        with httpx.Client(follow_redirects=False, trust_env=False, timeout=5.0) as created:
            return _dispatch(settings, journal, workers, created, now or datetime.now(UTC), startup)
    except Exception as exc:
        LOGGER.warning("Telegram alert cycle failed (%s)", type(exc).__name__)
        return {"paused": False, "exits": [], "alerts": "failed"}


def run_persisted_alert_cycle(
    settings: Settings,
    *,
    client: httpx.Client | None = None,
    now: datetime | None = None,
    startup: bool = False,
) -> dict[str, object]:
    """Observe durable worker state from a separate process, including dashboard outages."""
    if not settings.alerts_enabled:
        return {"paused": False, "exits": [], "alerts": "disabled"}
    try:
        lock_path = settings.state_path.with_name(f"{settings.state_path.name}.alerts")
        with exclusive_worker_lock(lock_path), TradeJournal(settings.state_path) as journal:
            workers = {name: journal.runtime_state(name) or {} for name in _WORKER_NAMES}
            return run_alert_cycle(
                settings, journal, workers, client=client, now=now, startup=startup
            )
    except Exception as exc:
        LOGGER.warning("Paper alert monitor failed (%s)", type(exc).__name__)
        return {"paused": False, "exits": [], "alerts": "failed"}


def watch_alerts(settings: Settings, *, interval_seconds: int = 60) -> None:
    """Run an independently supervised, bounded paper-health monitor."""
    if not 30 <= interval_seconds <= 3_600:
        raise ValueError("Alert interval must be between 30 and 3600 seconds")
    if not settings.alerts_enabled:
        return
    startup = True
    while True:
        started = time.monotonic()
        result = run_persisted_alert_cycle(settings, startup=startup)
        startup = startup and result.get("startup_done") is not True
        time.sleep(max(1.0, interval_seconds - (time.monotonic() - started)))
