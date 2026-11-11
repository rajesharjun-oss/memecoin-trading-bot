from __future__ import annotations

import json
import os
import time
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest

from memecoin_bot import alerts, paper_health
from memecoin_bot.alerts import run_alert_cycle
from memecoin_bot.cli import build_parser
from memecoin_bot.config import ConfigurationError, Settings
from memecoin_bot.journal import TradeJournal
from memecoin_bot.paper_health import paper_worker_health
from memecoin_bot.paper_identity import ensure_paper_identity

TOKEN = "123456:abcdefghijklmnopqrstuvwxyz0123456789"  # noqa: S105 - synthetic test token


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        rpc_url="https://rpc.example.test",
        keypair_path=tmp_path / "paper-keypair.json",
        jupiter_api_key=None,
        live_trading_enabled=False,
        max_trade_lamports=50_000_000,
        max_daily_lamports=100_000_000,
        max_slippage_bps=300,
        max_price_impact_bps=500,
        max_top10_concentration_bps=7_000,
        max_priority_fee_lamports=1_000_000,
        confirmation_timeout_seconds=45,
        allow_token_2022=False,
        state_path=tmp_path / "paper.sqlite3",
        paper_automation_autostart=True,
        alerts_enabled=True,
        telegram_bot_token=TOKEN,
        telegram_chat_id="1234567890",
    )


def _worker_status(now: datetime, *, interval: int = 30) -> dict[str, object]:
    return {
        "state": "running",
        "running": True,
        "run_id": "current-run",
        "last_completed_run_id": "current-run",
        "last_started_at": now.isoformat(),
        "last_completed_at": now.isoformat(),
        "interval_seconds": interval,
        "last_error": None,
        "last_result": {"exit_failures": 0, "quote_failures": 0},
    }


def _journal_with_healthy_workers(settings: Settings, now: datetime) -> None:
    with TradeJournal(settings.state_path) as journal:
        journal.set_runtime_state("paper_automation", _worker_status(now, interval=300))
        journal.set_runtime_state("paper_exit_monitor", _worker_status(now, interval=120))
        journal.set_runtime_state("market_collector", _worker_status(now))


def test_healthcheck_fails_closed_for_stale_exit_and_collector(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    now = datetime(2026, 11, 11, tzinfo=UTC)
    assert paper_worker_health(settings, now=now) == (False, ("journal_unavailable",))
    _journal_with_healthy_workers(settings, now)
    assert paper_worker_health(settings, now=now) == (True, ())

    with TradeJournal(settings.state_path) as journal:
        journal.set_runtime_state(
            "paper_exit_monitor", _worker_status(now - timedelta(seconds=301), interval=120)
        )
    healthy, issues = paper_worker_health(settings, now=now)
    assert not healthy
    assert "exit_monitor_stale" in issues

    with TradeJournal(settings.state_path) as journal:
        journal.set_runtime_state("paper_exit_monitor", _worker_status(now, interval=120))
        journal.set_runtime_state("market_collector", _worker_status(now - timedelta(seconds=121)))
    healthy, issues = paper_worker_health(settings, now=now)
    assert not healthy
    assert "collector_failure:market_collector" in issues


def test_healthcheck_requires_paper_autostart(tmp_path: Path) -> None:
    settings = replace(_settings(tmp_path), paper_automation_autostart=False)
    assert paper_worker_health(settings)[1] == ("paper_automation_not_configured",)
    assert build_parser().parse_args(["paper-health"]).command == "paper-health"


def test_healthcheck_fails_closed_on_unexpected_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail(
        _settings: Settings, _journal: TradeJournal, _workers: object, *, now: datetime
    ) -> None:
        raise RuntimeError("unexpected test failure")

    settings = _settings(tmp_path)
    _journal_with_healthy_workers(settings, datetime(2026, 11, 11, tzinfo=UTC))
    monkeypatch.setattr(paper_health, "collect_alert_issues", fail)
    assert paper_worker_health(settings)[1] == ("healthcheck_failed",)


def test_paper_identity_is_unfunded_local_and_never_overwritten(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    assert ensure_paper_identity(settings)
    assert settings.keypair_path is not None
    before = settings.keypair_path.stat()
    assert before.st_size > 0
    if os.name != "nt":
        assert before.st_mode & 0o077 == 0
    assert not ensure_paper_identity(settings)
    assert settings.keypair_path.stat().st_mtime_ns == before.st_mtime_ns
    with pytest.raises(ConfigurationError, match="beside the state journal"):
        ensure_paper_identity(replace(settings, keypair_path=tmp_path.parent / "outside.json"))
    with pytest.raises(ConfigurationError, match="live trading"):
        ensure_paper_identity(replace(settings, live_trading_enabled=True))
    assert build_parser().parse_args(["init-paper-identity"]).command == "init-paper-identity"


def test_alert_monitor_startup_and_heartbeat_persist_without_restart_spam(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    now = datetime(2026, 11, 11, tzinfo=UTC)
    messages: list[str] = []

    def send(request: httpx.Request) -> httpx.Response:
        messages.append(json.loads(request.content)["text"])
        return httpx.Response(200, json={"ok": True})

    with httpx.Client(transport=httpx.MockTransport(send)) as client:
        workers = {"market_collector": _worker_status(now)}
        with TradeJournal(settings.state_path) as journal:
            first = run_alert_cycle(
                settings, journal, workers, client=client, now=now, startup=True
            )
        with TradeJournal(settings.state_path) as journal:
            second = run_alert_cycle(
                settings,
                journal,
                workers,
                client=client,
                now=now + timedelta(seconds=30),
                startup=True,
            )
        assert first["startup_done"] is True
        assert second["startup_done"] is True
        assert sum("monitor started" in message for message in messages) == 1
        assert sum("Daily heartbeat" in message for message in messages) == 1
        with TradeJournal(settings.state_path) as journal:
            run_alert_cycle(
                settings,
                journal,
                workers,
                client=client,
                now=now + timedelta(days=1),
                startup=False,
            )
        assert sum("Daily heartbeat" in message for message in messages) == 2


def test_watcher_retries_failed_startup_then_stops(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _settings(tmp_path)
    observed: list[bool] = []

    def cycle(_settings: Settings, *, startup: bool = False) -> dict[str, object]:
        observed.append(startup)
        if len(observed) == 3:
            raise InterruptedError
        return {"startup_done": len(observed) == 2}

    def stop(_seconds: float) -> None:
        return None

    monkeypatch.setattr(alerts, "run_persisted_alert_cycle", cycle)
    monkeypatch.setattr(time, "sleep", stop)
    with pytest.raises(InterruptedError):
        alerts.watch_alerts(settings, interval_seconds=30)
    assert observed == [True, True, False]
