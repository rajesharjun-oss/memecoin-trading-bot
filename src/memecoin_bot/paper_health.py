"""Read-only health signal for one unattended paper worker."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime

from .alerts import _WORKER_NAMES, _worker_failed, collect_alert_issues
from .config import Settings
from .journal import TradeJournal
from .operations import paper_exit_monitor_issues


def paper_worker_health(
    settings: Settings, *, now: datetime | None = None
) -> tuple[bool, tuple[str, ...]]:
    """Use the existing worker freshness gates without modifying the journal."""
    if settings.live_trading_enabled:
        return False, ("live_mode_configured",)
    if not settings.paper_automation_autostart:
        return False, ("paper_automation_not_configured",)
    current = now or datetime.now(UTC)
    try:
        with TradeJournal(settings.state_path, readonly=True) as journal:
            workers = {name: journal.runtime_state(name) or {} for name in _WORKER_NAMES}
            issues, _ = collect_alert_issues(settings, journal, workers, now=current)
            exit_status = workers["paper_exit_monitor"]
            issues.update(
                paper_exit_monitor_issues(
                    exit_status, settings.paper_exit_interval_seconds, now=current
                )
            )
            if _worker_failed(workers["paper_automation"], current):
                issues.add("worker_crash:paper_automation")
    except (OSError, sqlite3.Error):
        return False, ("journal_unavailable",)
    except Exception:
        return False, ("healthcheck_failed",)
    return not issues, tuple(sorted(issues))
