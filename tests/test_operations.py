from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from memecoin_bot.journal import TradeJournal
from memecoin_bot.operations import StateBackupManager, operational_health


def test_backup_manager_creates_verified_online_backup(tmp_path: Path) -> None:
    state = tmp_path / "state.sqlite3"
    with TradeJournal(state) as journal:
        journal.set_runtime_state("test", {"value": 7})

    manager = StateBackupManager(
        state_path=state,
        backup_directory=tmp_path / "backups",
        interval_seconds=300,
        enabled=True,
    )
    result = manager.run_if_due(force=True)

    assert result["created"] is True
    backup_path = Path(str(result["last_backup_path"]))
    assert backup_path.is_file()
    assert len(str(result["last_backup_sha256"])) == 64
    with TradeJournal(backup_path) as backup:
        assert backup.quick_check() is True
        assert backup.runtime_state("test")["value"] == 7  # type: ignore[index]


def test_operational_health_reports_degraded_worker(tmp_path: Path) -> None:
    state = tmp_path / "state.sqlite3"
    with TradeJournal(state):
        pass
    health = operational_health(
        state_path=state,
        workers={"paper": {"state": "degraded", "last_error": "provider timeout"}},
        backup={"last_error": None},
    )
    assert health["state"] == "degraded"
    assert health["database"]["quick_check"] == "ok"
    assert health["degraded_workers"] == ["paper"]


def test_operational_health_flags_unquotable_paper_exits(tmp_path: Path) -> None:
    state = tmp_path / "state.sqlite3"
    with TradeJournal(state):
        pass
    health = operational_health(
        state_path=state,
        workers={
            "paper": {
                "state": "running",
                "last_error": None,
                "last_result": {"quote_failures": 2},
            }
        },
        backup={"last_error": None},
    )
    assert health["state"] == "degraded"
    assert health["degraded_workers"] == ["paper"]


def test_operational_health_flags_failed_paper_exit_evaluation(tmp_path: Path) -> None:
    state = tmp_path / "state.sqlite3"
    with TradeJournal(state):
        pass
    health = operational_health(
        state_path=state,
        workers={
            "paper": {
                "state": "running",
                "last_error": None,
                "last_result": {"quote_failures": 0, "exit_failures": 1},
            }
        },
        backup={"last_error": None},
    )
    assert health["state"] == "degraded"
    assert health["degraded_workers"] == ["paper"]


@pytest.mark.parametrize(
    ("exit_status", "reason"),
    [
        ({}, "exit_monitor_unavailable"),
        (
            {
                "state": "running",
                "running": True,
                "interval_seconds": 120,
                "run_id": "current-run",
                "last_completed_run_id": "current-run",
                "last_completed_at": "2000-01-01T00:00:00+00:00",
                "last_result": {"quote_failures": 0, "exit_failures": 0},
            },
            "exit_monitor_stale",
        ),
    ],
)
def test_operational_health_flags_missing_or_stale_exit_monitor(
    tmp_path: Path, exit_status: dict[str, object], reason: str
) -> None:
    state = tmp_path / "state.sqlite3"
    with TradeJournal(state):
        pass
    health = operational_health(
        state_path=state,
        workers={
            "paper_automation": {"state": "running", "running": True},
            "paper_exit_monitor": exit_status,
        },
        backup={"last_error": None},
    )
    assert health["state"] == "degraded"
    assert reason in health["degradation_reasons"]["paper_exit_monitor"]


def test_operational_health_accepts_fresh_exit_monitor(tmp_path: Path) -> None:
    state = tmp_path / "state.sqlite3"
    with TradeJournal(state):
        pass
    health = operational_health(
        state_path=state,
        workers={
            "paper_automation": {"state": "running", "running": True},
            "paper_exit_monitor": {
                "state": "running",
                "running": True,
                "interval_seconds": 120,
                "run_id": "current-run",
                "last_completed_run_id": "current-run",
                "last_completed_at": (datetime.now(UTC) - timedelta(seconds=100)).isoformat(),
                "last_result": {"quote_failures": 0, "exit_failures": 0},
            },
        },
        backup={"last_error": None},
    )
    assert health["state"] == "healthy"
    assert health["degradation_reasons"] == {}


@pytest.mark.parametrize(
    ("watchdog_state", "expected_issue"),
    [
        ({"state": "stopped", "running": False}, "paper_watchdog_unavailable"),
        (
            {
                "state": "running",
                "running": True,
                "run_id": "current-run",
                "last_completed_run_id": "current-run",
                "last_completed_at": "2000-01-01T00:00:00+00:00",
                "interval_seconds": 30,
            },
            "paper_watchdog_stale",
        ),
    ],
)
def test_operational_health_flags_unavailable_watchdog(
    tmp_path: Path, watchdog_state: dict[str, object], expected_issue: str
) -> None:
    state = tmp_path / "state.sqlite3"
    with TradeJournal(state):
        pass
    health = operational_health(
        state_path=state,
        workers={
            "paper_automation": {"state": "running", "running": True},
            "paper_exit_monitor": {
                "state": "running",
                "running": True,
                "interval_seconds": 120,
                "run_id": "current-run",
                "last_completed_run_id": "current-run",
                "last_completed_at": datetime.now(UTC).isoformat(),
                "last_result": {"quote_failures": 0, "exit_failures": 0},
            },
            "paper_watchdog": watchdog_state,
        },
        backup={"last_error": None},
    )
    assert health["state"] == "degraded"
    assert expected_issue in health["degradation_reasons"]["paper_watchdog"]


def test_operational_health_rejects_prior_run_exit_check(tmp_path: Path) -> None:
    state = tmp_path / "state.sqlite3"
    with TradeJournal(state):
        pass
    health = operational_health(
        state_path=state,
        workers={
            "paper_automation": {"state": "running", "running": True},
            "paper_exit_monitor": {
                "state": "running",
                "running": True,
                "interval_seconds": 120,
                "run_id": "new-run",
                "last_completed_run_id": "previous-run",
                "last_completed_at": datetime.now(UTC).isoformat(),
                "last_result": {"quote_failures": 0, "exit_failures": 0},
            },
        },
        backup={"last_error": None},
    )
    assert "exit_monitor_unavailable" in health["degradation_reasons"]["paper_exit_monitor"]


@pytest.mark.parametrize(
    ("collection", "reason"),
    [
        ({"rate_limited": True}, "ohlcv_rate_limited"),
        (
            {"dex_requests_attempted": 2, "dex_requests_succeeded": 1},
            "dex_requests_failed",
        ),
        (
            {
                "ohlcv_requests_attempted": 3,
                "ohlcv_requests_succeeded": 0,
                "provider_error_count": 3,
            },
            "ohlcv_requests_failed",
        ),
    ],
)
def test_operational_health_flags_incomplete_market_collection(
    tmp_path: Path, collection: dict[str, object], reason: str
) -> None:
    state = tmp_path / "state.sqlite3"
    with TradeJournal(state):
        pass
    health = operational_health(
        state_path=state,
        workers={
            "market_collector": {
                "state": "running",
                "last_error": None,
                "last_result": {"market_collection": collection},
            }
        },
        backup={"last_error": None},
    )
    assert health["state"] == "degraded"
    assert health["degraded_workers"] == ["market_collector"]
    assert reason in health["degradation_reasons"]["market_collector"]


def test_operational_health_accepts_complete_market_collection(tmp_path: Path) -> None:
    state = tmp_path / "state.sqlite3"
    with TradeJournal(state):
        pass
    health = operational_health(
        state_path=state,
        workers={
            "market_collector": {
                "state": "running",
                "last_error": None,
                "last_result": {
                    "market_collection": {
                        "rate_limited": False,
                        "dex_requests_attempted": 2,
                        "dex_requests_succeeded": 2,
                        "ohlcv_requests_attempted": 3,
                        "ohlcv_requests_succeeded": 2,
                        "provider_error_count": 0,
                    }
                },
            }
        },
        backup={"last_error": None},
    )
    assert health["state"] == "healthy"
    assert health["degraded_workers"] == []
    assert health["degradation_reasons"] == {}


@pytest.mark.parametrize(("market_ready", "degraded"), [(16, True), (4, False)])
def test_operational_health_compares_candle_capacity_with_current_demand(
    tmp_path: Path, market_ready: int, degraded: bool
) -> None:
    state = tmp_path / "state.sqlite3"
    now = int(datetime.now(UTC).timestamp())
    with TradeJournal(state) as journal:
        journal.set_runtime_state(
            "ohlcv_request_budget",
            {
                "recent_cycles": [
                    {
                        "started_at": now - 180,
                        "at": now - 150,
                        "attempted": 4,
                        "succeeded": 4,
                        "cache_hits": 0,
                        "rate_limited": False,
                        "market_ready": market_ready,
                    },
                    {
                        "started_at": now - 30,
                        "at": now,
                        "attempted": 4,
                        "succeeded": 4,
                        "cache_hits": 0,
                        "rate_limited": False,
                        "market_ready": market_ready,
                    },
                ]
            },
        )
    health = operational_health(
        state_path=state,
        workers={
            "market_collector": {
                "state": "running",
                "last_error": None,
                "last_result": {"market_collection": {"rate_limited": False}},
            }
        },
        backup={"last_error": None},
    )
    assert health["ohlcv_capacity"]["observed_successful_requests_per_minute"] == 2.67
    assert (
        "ohlcv_capacity_below_demand" in health["degradation_reasons"].get("market_collector", [])
    ) is degraded
    assert (health["state"] == "degraded") is degraded
