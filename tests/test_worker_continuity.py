from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

from memecoin_bot.automation import PaperAutomationWorker
from memecoin_bot.journal import TradeJournal
from memecoin_bot.operations import summarize_paper_continuity


def _stamp(start: datetime, seconds: int) -> str:
    return (start + timedelta(seconds=seconds)).isoformat()


def test_worker_run_id_changes_on_restart() -> None:
    worker = PaperAutomationWorker(
        interval_seconds=30,
        cycle=lambda: {"paused": False, "scan": {}, "decision": {}, "exits": []},
    )
    first = worker.start()["run_id"]
    worker.stop()
    second = worker.start()["run_id"]
    worker.stop()
    assert isinstance(first, str) and isinstance(second, str) and first != second


def test_worker_history_only_records_new_completions(tmp_path: Path) -> None:
    start = datetime(2026, 11, 3, tzinfo=UTC)
    with TradeJournal(tmp_path / "state.sqlite3") as journal:
        old: dict[str, object] = {
            "state": "stopped",
            "last_started_at": _stamp(start, 0),
            "last_completed_at": _stamp(start, 10),
        }
        journal.set_runtime_state("paper_automation", old)
        journal.save_worker_state("paper_automation", old)
        assert journal.worker_cycles_since("paper_automation", _stamp(start, 0)) == []

        begun: dict[str, object] = {**old, "state": "working", "last_started_at": _stamp(start, 60)}
        journal.save_worker_state("paper_automation", begun)
        assert journal.worker_cycles_since("paper_automation", _stamp(start, 0)) == []

        finished: dict[str, object] = {
            **begun,
            "state": "running",
            "last_completed_at": _stamp(start, 70),
            "last_result": {"rpc_request_counts": {"getTransaction": 3}},
        }
        journal.save_worker_state("paper_automation", finished)
        journal.save_worker_state("paper_automation", finished)
        assert journal.worker_cycles_since("paper_automation", _stamp(start, 0)) == [
            {
                "started_at": _stamp(start, 60),
                "completed_at": _stamp(start, 70),
                "outcome": "completed",
                "run_id": None,
            }
        ]
        assert (
            journal._connection.execute(
                "SELECT rpc_requests_json FROM worker_cycle_history WHERE worker_key=?",
                ("paper_automation",),
            ).fetchone()[0]
            == '{"getTransaction": 3}'
        )


def test_continuity_resets_on_gap_and_error(tmp_path: Path) -> None:
    start = datetime(2026, 11, 3, tzinfo=UTC)
    worker_names = ("paper_automation", "paper_exit_monitor", "market_collector")
    with TradeJournal(tmp_path / "state.sqlite3") as journal:
        journal.set_runtime_state(
            "paper_campaign", {"mode": "forward_paper_only", "started_at": start.isoformat()}
        )
        for name in worker_names:
            for seconds in (10, 310):
                journal.save_worker_state(
                    name,
                    {
                        "state": "running",
                        "last_started_at": _stamp(start, seconds - 2),
                        "last_completed_at": _stamp(start, seconds),
                    },
                )
        statuses: dict[str, dict[str, object]] = {
            name: {"running": True, "interval_seconds": 300, "last_error": None}
            for name in worker_names
        }
        first = summarize_paper_continuity(journal, statuses, now=start + timedelta(seconds=320))
        assert first["state"] == "current"
        assert first["current_streak_seconds"] == 310

        journal.save_worker_state(
            "market_collector",
            {
                "state": "running",
                "last_started_at": _stamp(start, 1008),
                "last_completed_at": _stamp(start, 1010),
            },
        )
        gap = summarize_paper_continuity(journal, statuses, now=start + timedelta(seconds=1020))
        assert gap["state"] == "unverified"  # Other worker heartbeats are stale.
        gap_workers = cast(dict[str, Any], gap["workers"])
        assert gap_workers["market_collector"]["heartbeat_gaps"] == 1
        assert gap_workers["market_collector"]["current_streak_seconds"] == 10

        journal.save_worker_state(
            "market_collector",
            {
                "state": "degraded",
                "last_started_at": _stamp(start, 1020),
                "last_completed_at": _stamp(start, 1022),
                "last_error": "ProviderTimeout: redacted",
            },
        )
        failed = summarize_paper_continuity(journal, statuses, now=start + timedelta(seconds=1025))
        failed_workers = cast(dict[str, Any], failed["workers"])
        assert failed_workers["market_collector"]["error_cycles"] == 1
        assert failed_workers["market_collector"]["current_streak_seconds"] == 0


def test_continuity_resets_after_quick_worker_restart(tmp_path: Path) -> None:
    start = datetime(2026, 11, 3, tzinfo=UTC)
    names = ("paper_automation", "paper_exit_monitor", "market_collector")
    with TradeJournal(tmp_path / "state.sqlite3") as journal:
        journal.set_runtime_state("paper_campaign", {"started_at": start.isoformat()})
        for name in names:
            for seconds, run_id in ((10, "first-run"), (310, "second-run")):
                journal.save_worker_state(
                    name,
                    {
                        "state": "running",
                        "last_started_at": _stamp(start, seconds - 2),
                        "last_completed_at": _stamp(start, seconds),
                        "run_id": run_id,
                    },
                )
        statuses: dict[str, dict[str, object]] = {
            name: {
                "running": True,
                "interval_seconds": 300,
                "last_error": None,
                "run_id": "second-run",
            }
            for name in names
        }
        summary = summarize_paper_continuity(journal, statuses, now=start + timedelta(seconds=320))
        assert summary["state"] == "current"
        assert summary["current_streak_seconds"] == 10
        assert all(
            row["worker_restarts"] == 1 for row in cast(dict[str, Any], summary["workers"]).values()
        )

        statuses["market_collector"]["run_id"] = "third-run"
        assert (
            summarize_paper_continuity(journal, statuses, now=start + timedelta(seconds=320))[
                "state"
            ]
            == "unverified"
        )


def test_continuity_read_stays_bounded_and_can_prove_thirty_days(tmp_path: Path) -> None:
    start = datetime(2026, 9, 1, tzinfo=UTC)
    names = ("paper_automation", "paper_exit_monitor", "market_collector")
    with TradeJournal(tmp_path / "state.sqlite3") as journal:
        journal.set_runtime_state("paper_campaign", {"started_at": start.isoformat()})
        for name in names:
            for day in range(46):
                completed = day * 86_400
                journal.save_worker_state(
                    name,
                    {
                        "state": "running",
                        "last_started_at": _stamp(start, completed - 2),
                        "last_completed_at": _stamp(start, completed),
                        "run_id": "one-uninterrupted-run",
                    },
                )
        statuses: dict[str, dict[str, object]] = {
            name: {
                "running": True,
                "interval_seconds": 86_400,
                "last_error": None,
                "run_id": "one-uninterrupted-run",
            }
            for name in names
        }
        summary = summarize_paper_continuity(
            journal, statuses, now=start + timedelta(days=45, seconds=10)
        )
        assert summary["state"] == "current"
        assert summary["history_window_started_at"] == _stamp(start, 14 * 86_400 + 10)
        assert cast(int, summary["current_streak_seconds"]) >= 30 * 86_400
        assert all(
            row["recorded_cycles"] == 31
            for row in cast(dict[str, Any], summary["workers"]).values()
        )
