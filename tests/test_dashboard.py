from __future__ import annotations

import hashlib
import sqlite3
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast

import pytest
from fastapi.testclient import TestClient
from solders.pubkey import Pubkey

from memecoin_bot.config import Settings
from memecoin_bot.dashboard import (
    REMEMBERED_IDLE_SECONDS,
    SESSION_COOKIE,
    _research_entry_cutoff,
    create_dashboard_app,
)
from memecoin_bot.execution import estimated_paper_swap_fraction_bps
from memecoin_bot.journal import TradeJournal
from memecoin_bot.jupiter import JupiterError
from memecoin_bot.ohlcv import PoolCandle
from memecoin_bot.strategy import StrategyDecision
from memecoin_bot.strategy_lab import RESEARCH_COHORT_KEY, RESEARCH_SELECTION_METHOD
from memecoin_bot.worker_lock import WorkerAlreadyRunningError


def _settings(tmp_path: Path) -> Settings:
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
        state_path=tmp_path / "state.sqlite3",
    )


def test_openai_key_is_never_returned_by_dashboard(tmp_path: Path) -> None:
    settings = replace(_settings(tmp_path), openai_api_key="private-openai-test-key")
    app = create_dashboard_app(settings, "test-pairing-code")
    with TestClient(app, base_url="http://127.0.0.1") as client:
        assert client.get("/api/narrative").status_code == 401
        assert client.post("/api/auth/pair", json={"code": "test-pairing-code"}).status_code == 200
        narrative = client.get("/api/narrative")
        assert narrative.status_code == 200
        assert narrative.json()["openai_research"]["key_configured"] is True
        assert narrative.json()["openai_research"]["enabled"] is False
        assert "private-openai-test-key" not in narrative.text


def test_duplicate_dashboard_cannot_start_workers_on_same_journal(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    first = create_dashboard_app(settings, "first-pairing-code")
    second = create_dashboard_app(settings, "second-pairing-code")
    with TestClient(first, base_url="http://127.0.0.1") as first_client:
        assert first_client.get("/").status_code == 200
        with (
            pytest.raises(WorkerAlreadyRunningError),
            TestClient(second, base_url="http://127.0.0.1"),
        ):
            pass


def test_startup_failure_stops_workers_before_releasing_journal_lock(tmp_path: Path) -> None:
    settings = replace(
        _settings(tmp_path),
        paper_automation_autostart=True,
        strategy_lab_collection_enabled=True,
    )
    app = create_dashboard_app(settings, "test-pairing-code")
    app.state.paper_automation._cycle = lambda: {"paused": False, "exits": []}
    app.state.paper_exit_monitor._cycle = lambda: {"paused": False, "exits": []}

    def fail_start() -> None:
        raise RuntimeError("simulated collector startup failure")

    app.state.market_collector.start = fail_start
    with (
        pytest.raises(RuntimeError, match="simulated collector startup failure"),
        TestClient(app, base_url="http://127.0.0.1"),
    ):
        pass
    assert app.state.paper_automation.status()["running"] is False
    assert app.state.paper_exit_monitor.status()["running"] is False


def test_startup_quarantines_unmonitored_open_paper_position(tmp_path: Path) -> None:
    settings = replace(_settings(tmp_path), paper_automation_autostart=True)
    old = (datetime.now(UTC) - timedelta(minutes=10)).isoformat()
    with TradeJournal(settings.state_path) as journal:
        journal.set_runtime_state(
            "paper_campaign",
            {"mode": "forward_paper_only", "status": "active", "started_at": old},
        )
        journal.open_paper_position(
            position_id="unmonitored",
            mint=str(Pubkey.new_unique()),
            entry_lamports=100,
            token_amount=1_000,
            simulated_units=1_000,
            starting_lamports=1_000,
        )
        journal._connection.execute(
            "INSERT INTO worker_cycle_history "
            "(worker_key, started_at, completed_at, outcome, run_id) VALUES (?, ?, ?, ?, ?)",
            ("paper_exit_monitor", old, old, "completed", "old-worker"),
        )
        journal._connection.commit()
        original_fills = len(journal.paper_fills())

    app = create_dashboard_app(settings, "test-pairing-code")
    with TestClient(app, base_url="http://127.0.0.1") as client:
        assert client.get("/").status_code == 200
        assert app.state.paper_automation.status()["running"] is False
        assert app.state.paper_exit_monitor.status()["running"] is False
        paired = client.post("/api/auth/pair", json={"code": "test-pairing-code"})
        csrf = paired.json()["csrf_token"]
        headers = {"x-csrf-token": csrf}
        assert client.get("/api/operations/health").status_code == 200
        for path in (
            "/api/automation/start",
            "/api/automation/run-once",
            "/api/paper/portfolio/refresh",
            "/api/strategy/cycle",
        ):
            response = client.post(path, headers=headers)
            assert response.status_code == 400, path
            assert "interrupted" in response.text
    with TradeJournal(settings.state_path, readonly=True) as journal:
        assert (
            cast(dict[str, object], journal.runtime_state("paper_campaign"))["status"]
            == "interrupted"
        )
        assert journal.paper_position("unmonitored")["status"] == "open"
        assert len(journal.paper_fills()) == original_fills


def test_runtime_watchdog_quarantines_stalled_exit_monitor(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    now = datetime.now(UTC).isoformat()
    old = (datetime.now(UTC) - timedelta(minutes=10)).isoformat()
    with TradeJournal(settings.state_path) as journal:
        journal.set_runtime_state(
            "paper_campaign",
            {"mode": "forward_paper_only", "status": "active", "started_at": now},
        )
    app = create_dashboard_app(settings, "test-pairing-code")
    with TestClient(app, base_url="http://127.0.0.1"):
        with TradeJournal(settings.state_path) as journal:
            journal.open_paper_position(
                position_id="unmonitored",
                mint=str(Pubkey.new_unique()),
                entry_lamports=100,
                token_amount=1_000,
                simulated_units=1_000,
                starting_lamports=1_000,
            )
            journal._connection.execute(
                "INSERT INTO worker_cycle_history "
                "(worker_key, started_at, completed_at, outcome, run_id) "
                "VALUES (?, ?, ?, ?, ?)",
                ("paper_exit_monitor", old, old, "completed", "old-worker"),
            )
            journal._connection.commit()
            original_fills = len(journal.paper_fills())
        result = app.state.paper_watchdog.run_once()
        assert result["paused"] is True
        assert app.state.paper_automation.status()["running"] is False
        assert app.state.paper_exit_monitor.status()["running"] is False
        with TradeJournal(settings.state_path, readonly=True) as journal:
            assert (
                cast(dict[str, object], journal.runtime_state("paper_campaign"))["status"]
                == "interrupted"
            )
            assert journal.paper_position("unmonitored")["status"] == "open"
            assert len(journal.paper_fills()) == original_fills
            watchdog = journal.runtime_state("paper_watchdog") or {}
            assert watchdog.get("state") == "paused"
            assert watchdog.get("last_completed_at")
            assert journal.worker_cycles_since("paper_watchdog", old)[-1]["outcome"] == "paused"


def test_dashboard_decision_shows_blocking_reason_first(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    with TradeJournal(settings.state_path) as journal:
        journal.record_strategy_decision(
            StrategyDecision(
                mint=str(Pubkey.new_unique()),
                score=0,
                verdict="reject",
                reasons=("on-chain audit did not pass", "no token-linked narrative evidence (+0)"),
                evidence={"market_gate_reasons": ["on-chain audit did not pass"]},
            ),
            action="rejected",
        )
    app = create_dashboard_app(settings, "test-pairing-code")
    with TestClient(app, base_url="http://127.0.0.1") as client:
        assert client.get("/api/strategy/decisions").status_code == 401
        assert client.post("/api/auth/pair", json={"code": "test-pairing-code"}).status_code == 200
        decision = client.get("/api/strategy/decisions").json()["decisions"][0]
        assert decision["primary_reason"] == "on-chain audit did not pass"
        assert decision["reasons"][-1] == "no token-linked narrative evidence (+0)"


def test_dashboard_requires_pairing_and_csrf(tmp_path: Path) -> None:
    app = create_dashboard_app(_settings(tmp_path), "test-pairing-code")
    with TestClient(app, base_url="http://127.0.0.1") as client:
        index = client.get("/")
        assert index.status_code == 200
        assert "default-src 'self'" in index.headers["content-security-policy"]
        assert index.headers["x-content-type-options"] == "nosniff"

        assert client.get("/api/auth/status").json() == {"authenticated": False}

        assert client.get("/api/orders").status_code == 401
        assert client.get("/api/strategy-lab/summary").status_code == 401
        assert client.post("/api/auth/pair", json={"code": "wrong-code"}).status_code == 401

        paired = client.post("/api/auth/pair", json={"code": "test-pairing-code"})
        assert paired.status_code == 200
        csrf = paired.json()["csrf_token"]
        assert csrf
        assert client.get("/api/auth/status").json()["authenticated"] is True
        assert client.get("/api/discovery/candidates").status_code == 200
        assert client.get("/api/automation/status").status_code == 200
        assert client.get("/api/calls").status_code == 200
        assert client.get("/api/narrative").status_code == 200
        lab_summary = client.get("/api/strategy-lab/summary")
        assert lab_summary.status_code == 200
        assert lab_summary.json()["latest_followup"] is None
        assert lab_summary.json()["entry_readiness"]["market_gate_eligible"] == 0
        assert client.get("/api/operations/health").status_code == 200
        assert client.post("/api/discovery/scan").status_code == 403
        assert client.post("/api/automation/start").status_code == 403
        assert client.post("/api/strategy-lab/collect").status_code == 403
        assert client.post("/api/narrative/refresh").status_code == 403
        assert client.post("/api/operations/backup").status_code == 403
        assert (
            client.post(
                "/api/strategy-lab/backtest",
                json={
                    "mint": "11111111111111111111111111111111",
                    "timeframe": "5m",
                    "tp_levels_bps": [10_000],
                    "tp_sell_bps": [5_000],
                    "stop_loss_bps": 3_500,
                },
            ).status_code
            == 403
        )
        assert (
            client.post(
                "/api/strategy-lab/evaluate",
                json={
                    "mint": "11111111111111111111111111111111",
                    "timeframe": "5m",
                    "tp_levels_bps": [10_000],
                    "tp_sell_bps": [5_000],
                    "stop_loss_bps": 3_500,
                },
            ).status_code
            == 403
        )
        assert (
            client.post(
                "/api/calls",
                json={"mints": ["11111111111111111111111111111111"]},
            ).status_code
            == 403
        )
        assert (
            client.post(
                "/api/paper/open",
                json={"mint": "11111111111111111111111111111111", "amount_sol": "0.005"},
            ).status_code
            == 403
        )

        assert client.post("/api/auth/logout").status_code == 403
        logged_out = client.post("/api/auth/logout", headers={"X-CSRF-Token": csrf})
        assert logged_out.status_code == 200
        assert client.get("/api/auth/status").json() == {"authenticated": False}


def test_remembered_browser_survives_restart_and_logout_revokes_it(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    first_app = create_dashboard_app(settings, "first-pairing-code")
    with TestClient(first_app, base_url="http://127.0.0.1") as client:
        paired = client.post(
            "/api/auth/pair", json={"code": "first-pairing-code", "remember_browser": True}
        )
        assert paired.status_code == 200
        assert "Max-Age=1209600" in paired.headers["set-cookie"]
        session_id = client.cookies.get(SESSION_COOKIE)
        assert session_id is not None
        first_csrf = paired.json()["csrf_token"]
        auth_db = settings.state_path.with_name("state.dashboard-auth.sqlite3")
        assert auth_db.is_file()
        assert session_id.encode() not in auth_db.read_bytes()
        assert b"first-pairing-code" not in auth_db.read_bytes()

    restarted_app = create_dashboard_app(settings, "new-pairing-code")
    with TestClient(restarted_app, base_url="http://127.0.0.1") as client:
        client.cookies.set(SESSION_COOKIE, session_id)
        status = client.get("/api/auth/status")
        assert status.status_code == 200
        assert status.json()["authenticated"] is True
        restarted_csrf = status.json()["csrf_token"]
        assert restarted_csrf != first_csrf
        assert client.get("/api/operations/health").status_code == 200
        assert (
            client.post("/api/auth/logout", headers={"X-CSRF-Token": first_csrf}).status_code == 403
        )
        assert (
            client.post("/api/auth/logout", headers={"X-CSRF-Token": restarted_csrf}).status_code
            == 200
        )
        client.cookies.set(SESSION_COOKIE, session_id)
        assert client.get("/api/auth/status").json() == {"authenticated": False}


def test_short_session_does_not_survive_restart_and_remember_option_is_strict(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    with TestClient(
        create_dashboard_app(settings, "first-pairing-code"), base_url="http://127.0.0.1"
    ) as client:
        assert (
            client.post(
                "/api/auth/pair",
                json={"code": "first-pairing-code", "remember_browser": "yes"},
            ).status_code
            == 422
        )
        paired = client.post("/api/auth/pair", json={"code": "first-pairing-code"})
        assert paired.status_code == 200
        session_id = client.cookies.get(SESSION_COOKIE)
        assert session_id is not None
    with TestClient(
        create_dashboard_app(settings, "new-pairing-code"), base_url="http://127.0.0.1"
    ) as client:
        client.cookies.set(SESSION_COOKIE, session_id)
        assert client.get("/api/auth/status").json() == {"authenticated": False}


def test_remembered_expiry_and_logout_all_fail_closed(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    app = create_dashboard_app(settings, "test-pairing-code")
    with TestClient(app, base_url="http://127.0.0.1") as first:
        second = TestClient(app, base_url="http://127.0.0.1")
        third = TestClient(app, base_url="http://127.0.0.1")
        clients = (first, second, third)
        ids: list[str] = []
        for client in clients:
            paired = client.post(
                "/api/auth/pair", json={"code": "test-pairing-code", "remember_browser": True}
            )
            assert paired.status_code == 200
            session_id = client.cookies.get(SESSION_COOKIE)
            assert session_id is not None
            ids.append(session_id)
        auth_db = settings.state_path.with_name("state.dashboard-auth.sqlite3")
        with sqlite3.connect(auth_db) as connection:
            connection.execute(
                "UPDATE remembered_sessions SET last_seen_at = ? WHERE session_hash = ?",
                (
                    int(time.time()) - REMEMBERED_IDLE_SECONDS - 1,
                    hashlib.sha256(ids[0].encode()).hexdigest(),
                ),
            )
            connection.execute(
                "UPDATE remembered_sessions SET expires_at = ? WHERE session_hash = ?",
                (int(time.time()) - 1, hashlib.sha256(ids[1].encode()).hexdigest()),
            )
        assert first.get("/api/auth/status").json() == {"authenticated": False}
        assert second.get("/api/auth/status").json() == {"authenticated": False}
        csrf = third.get("/api/auth/status").json()["csrf_token"]
        assert third.post("/api/auth/logout-all").status_code == 403
        assert third.post("/api/auth/logout-all", headers={"X-CSRF-Token": csrf}).status_code == 200
        third.cookies.set(SESSION_COOKIE, ids[2])
        assert third.get("/api/auth/status").json() == {"authenticated": False}


def test_remembered_store_failure_does_not_issue_a_cookie(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    auth_db = settings.state_path.with_name("state.dashboard-auth.sqlite3")
    auth_db.write_bytes(b"not a SQLite database")
    app = create_dashboard_app(settings, "test-pairing-code")
    with TestClient(app, base_url="http://127.0.0.1") as client:
        response = client.post(
            "/api/auth/pair", json={"code": "test-pairing-code", "remember_browser": True}
        )
        assert response.status_code == 503
        assert client.cookies.get(SESSION_COOKIE) is None
        short_session = client.post("/api/auth/pair", json={"code": "test-pairing-code"})
        assert short_session.status_code == 200
        assert (
            client.post(
                "/api/auth/logout", headers={"X-CSRF-Token": short_session.json()["csrf_token"]}
            ).status_code
            == 200
        )


def test_automation_controls_both_discovery_and_exit_workers(tmp_path: Path) -> None:
    app = create_dashboard_app(_settings(tmp_path), "test-pairing-code")

    def empty_cycle() -> dict[str, object]:
        return {"paused": False, "scan": {}, "decision": {}, "exits": []}

    app.state.paper_automation._cycle = empty_cycle
    app.state.paper_exit_monitor._cycle = empty_cycle
    with TestClient(app, base_url="http://127.0.0.1") as client:
        csrf = client.post("/api/auth/pair", json={"code": "test-pairing-code"}).json()[
            "csrf_token"
        ]
        headers = {"X-CSRF-Token": csrf}
        started = client.post("/api/automation/start", headers=headers)
        assert started.status_code == 200
        assert started.json()["running"] is True
        assert started.json()["exit_monitor"]["running"] is True
        health = client.get("/api/operations/health").json()
        assert "paper_exit_monitor" in health["workers"]
        app.state.security.last_automation_command = 0
        stopped = client.post("/api/automation/stop", headers=headers)
        assert stopped.status_code == 200
        assert stopped.json()["running"] is False
        assert stopped.json()["exit_monitor"]["running"] is False


def test_collector_fresh_entry_respects_pause_and_exit_monitor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _settings(tmp_path)
    app = create_dashboard_app(settings, "test-pairing-code")
    mint = str(Pubkey.new_unique())
    monkeypatch.setattr(
        "memecoin_bot.dashboard.collect_market_snapshots",
        lambda *_args, **_kwargs: {
            "ohlcv_refreshed_mints": [mint],
            "authentic_candles_refreshed": 1,
        },
    )
    monkeypatch.setattr(
        "memecoin_bot.dashboard.refreshed_paper_entry_opportunities",
        lambda *_args, **_kwargs: [mint],
    )

    def fail_resources(*_args: object, **_kwargs: object) -> None:
        pytest.fail("Paused collector loaded the trading wallet")

    monkeypatch.setattr("memecoin_bot.dashboard._paper_resources", fail_resources)
    app.state.paper_automation.status = lambda: {"state": "paused"}
    assert app.state.market_collector._cycle()["decision"] is None

    @contextmanager
    def fake_resources(
        _settings: Settings,
    ) -> Iterator[tuple[object, object, object, TradeJournal]]:
        with TradeJournal(settings.state_path) as journal:
            yield object(), object(), object(), journal

    observed_breakers: list[list[str]] = []

    def fake_decision_cycle(*, forced_breaker_reasons: list[str], **_: object) -> dict[str, object]:
        observed_breakers.append(forced_breaker_reasons)
        return {
            "evaluated": 1,
            "opened": None,
            "entry_paused": True,
            "breaker_reasons": forced_breaker_reasons,
        }

    monkeypatch.setattr("memecoin_bot.dashboard._paper_resources", fake_resources)
    monkeypatch.setattr(
        "memecoin_bot.dashboard.paper_exit_monitor_issues",
        lambda *_args, **_kwargs: ["exit_monitor_unavailable"],
    )
    monkeypatch.setattr("memecoin_bot.dashboard.run_paper_decision_cycle", fake_decision_cycle)
    app.state.paper_automation.status = lambda: {"state": "running"}
    result = app.state.market_collector._cycle()
    assert observed_breakers == [["exit_monitor_unavailable"]]
    assert result["entry_paused"] is True
    assert result["pause_reason"] == "exit_monitor_unavailable"


def test_strategy_lab_summary_exposes_recorded_followup(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    selected_at = int(time.time()) - 30
    with TradeJournal(settings.state_path) as journal:
        journal.record_market_snapshot(
            mint="research-mint",
            pool="research-pool",
            price_usd="1",
            liquidity_usd="50000",
            volume_24h_usd="100000",
            observed_at=selected_at - 5,
        )
        journal.record_decision_followup_selection(
            "research-mint",
            selected_at=selected_at,
            expires_at=selected_at + 3600,
            population_count=4,
            method="sha256_60m_market_eligible_v2",
        )
    app = create_dashboard_app(settings, "test-pairing-code")
    with TestClient(app, base_url="http://127.0.0.1") as client:
        assert client.get("/api/strategy-lab/summary").status_code == 401
        assert client.post("/api/auth/pair", json={"code": "test-pairing-code"}).status_code == 200
        response = client.get("/api/strategy-lab/summary")
        assert response.status_code == 200
        followup = response.json()["latest_followup"]
        assert followup["mint"] == "research-mint"
        assert followup["selection_method"] == "sha256_60m_market_eligible_v2"
        assert followup["population_count"] == 4
        assert followup["outcomes"]["5m"]["status"] == "pending_horizon"
        assert followup["outcomes"]["15m"]["status"] == "pending_horizon"


def test_manual_paper_open_obeys_position_breaker_before_network(tmp_path: Path) -> None:
    settings = replace(
        _settings(tmp_path),
        strategy_lab_collection_enabled=False,
        paper_automation_autostart=False,
        narrative_intelligence_enabled=False,
    )
    with TradeJournal(settings.state_path) as journal:
        for index in range(settings.strategy_max_open_positions):
            journal.open_paper_position(
                position_id=f"held-{index}",
                mint=f"mint-{index}",
                entry_lamports=10_000_000,
                token_amount=1_000_000,
                simulated_units=0,
                starting_lamports=settings.paper_starting_lamports,
            )
    app = create_dashboard_app(settings, "test-pairing-code")
    with TestClient(app, base_url="http://127.0.0.1") as client:
        csrf = client.post("/api/auth/pair", json={"code": "test-pairing-code"}).json()[
            "csrf_token"
        ]
        result = client.post(
            "/api/paper/open",
            headers={"X-CSRF-Token": csrf},
            json={"mint": "11111111111111111111111111111111", "amount_sol": "0.005"},
        )
    assert result.status_code == 400
    assert result.json()["message"] == "Paper entry paused: position_limit"


def test_dashboard_rejects_untrusted_host(tmp_path: Path) -> None:
    app = create_dashboard_app(_settings(tmp_path), "test-pairing-code")
    with TestClient(app, base_url="http://malicious.example") as client:
        assert client.get("/").status_code == 400


def test_dashboard_returns_safe_expected_error(tmp_path: Path) -> None:
    app = create_dashboard_app(_settings(tmp_path), "test-pairing-code")

    @app.get("/test/provider-error")
    def provider_error() -> None:
        raise JupiterError("Jupiter rejected the request: route unavailable")

    with TestClient(app, base_url="http://127.0.0.1", raise_server_exceptions=False) as client:
        response = client.get("/test/provider-error")

    assert response.status_code == 400
    assert response.json() == {
        "error": "JupiterError",
        "message": "Jupiter rejected the request: route unavailable",
    }
    assert response.headers["cache-control"] == "no-store"


def test_strategy_lab_api_exposes_real_candles_and_costed_paper_replay(tmp_path: Path) -> None:
    settings = replace(
        _settings(tmp_path),
        strategy_lab_collection_enabled=False,
        paper_automation_autostart=False,
        narrative_intelligence_enabled=False,
    )
    mint = "DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263"
    pool = "5zpyutJu9ee6jFymDGoK7F6S5Kczqtc9FomP3ueKuyA9"
    now = int(datetime.now(UTC).timestamp())
    latest = (now // 60 - 1) * 60
    with TradeJournal(settings.state_path) as journal:
        journal.upsert_discovery_candidate(
            mint=mint,
            source_program="test",
            audit_status="safe",
            audit_summary=None,
            top10_concentration_bps=1_000,
            decimals=5,
            market={"pair_address": pool, "price_usd": "1", "liquidity_usd": "50000"},
        )
        candles = []
        for index in range(10):
            opened = latest - (9 - index) * 60
            price = 1 + index * 0.02
            candles.append(
                PoolCandle(
                    mint=mint,
                    pool=pool,
                    timeframe="1m",
                    opened_at=opened,
                    open=str(price),
                    high=str(price * 1.02),
                    low=str(price * 0.99),
                    close=str(price * 1.01),
                    volume_usd=str(100 + index * 50),
                )
            )
            journal.record_market_snapshot(
                mint=mint,
                pool=pool,
                price_usd=str(price),
                liquidity_usd="50000",
                volume_24h_usd="1000",
                observed_at=opened + 59,
            )
        journal.record_pool_candles(candles, fetched_at=now)
        aligned_5m = latest - latest % 300
        five_minute_candles = []
        for index in range(8):
            opened = aligned_5m - (8 - index) * 300
            price = 1 + index * 0.02
            five_minute_candles.append(
                PoolCandle(
                    mint=mint,
                    pool=pool,
                    timeframe="5m",
                    opened_at=opened,
                    open=str(price),
                    high=str(price * 1.02),
                    low=str(price * 0.99),
                    close=str(price * 1.01),
                    volume_usd=str(100 + index * 50),
                )
            )
            journal.record_market_snapshot(
                mint=mint,
                pool=pool,
                price_usd=str(price),
                liquidity_usd="50000",
                volume_24h_usd="1000",
                observed_at=opened + 299,
            )
        journal.record_pool_candles(five_minute_candles, fetched_at=now)
        journal.record_research_selection(
            [mint],
            cohort_started_at=now - 600,
            selected_at=now - 600,
            expires_at=now + 3_600,
            population_count=1,
            method=RESEARCH_SELECTION_METHOD,
        )
    app = create_dashboard_app(settings, "test-pairing-code")
    with TestClient(app, base_url="http://127.0.0.1") as client:
        csrf = client.post("/api/auth/pair", json={"code": "test-pairing-code"}).json()[
            "csrf_token"
        ]
        payload = client.get(
            "/api/strategy-lab/candles", params={"mint": mint, "timeframe": "1m"}
        ).json()
        assert payload["source"] == "geckoterminal_pool_trades"
        assert len(payload["candles"]) == 10
        assert payload["candles"][-1]["volume_delta_usd"] == 550
        response = client.post(
            "/api/strategy-lab/backtest",
            headers={"X-CSRF-Token": csrf},
            json={
                "mint": mint,
                "timeframe": "1m",
                "tp_levels_bps": [10_000],
                "tp_sell_bps": [5_000],
                "stop_loss_bps": 3_500,
            },
        )
    assert response.status_code == 200
    replay = response.json()
    assert replay["paper_only"] is True
    assert replay["result"]["status"] == "insufficient_audit_history"
    assert replay["result"]["entry_model"] == "multi_timeframe_candle_gate"
    assert replay["result"]["execution_cost_bps"] > 0
    assert replay["result"]["position_fraction_bps"] == estimated_paper_swap_fraction_bps(settings)
    assert replay["result"]["trades"] == 0  # Backfilled candles predate discovery.


def test_strategy_lab_rejects_replay_when_paper_entry_budget_is_unviable(
    tmp_path: Path,
) -> None:
    settings = replace(_settings(tmp_path), paper_risk_per_trade_bps=1)
    payload = {
        "mint": str(Pubkey.new_unique()),
        "timeframe": "1m",
        "tp_levels_bps": [10_000],
        "tp_sell_bps": [5_000],
        "stop_loss_bps": 3_500,
    }
    for path in ("/api/strategy-lab/backtest", "/api/strategy-lab/evaluate"):
        app = create_dashboard_app(settings, "test-pairing-code")
        with TestClient(app, base_url="http://127.0.0.1") as client:
            csrf = client.post("/api/auth/pair", json={"code": "test-pairing-code"}).json()[
                "csrf_token"
            ]
            response = client.post(path, headers={"X-CSRF-Token": csrf}, json=payload)
            assert response.status_code == 422
            assert response.json()["detail"] == "Paper risk budget is too small for an entry"


def test_research_replay_cutoff_uses_actual_sample_entry_time(tmp_path: Path) -> None:
    mint = str(Pubkey.new_unique())
    with TradeJournal(tmp_path / "cohort-cutoff.sqlite3") as journal:
        journal.record_research_selection(
            [mint],
            cohort_started_at=1_000,
            selected_at=1_500,
            expires_at=87_400,
            population_count=30,
            method=RESEARCH_SELECTION_METHOD,
        )
        candidate: dict[str, object] = {
            "mint": mint,
            "first_seen": datetime.fromtimestamp(1_000, UTC).isoformat(),
        }
        assert _research_entry_cutoff(journal, candidate) == 1_500


def test_research_cohort_remains_visible_after_new_discoveries(tmp_path: Path) -> None:
    settings = replace(
        _settings(tmp_path),
        strategy_lab_collection_enabled=False,
        paper_automation_autostart=False,
        narrative_intelligence_enabled=False,
    )
    mint = str(Pubkey.new_unique())
    pool = str(Pubkey.new_unique())
    now = int(datetime.now(UTC).timestamp())
    with TradeJournal(settings.state_path) as journal:
        journal.upsert_discovery_candidate(
            mint=mint,
            source_program="research",
            audit_status="blocked",
            audit_summary="Research includes failed audits",
            top10_concentration_bps=None,
            decimals=6,
            market={"pair_address": pool, "price_usd": "1", "liquidity_usd": "10000"},
        )
        journal.record_pool_candles(
            [PoolCandle(mint, pool, "1m", (now // 60 - 2) * 60, "1", "1", "1", "1", "100")],
            fetched_at=now,
        )
        for _ in range(101):
            journal.upsert_discovery_candidate(
                mint=str(Pubkey.new_unique()),
                source_program="newer",
                audit_status="safe",
                audit_summary=None,
                top10_concentration_bps=None,
                decimals=6,
            )
        assert mint not in {str(item["mint"]) for item in journal.discovery_candidates(limit=100)}
        journal.set_runtime_state(
            RESEARCH_COHORT_KEY,
            {"mints": [mint], "selected_at": now, "population_count": 100},
        )
    app = create_dashboard_app(settings, "test-pairing-code")
    with TestClient(app, base_url="http://127.0.0.1") as client:
        client.post("/api/auth/pair", json={"code": "test-pairing-code"})
        summary = client.get("/api/strategy-lab/summary").json()
        candles = client.get(
            "/api/strategy-lab/candles", params={"mint": mint, "timeframe": "1m"}
        ).json()
    assert summary["research_cohort"]["size"] == 1
    assert summary["research_capture"]["scope"] == "current_cohort_current_recorded_pool"
    tracked = next(item for item in summary["tracked"] if item["mint"] == mint)
    assert tracked["trusted_candle_count_1m"] == 1
    assert isinstance(tracked["trusted_close_age_seconds_1m"], int)
    assert 0 <= tracked["trusted_close_age_seconds_1m"] <= 120
    assert candles["source"] == "geckoterminal_pool_trades"
    assert len(candles["candles"]) == 1
    assert isinstance(candles["latest_trusted_close_age_seconds"], int)
    assert 0 <= candles["latest_trusted_close_age_seconds"] <= 120


def test_dashboard_marks_legacy_proxy_backtests_unverified(tmp_path: Path) -> None:
    settings = replace(
        _settings(tmp_path),
        strategy_lab_collection_enabled=False,
        paper_automation_autostart=False,
        narrative_intelligence_enabled=False,
    )
    with TradeJournal(settings.state_path) as journal:
        journal.record_backtest(
            mint="legacy-mint",
            timeframe="1m",
            strategy_name="legacy",
            result={"status": "complete", "return_pct": 900, "promotion_ready": True},
        )
    app = create_dashboard_app(settings, "test-pairing-code")
    with TestClient(app, base_url="http://127.0.0.1") as client:
        client.post("/api/auth/pair", json={"code": "test-pairing-code"})
        runs = client.get("/api/strategy-lab/summary").json()["runs"]
    assert runs[0]["result"]["status"] == "legacy_unverified"
    assert runs[0]["result"]["promotion_ready"] is False
    assert "return_pct" not in runs[0]["result"]


def test_dashboard_excludes_saved_single_frame_runs_without_entry_model(tmp_path: Path) -> None:
    settings = replace(
        _settings(tmp_path),
        strategy_lab_collection_enabled=False,
        paper_automation_autostart=False,
        narrative_intelligence_enabled=False,
    )
    with TradeJournal(settings.state_path) as journal:
        journal.record_backtest(
            mint="old-real-candles",
            timeframe="1m",
            strategy_name="multi-factor-confirmation-v1",
            result={
                "status": "complete",
                "data_source": "geckoterminal_pool_trades",
                "return_pct": 900,
                "promotion_ready": True,
            },
        )
    app = create_dashboard_app(settings, "test-pairing-code")
    with TestClient(app, base_url="http://127.0.0.1") as client:
        client.post("/api/auth/pair", json={"code": "test-pairing-code"})
        runs = client.get("/api/strategy-lab/summary").json()["runs"]
    assert runs[0]["result"]["status"] == "legacy_entry_model_unverified"
    assert runs[0]["result"]["promotion_ready"] is False
    assert "return_pct" not in runs[0]["result"]


def test_dashboard_excludes_old_five_minute_provenance_runs(tmp_path: Path) -> None:
    settings = replace(
        _settings(tmp_path),
        strategy_lab_collection_enabled=False,
        paper_automation_autostart=False,
        narrative_intelligence_enabled=False,
    )
    with TradeJournal(settings.state_path) as journal:
        journal.record_backtest(
            mint="old-five-minute-model",
            timeframe="1m",
            strategy_name="multi-timeframe-candle-gate-v2",
            result={
                "status": "complete",
                "data_source": "geckoterminal_pool_trades",
                "entry_model": "multi_timeframe_candle_gate",
                "return_pct": 900,
                "promotion_ready": True,
            },
        )
    app = create_dashboard_app(settings, "test-pairing-code")
    with TestClient(app, base_url="http://127.0.0.1") as client:
        client.post("/api/auth/pair", json={"code": "test-pairing-code"})
        runs = client.get("/api/strategy-lab/summary").json()["runs"]
    assert runs[0]["result"]["status"] == "legacy_candle_provenance_unverified"
    assert runs[0]["result"]["promotion_ready"] is False
    assert "return_pct" not in runs[0]["result"]
