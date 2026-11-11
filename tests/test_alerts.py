from __future__ import annotations

import json
import logging
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from memecoin_bot import cli
from memecoin_bot.alerts import (
    collect_alert_issues,
    run_alert_cycle,
    run_persisted_alert_cycle,
)
from memecoin_bot.cli import main
from memecoin_bot.config import Settings
from memecoin_bot.dashboard import create_dashboard_app
from memecoin_bot.journal import TradeJournal
from memecoin_bot.worker_lock import exclusive_worker_lock

TOKEN = "123456:abcdefghijklmnopqrstuvwxyz0123456789"  # noqa: S105 - synthetic test token
CHAT_ID = "-1001234567890"


def _settings(tmp_path: Path, *, enabled: bool = True) -> Settings:
    return Settings(
        rpc_url="https://rpc.example.test",
        keypair_path=None,
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
        state_path=tmp_path / "alerts.sqlite3",
        alerts_enabled=enabled,
        telegram_bot_token=TOKEN,
        telegram_chat_id=CHAT_ID,
    )


def _status(now: datetime, *, run_id: str = "run-a", error: str | None = None) -> dict[str, object]:
    return {
        "state": "degraded" if error else "running",
        "running": True,
        "run_id": run_id,
        "last_completed_run_id": run_id,
        "last_started_at": now.isoformat(),
        "last_completed_at": now.isoformat(),
        "last_error": error,
        "last_result": {},
        "interval_seconds": 60,
    }


def _client(messages: list[str]) -> httpx.Client:
    def handle(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "api.telegram.org"
        assert request.method == "POST"
        payload = json.loads(request.content)
        assert payload["chat_id"] == CHAT_ID
        messages.append(payload["text"])
        return httpx.Response(200, json={"ok": True, "result": {}})

    return httpx.Client(transport=httpx.MockTransport(handle))


def test_alert_config_defaults_off_and_redacts_credentials(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("ALERTS_ENABLED", raising=False)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", TOKEN)
    monkeypatch.setenv("TELEGRAM_CHAT_ID", CHAT_ID)
    settings = Settings.from_env()
    assert settings.alerts_enabled is False
    assert settings.telegram_bot_token == TOKEN
    assert settings.telegram_chat_id == CHAT_ID
    assert TOKEN not in repr(settings)
    assert CHAT_ID not in repr(settings)
    monkeypatch.setenv("ALERTS_ENABLED", "true")
    assert Settings.from_env().alerts_enabled is True


def test_alerts_are_disabled_by_default_and_make_no_request(tmp_path: Path) -> None:
    settings = _settings(tmp_path, enabled=False)
    messages: list[str] = []
    with TradeJournal(settings.state_path) as journal, _client(messages) as client:
        result = run_alert_cycle(settings, journal, {}, client=client)
        assert result["alerts"] == "disabled"
        assert journal.runtime_state("telegram_alerts_v1") is None
    assert messages == []


def test_alerts_cli_once_is_disabled_by_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("ALERTS_ENABLED", "false")
    outputs: list[object] = []
    monkeypatch.setattr(cli, "_json", lambda value: outputs.append(value))
    assert main(["alerts", "--once"]) == 0
    assert outputs == [{"paused": False, "exits": [], "alerts": "disabled"}]


def test_dashboard_api_does_not_expose_alert_credentials(tmp_path: Path) -> None:
    settings = replace(
        _settings(tmp_path),
        strategy_lab_collection_enabled=False,
        narrative_intelligence_enabled=False,
    )
    app = create_dashboard_app(settings, "test-pairing-code")
    with TestClient(app, base_url="http://127.0.0.1") as client:
        assert client.post("/api/auth/pair", json={"code": "test-pairing-code"}).status_code == 200
        for path in ("/api/operations/health", "/api/automation/status"):
            response = client.get(path)
            assert response.status_code == 200
            assert TOKEN not in response.text
            assert CHAT_ID not in response.text


def test_issue_cooldown_recovery_and_daily_heartbeat_survive_restart(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    t0 = datetime(2026, 11, 10, tzinfo=UTC)
    messages: list[str] = []
    with TradeJournal(settings.state_path) as journal, _client(messages) as client:
        failed = {"market_collector": _status(t0, error="provider failed")}
        assert run_alert_cycle(settings, journal, failed, client=client, now=t0)["alerts_sent"] == 2
        assert (
            run_alert_cycle(
                settings, journal, failed, client=client, now=t0 + timedelta(minutes=5)
            )["alerts_sent"]
            == 0
        )
        assert (
            run_alert_cycle(
                settings, journal, failed, client=client, now=t0 + timedelta(minutes=31)
            )["alerts_sent"]
            == 1
        )
        healthy = {"market_collector": _status(t0 + timedelta(minutes=32))}
        assert (
            run_alert_cycle(
                settings, journal, healthy, client=client, now=t0 + timedelta(minutes=32)
            )["alerts_sent"]
            == 1
        )
        assert (
            run_alert_cycle(
                settings, journal, failed, client=client, now=t0 + timedelta(minutes=33)
            )["alerts_sent"]
            == 0
        )
    with TradeJournal(settings.state_path) as journal, _client(messages) as client:
        healthy = {"market_collector": _status(t0 + timedelta(minutes=33))}
        assert (
            run_alert_cycle(
                settings, journal, healthy, client=client, now=t0 + timedelta(minutes=33)
            )["alerts_sent"]
            == 0
        )
        tomorrow = t0 + timedelta(days=1, minutes=1)
        assert (
            run_alert_cycle(
                settings,
                journal,
                {"market_collector": _status(tomorrow)},
                client=client,
                now=tomorrow,
            )["alerts_sent"]
            == 1
        )
    assert sum("ALERT: Market collector" in message for message in messages) == 2
    assert sum("RECOVERED: Market collector" in message for message in messages) == 1
    assert sum("Daily heartbeat" in message for message in messages) == 2


def test_pending_delivery_claim_prevents_restart_respam(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    t0 = datetime(2026, 11, 10, tzinfo=UTC)
    messages: list[str] = []
    failed = {"market_collector": _status(t0, error="provider failed")}
    with TradeJournal(settings.state_path) as journal:
        journal.set_runtime_state(
            "telegram_alerts_v1",
            {"pending": {"issue:collector_failure:market_collector": int(t0.timestamp())}},
        )
    with TradeJournal(settings.state_path) as journal, _client(messages) as client:
        assert (
            run_alert_cycle(
                settings, journal, failed, client=client, now=t0 + timedelta(minutes=1)
            )["alerts_sent"]
            == 1
        )  # Only the heartbeat is due.
        assert (
            run_alert_cycle(
                settings, journal, failed, client=client, now=t0 + timedelta(minutes=31)
            )["alerts_sent"]
            == 1
        )
    assert sum("ALERT: Market collector" in message for message in messages) == 1


def test_provider_failure_is_isolated_and_secrets_are_not_logged(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    settings = _settings(tmp_path)
    now = datetime(2026, 11, 10, tzinfo=UTC)
    caplog.set_level(logging.DEBUG, logger="httpx")
    caplog.set_level(logging.DEBUG, logger="httpcore")

    def fail(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(f"failed for {request.url}", request=request)

    with (
        TradeJournal(settings.state_path) as journal,
        httpx.Client(transport=httpx.MockTransport(fail)) as client,
    ):
        result = run_alert_cycle(
            settings,
            journal,
            {"market_collector": _status(now, error="provider failed")},
            client=client,
            now=now,
        )
        assert result["alerts_sent"] == 0
        persisted = journal.runtime_state("telegram_alerts_v1")
        assert persisted is not None
        assert TOKEN not in str(persisted)
        assert CHAT_ID not in str(persisted)
    assert TOKEN not in caplog.text
    assert CHAT_ID not in caplog.text
    assert logging.getLogger("httpx").getEffectiveLevel() >= logging.WARNING
    assert logging.getLogger("httpcore").getEffectiveLevel() >= logging.WARNING
    assert TOKEN not in repr(settings)
    assert CHAT_ID not in repr(settings)

    messages: list[str] = []
    with TradeJournal(settings.state_path) as journal, _client(messages) as client:
        assert (
            run_alert_cycle(
                settings,
                journal,
                {"market_collector": _status(now + timedelta(minutes=1), error="provider failed")},
                client=client,
                now=now + timedelta(minutes=1),
            )["alerts_sent"]
            == 2
        )


def test_invalid_credentials_do_not_stop_paper_workers(tmp_path: Path) -> None:
    settings = replace(_settings(tmp_path), telegram_bot_token="invalid-token")  # noqa: S106
    messages: list[str] = []
    with TradeJournal(settings.state_path) as journal, _client(messages) as client:
        assert run_alert_cycle(settings, journal, {}, client=client)["alerts"] == "misconfigured"
    assert messages == []


def test_detects_position_mark_gap_circuit_breaker_and_worker_crash(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    now = datetime.now(UTC)
    with TradeJournal(settings.state_path) as journal:
        journal.set_runtime_state(
            "paper_campaign", {"mode": "forward_paper_only", "status": "active"}
        )
        journal.open_paper_position(
            position_id="paper-position",
            mint="test-mint",
            entry_lamports=100,
            token_amount=1000,
            simulated_units=1000,
            starting_lamports=1000,
        )
        workers = {
            "paper_automation": {
                **_status(now),
                "last_result": {"entry_paused": True, "pause_reason": "daily_loss_limit"},
            },
            "paper_exit_monitor": _status(now),
            "paper_watchdog": _status(now, error="stopped"),
            "market_collector": _status(now),
        }
        later = now + timedelta(minutes=10)
        issues, _ = collect_alert_issues(settings, journal, workers, now=later)
    assert "position_mark_gap" in issues
    assert "exit_monitor_stale" in issues
    assert "circuit_breaker" in issues
    assert "worker_crash:paper_watchdog" in issues


def test_active_campaign_requires_exit_monitor_without_open_positions(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    now = datetime(2026, 11, 10, tzinfo=UTC)
    with TradeJournal(settings.state_path) as journal:
        journal.set_runtime_state(
            "paper_campaign", {"mode": "forward_paper_only", "status": "active"}
        )
        issues, expected = collect_alert_issues(settings, journal, {}, now=now)
    assert "paper_exit_monitor" in expected
    assert "exit_monitor_unavailable" in issues


def test_stale_exit_monitor_produces_one_issue_not_duplicate_crash(tmp_path: Path) -> None:
    settings = replace(
        _settings(tmp_path),
        strategy_lab_collection_enabled=False,
        narrative_intelligence_enabled=False,
    )
    now = datetime(2026, 11, 10, tzinfo=UTC)
    with TradeJournal(settings.state_path) as journal:
        journal.set_runtime_state(
            "paper_campaign", {"mode": "forward_paper_only", "status": "active"}
        )
        workers = {
            "paper_exit_monitor": _status(now - timedelta(minutes=10)),
            "paper_watchdog": _status(now),
        }
        issues, _ = collect_alert_issues(settings, journal, workers, now=now)
    assert issues == {"exit_monitor_stale"}


def test_worker_restart_is_alerted_once(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    now = datetime(2026, 11, 10, tzinfo=UTC)
    messages: list[str] = []
    with TradeJournal(settings.state_path) as journal, _client(messages) as client:
        run_alert_cycle(
            settings, journal, {"market_collector": _status(now)}, client=client, now=now
        )
        assert (
            run_alert_cycle(
                settings,
                journal,
                {"market_collector": _status(now + timedelta(minutes=1), run_id="run-b")},
                client=client,
                now=now + timedelta(minutes=1),
            )["alerts_sent"]
            == 1
        )
        assert (
            run_alert_cycle(
                settings,
                journal,
                {"market_collector": _status(now + timedelta(minutes=2), run_id="run-b")},
                client=client,
                now=now + timedelta(minutes=2),
            )["alerts_sent"]
            == 0
        )
    assert sum("market collector restarted" in message for message in messages) == 1


def test_separate_alert_process_detects_persisted_stale_collector(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    now = datetime(2026, 11, 10, tzinfo=UTC)
    with TradeJournal(settings.state_path) as journal:
        journal.set_runtime_state("market_collector", _status(now - timedelta(minutes=10)))
    messages: list[str] = []
    with _client(messages) as client:
        result = run_persisted_alert_cycle(settings, client=client, now=now)
    assert result["alerts_sent"] == 2
    assert any("ALERT: Market collector" in message for message in messages)


def test_only_one_alert_monitor_owns_the_state_file(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    lock_path = settings.state_path.with_name(f"{settings.state_path.name}.alerts")
    messages: list[str] = []
    with exclusive_worker_lock(lock_path), _client(messages) as client:
        result = run_persisted_alert_cycle(settings, client=client)
    assert result["alerts"] == "failed"
    assert messages == []
