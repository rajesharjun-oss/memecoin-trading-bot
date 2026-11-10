from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Event, Thread
from types import SimpleNamespace
from typing import cast

import pytest
from solders.pubkey import Pubkey

from memecoin_bot import automation
from memecoin_bot.automation import (
    PaperAutomationWorker,
    evaluate_exit_signal,
    record_paper_quote_health,
)
from memecoin_bot.config import Settings
from memecoin_bot.journal import TradeJournal
from memecoin_bot.paper import PaperTradingService
from memecoin_bot.rpc import RpcClient
from memecoin_bot.service import TradingService
from memecoin_bot.strategy import open_position_liquidity_status, paper_entry_breakers


def _settings(tmp_path: Path, **overrides: object) -> Settings:
    values: dict[str, object] = {
        "rpc_url": "https://rpc.example.test",
        "keypair_path": None,
        "jupiter_api_key": None,
        "live_trading_enabled": False,
        "max_trade_lamports": 50_000_000,
        "max_daily_lamports": 100_000_000,
        "max_slippage_bps": 300,
        "max_price_impact_bps": 500,
        "max_top10_concentration_bps": 7_000,
        "max_priority_fee_lamports": 1_000_000,
        "confirmation_timeout_seconds": 45,
        "allow_token_2022": False,
        "state_path": tmp_path / "state.sqlite3",
    }
    values.update(overrides)
    return Settings(**values)  # type: ignore[arg-type]


def _position(*, current: int, peak: int, opened_at: datetime) -> dict[str, object]:
    return {
        "position_id": "paper-00000000-0000-0000-0000-000000000001",
        "status": "open",
        "quote_state": "live",
        "entry_lamports": 100,
        "current_value_lamports": current,
        "performance_value_lamports": current,
        "peak_value_lamports": peak,
        "tp_stage": 0,
        "opened_at": opened_at.isoformat(),
    }


def test_exit_policy_covers_stop_profit_trailing_and_max_hold(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    now = datetime.now(UTC)

    stop = evaluate_exit_signal(_position(current=60, peak=100, opened_at=now), settings, now=now)
    profit = evaluate_exit_signal(
        _position(current=210, peak=210, opened_at=now), settings, now=now
    )
    trailing = evaluate_exit_signal(
        _position(current=120, peak=150, opened_at=now), settings, now=now
    )
    max_hold = evaluate_exit_signal(
        _position(current=100, peak=100, opened_at=now - timedelta(days=2)),
        settings,
        now=now,
    )

    assert stop and stop.reason == "stop_loss"
    assert profit and profit.reason == "take_profit_stage_1"
    assert profit.action == "partial"
    assert profit.sell_bps == 5_000
    assert trailing and trailing.reason == "trailing_stop"
    assert max_hold and max_hold.reason == "max_hold_time"


def test_exit_monitor_runs_while_discovery_scan_is_blocked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _settings(tmp_path)
    position = _position(current=100, peak=100, opened_at=datetime.now(UTC) - timedelta(days=2))
    position["mint"] = str(Pubkey.new_unique())
    scan_started = Event()
    release_scan = Event()
    exit_closed = Event()
    monitor_completed = Event()
    is_open = True
    scan_result: dict[str, object] = {}

    def portfolio(*, refresh: bool) -> dict[str, object]:
        del refresh
        return {
            "positions": [position] if is_open else [],
            "equity_lamports": settings.paper_starting_lamports,
        }

    def close(**kwargs: object) -> dict[str, object]:
        nonlocal is_open
        assert kwargs["position_id"] == position["position_id"]
        is_open = False
        exit_closed.set()
        return {"exit_quote_lamports": 100}

    paper = cast(PaperTradingService, SimpleNamespace(portfolio=portfolio, close=close))

    def blocked_scan(**kwargs: object) -> dict[str, int]:
        del kwargs
        scan_started.set()
        assert release_scan.wait(5)
        return {"scanned": 0, "safe": 0}

    monkeypatch.setattr(automation, "scan_candidates", blocked_scan)
    monkeypatch.setattr(
        automation,
        "run_paper_decision_cycle",
        lambda **kwargs: {"evaluated": 0, "opened": None, "breaker_reasons": []},
    )

    def scan() -> None:
        with TradeJournal(settings.state_path) as journal:
            scan_result.update(
                automation.run_autonomous_paper_cycle(
                    settings=settings,
                    paper=paper,
                    rpc=cast(RpcClient, object()),
                    trading=cast(TradingService, object()),
                    journal=journal,
                    manage_exits=False,
                )
            )

    def exit_cycle() -> dict[str, object]:
        with TradeJournal(settings.state_path) as journal:
            return automation.run_paper_exit_cycle(settings=settings, paper=paper, journal=journal)

    def save_monitor(payload: dict[str, object]) -> None:
        with TradeJournal(settings.state_path) as journal:
            journal.set_runtime_state("paper_exit_monitor", payload)
        if payload.get("cycles_completed") == 1:
            monitor_completed.set()

    scan_thread = Thread(target=scan)
    scan_thread.start()
    assert scan_started.wait(3)
    monitor = PaperAutomationWorker(interval_seconds=30, cycle=exit_cycle, state_saver=save_monitor)
    try:
        monitor.start()
        assert exit_closed.wait(3), "Exit was blocked by discovery"
        assert monitor_completed.wait(3)
        assert scan_thread.is_alive(), "Discovery must still be blocked"
    finally:
        release_scan.set()
        scan_thread.join(timeout=5)
        monitor.stop()

    assert not scan_thread.is_alive()
    assert scan_result["entry_paused"] is False
    assert scan_result["exits"] == []


def test_entry_blocks_when_exit_monitor_is_missing_stale_or_failed(tmp_path: Path) -> None:
    settings = _settings(tmp_path, paper_automation_interval_seconds=300)
    with TradeJournal(settings.state_path) as journal:
        assert automation._exit_monitor_breakers(settings, journal) == ["exit_monitor_unavailable"]
        journal.set_runtime_state(
            "paper_exit_monitor",
            {
                "state": "running",
                "run_id": "same-run",
                "last_completed_run_id": "same-run",
                "last_completed_at": (datetime.now(UTC) - timedelta(minutes=6)).isoformat(),
                "last_result": {"quote_failures": 0, "exit_failures": 0},
            },
        )
        assert automation._exit_monitor_breakers(settings, journal) == ["exit_monitor_stale"]
        journal.set_runtime_state(
            "paper_exit_monitor",
            {
                "state": "running",
                "run_id": "same-run",
                "last_completed_run_id": "same-run",
                "last_completed_at": datetime.now(UTC).isoformat(),
                "last_result": {"quote_failures": 1, "exit_failures": 1},
            },
        )
        assert automation._exit_monitor_breakers(settings, journal) == [
            "exit_execution_failed",
            "exit_quote_unavailable",
        ]


def test_entry_uses_current_exit_worker_after_restart(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _settings(tmp_path)
    paper = cast(
        PaperTradingService,
        SimpleNamespace(
            portfolio=lambda *, refresh: {
                "positions": [],
                "equity_lamports": settings.paper_starting_lamports,
            }
        ),
    )
    monkeypatch.setattr(automation, "scan_candidates", lambda **kwargs: {"scanned": 0, "safe": 0})
    monkeypatch.setattr(automation, "paper_entry_breakers", lambda *args: [])
    monkeypatch.setattr(
        automation,
        "run_paper_decision_cycle",
        lambda **kwargs: {"evaluated": 0, "opened": None, "breaker_reasons": []},
    )
    now = datetime.now(UTC).isoformat()
    old_result = {
        "state": "running",
        "run_id": "previous-run",
        "last_completed_run_id": "previous-run",
        "last_completed_at": now,
        "last_result": {"quote_failures": 0, "exit_failures": 0},
    }
    with TradeJournal(settings.state_path) as journal:
        journal.set_runtime_state("paper_exit_monitor", old_result)
        result = automation.run_autonomous_paper_cycle(
            settings=settings,
            paper=paper,
            rpc=cast(RpcClient, object()),
            trading=cast(TradingService, object()),
            journal=journal,
            manage_exits=False,
            exit_monitor_status=lambda: {
                **old_result,
                "run_id": "current-run",
            },
        )
    assert result["entry_paused"] is True
    assert result["pause_reason"] == "exit_monitor_unavailable"


def test_exit_policy_advances_through_take_profit_ladder(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    now = datetime.now(UTC)
    position = _position(current=450, peak=450, opened_at=now)
    position["tp_stage"] = 1
    signal = evaluate_exit_signal(position, settings, now=now)

    assert signal and signal.reason == "take_profit_stage_2"
    assert signal.sell_bps == 2_000
    assert signal.tp_stage == 2


def test_stop_exit_survives_corrupt_nonessential_position_metadata(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    position = _position(current=60, peak=100, opened_at=datetime.now(UTC))
    position["tp_stage"] = "invalid"
    position["opened_at"] = "invalid"
    signal = evaluate_exit_signal(position, settings)
    assert signal and signal.reason == "stop_loss"


def test_profit_exit_does_not_require_a_valid_open_time(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    position = _position(current=210, peak=210, opened_at=datetime.now(UTC))
    position["opened_at"] = "invalid"
    signal = evaluate_exit_signal(position, settings)
    assert signal and signal.reason == "take_profit_stage_1"


def test_exit_policy_requires_a_live_quote(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    position = _position(current=50, peak=100, opened_at=datetime.now(UTC))
    position["quote_state"] = "stale"
    assert evaluate_exit_signal(position, settings) is None
    position["quote_state"] = "recent_mark"
    assert evaluate_exit_signal(position, settings) is None


@pytest.mark.parametrize(
    ("next_value", "next_quote_state", "expected_failures"),
    [(399, "live", 0), (500, "stale", 1)],
)
def test_partial_take_profit_requires_a_new_exit_quote_before_another_stage(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    next_value: int,
    next_quote_state: str,
    expected_failures: int,
) -> None:
    settings = _settings(
        tmp_path, paper_trailing_activation_bps=5_000, paper_trailing_stop_bps=3_000
    )
    first = _position(current=500, peak=500, opened_at=datetime.now(UTC))
    first["mint"] = str(Pubkey.new_unique())
    later = {
        **first,
        "performance_value_lamports": next_value,
        "peak_value_lamports": 500,
        "tp_stage": 1,
        "quote_state": next_quote_state,
    }
    if next_quote_state == "stale":
        later["quote_error"] = "JupiterError"
    refreshes: list[bool] = []
    partials: list[int] = []

    def portfolio(*, refresh: bool) -> dict[str, object]:
        refreshes.append(refresh)
        position = first if refresh and refreshes.count(True) == 1 else later
        return {"positions": [position], "equity_lamports": settings.paper_starting_lamports}

    def take_partial(**kwargs: object) -> dict[str, object]:
        sell_bps = kwargs["sell_bps_of_original"]
        assert isinstance(sell_bps, int)
        partials.append(sell_bps)
        return {
            "position": {**first, "tp_stage": len(partials)},
            "exit_quote_lamports": 500,
        }

    paper = cast(
        PaperTradingService,
        SimpleNamespace(portfolio=portfolio, take_partial=take_partial),
    )
    monkeypatch.setattr(automation, "scan_candidates", lambda **kwargs: {"scanned": 0, "safe": 0})
    monkeypatch.setattr(
        automation,
        "run_paper_decision_cycle",
        lambda **kwargs: {"evaluated": 0, "opened": None, "breaker_reasons": []},
    )
    with TradeJournal(settings.state_path) as journal:
        result = automation.run_autonomous_paper_cycle(
            settings=settings,
            paper=paper,
            rpc=cast(RpcClient, object()),
            trading=cast(TradingService, object()),
            journal=journal,
        )
    assert refreshes == [True, True, False]
    assert partials == [5_000]
    assert [exit_result["reason"] for exit_result in result["exits"]] == ["take_profit_stage_1"]
    assert len(result["quote_failures"]) == expected_failures
    assert result["entry_paused"] is bool(expected_failures)
    if expected_failures:
        assert "exit_quote_unavailable" in result["breaker_reasons"]


def test_partial_refresh_invalidates_other_positions_old_exit_quotes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _settings(
        tmp_path, paper_trailing_activation_bps=5_000, paper_trailing_stop_bps=3_000
    )
    now = datetime.now(UTC)
    first = _position(current=500, peak=500, opened_at=now)
    first["mint"] = str(Pubkey.new_unique())
    second = _position(current=50, peak=100, opened_at=now)
    second["position_id"] = "paper-00000000-0000-0000-0000-000000000002"
    second["mint"] = str(Pubkey.new_unique())
    refreshed_first = {**first, "performance_value_lamports": 399, "tp_stage": 1}
    refreshed_second = {**second, "quote_state": "stale", "quote_error": "JupiterError"}
    refreshes = 0
    closes: list[str] = []

    def portfolio(*, refresh: bool) -> dict[str, object]:
        nonlocal refreshes
        if refresh:
            refreshes += 1
        if refresh and refreshes == 1:
            positions = [first, second]
        else:
            positions = [refreshed_first, refreshed_second]
        return {"positions": positions, "equity_lamports": settings.paper_starting_lamports}

    def close(**kwargs: object) -> dict[str, object]:
        closes.append(str(kwargs["position_id"]))
        return {"exit_quote_lamports": 50}

    paper = cast(
        PaperTradingService,
        SimpleNamespace(
            portfolio=portfolio,
            take_partial=lambda **kwargs: {
                "position": refreshed_first,
                "exit_quote_lamports": 500,
            },
            close=close,
        ),
    )
    monkeypatch.setattr(automation, "scan_candidates", lambda **kwargs: {"scanned": 0, "safe": 0})
    monkeypatch.setattr(
        automation,
        "run_paper_decision_cycle",
        lambda **kwargs: {"evaluated": 0, "opened": None, "breaker_reasons": []},
    )
    with TradeJournal(settings.state_path) as journal:
        result = automation.run_autonomous_paper_cycle(
            settings=settings,
            paper=paper,
            rpc=cast(RpcClient, object()),
            trading=cast(TradingService, object()),
            journal=journal,
        )
    assert refreshes == 2
    assert closes == []
    assert [item["reason"] for item in result["exits"]] == ["take_profit_stage_1"]
    assert result["entry_paused"] is True
    assert result["pause_reason"] == "exit_quote_unavailable"


def test_quote_outage_is_logged_once_then_escalated_and_recovered(tmp_path: Path) -> None:
    position = _position(current=50, peak=100, opened_at=datetime.now(UTC))
    position["quote_state"] = "stale"
    position["quote_error"] = "JupiterError"
    with TradeJournal(tmp_path / "quote-health.sqlite3") as journal:
        for count in range(1, 4):
            failures = record_paper_quote_health(journal, [position])
            assert len(failures) == 1
            assert failures[0]["failure_cycles"] == count
        events = journal.automation_events()
        assert [event["event_type"] for event in events] == [
            "paper_quote_unavailable",
            "paper_quote_unavailable",
        ]
        position["quote_state"] = "live"
        assert record_paper_quote_health(journal, [position]) == []
        journal._connection.execute(
            "UPDATE paper_automation_events SET created_at = ?",
            ("2026-11-03T00:00:00+00:00",),
        )
        assert journal.automation_events()[0]["event_type"] == "paper_quote_recovered"
        assert record_paper_quote_health(journal, [position]) == []
        assert len(journal.automation_events()) == 3


def test_uneconomic_exit_is_logged_separately_and_remains_unavailable(tmp_path: Path) -> None:
    position = _position(current=50, peak=100, opened_at=datetime.now(UTC))
    position["quote_state"] = "stale"
    position["quote_error"] = "UneconomicPaperExitError"
    position["quote_error_reason"] = "below_exit_fees"
    position["quote_gross_out_lamports"] = 1_344
    with TradeJournal(tmp_path / "below-fees.sqlite3") as journal:
        failures = record_paper_quote_health(journal, [position])
        assert failures[0]["error_reason"] == "below_exit_fees"
        assert failures[0]["quote_gross_out_lamports"] == 1_344
        assert journal.automation_events()[0]["event_type"] == "paper_exit_uneconomic"
        assert journal.runtime_state("paper_quote_health")["unavailable"]


def test_unquotable_open_exit_blocks_new_paper_entries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _settings(tmp_path)
    mint = str(Pubkey.new_unique())
    position = _position(current=100, peak=100, opened_at=datetime.now(UTC))
    position.update({"mint": mint, "quote_state": "stale", "quote_error": "JupiterError"})
    paper = cast(
        PaperTradingService,
        SimpleNamespace(
            portfolio=lambda *, refresh: {
                "positions": [position],
                "equity_lamports": settings.paper_starting_lamports,
            }
        ),
    )
    monkeypatch.setattr(automation, "scan_candidates", lambda **kwargs: {"scanned": 0, "safe": 0})
    with TradeJournal(settings.state_path) as journal:
        journal.open_paper_position(
            position_id=str(position["position_id"]),
            mint=mint,
            entry_lamports=100,
            token_amount=1_000,
            simulated_units=0,
            starting_lamports=settings.paper_starting_lamports,
        )
        result = automation.run_autonomous_paper_cycle(
            settings=settings,
            paper=paper,
            rpc=cast(RpcClient, object()),
            trading=cast(TradingService, object()),
            journal=journal,
        )
    assert result["entry_paused"] is True
    assert result["pause_reason"] == "exit_quote_unavailable"
    assert len(result["quote_failures"]) == 1
    assert result["decision"]["opened"] is None


def test_failed_automated_exit_blocks_new_paper_entries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _settings(tmp_path)
    position = _position(current=50, peak=100, opened_at=datetime.now(UTC))
    position["mint"] = str(Pubkey.new_unique())

    def fail_close(**kwargs: object) -> dict[str, object]:
        raise RuntimeError("route failed")

    paper = cast(
        PaperTradingService,
        SimpleNamespace(
            portfolio=lambda *, refresh: {
                "positions": [position],
                "equity_lamports": settings.paper_starting_lamports,
            },
            close=fail_close,
        ),
    )
    monkeypatch.setattr(automation, "scan_candidates", lambda **kwargs: {"scanned": 0, "safe": 0})
    with TradeJournal(settings.state_path) as journal:
        result = automation.run_autonomous_paper_cycle(
            settings=settings,
            paper=paper,
            rpc=cast(RpcClient, object()),
            trading=cast(TradingService, object()),
            journal=journal,
        )
        events = journal.automation_events()
    assert result["entry_paused"] is True
    assert result["pause_reason"] == "exit_execution_failed"
    assert result["exit_failures"] == [
        {"position_id": position["position_id"], "error_type": "RuntimeError"}
    ]
    assert result["decision"]["opened"] is None
    assert any(event["event_type"] == "paper_exit_failed" for event in events)


def test_bad_exit_position_does_not_skip_other_exits_or_allow_entries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _settings(tmp_path)
    now = datetime.now(UTC)
    bad = _position(current=0, peak=100, opened_at=now)
    bad["mint"] = str(Pubkey.new_unique())
    good = _position(current=50, peak=100, opened_at=now)
    good["position_id"] = "paper-00000000-0000-0000-0000-000000000002"
    good["mint"] = str(Pubkey.new_unique())
    closed: list[str] = []

    def portfolio(*, refresh: bool) -> dict[str, object]:
        good_position = {**good, "status": "closed"} if closed else good
        return {
            "positions": [bad, good_position],
            "equity_lamports": settings.paper_starting_lamports,
        }

    def close(**kwargs: object) -> dict[str, object]:
        closed.append(str(kwargs["position_id"]))
        return {"exit_quote_lamports": 50}

    paper = cast(PaperTradingService, SimpleNamespace(portfolio=portfolio, close=close))
    monkeypatch.setattr(automation, "scan_candidates", lambda **kwargs: {"scanned": 0, "safe": 0})
    monkeypatch.setattr(
        automation,
        "run_paper_decision_cycle",
        lambda **kwargs: {"evaluated": 0, "opened": None, "breaker_reasons": []},
    )
    with TradeJournal(settings.state_path) as journal:
        result = automation.run_autonomous_paper_cycle(
            settings=settings,
            paper=paper,
            rpc=cast(RpcClient, object()),
            trading=cast(TradingService, object()),
            journal=journal,
        )
        events = journal.automation_events()
    assert closed == [good["position_id"]]
    assert [item["reason"] for item in result["exits"]] == ["stop_loss"]
    assert result["exit_failures"] == [
        {"position_id": bad["position_id"], "error_type": "PaperAutomationError"}
    ]
    assert result["entry_paused"] is True
    assert result["pause_reason"] == "exit_evaluation_failed"
    assert result["decision"]["opened"] is None
    assert any(event["event_type"] == "paper_exit_evaluation_failed" for event in events)


def test_open_position_liquidity_breaker_uses_fresh_snapshots_outside_recent_view(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    mint = str(Pubkey.new_unique())
    pool = str(Pubkey.new_unique())
    now = datetime.now(UTC)
    with TradeJournal(settings.state_path) as journal:
        journal.upsert_discovery_candidate(
            mint=mint,
            source_program="test",
            audit_status="safe",
            audit_summary=None,
            top10_concentration_bps=1_000,
            decimals=6,
            market={"pair_address": pool},
        )
        journal.open_paper_position(
            position_id="held-outside-view",
            mint=mint,
            entry_lamports=1_000_000,
            token_amount=1_000_000,
            simulated_units=0,
            starting_lamports=settings.paper_starting_lamports,
        )
        for _ in range(101):
            journal.upsert_discovery_candidate(
                mint=str(Pubkey.new_unique()),
                source_program="other",
                audit_status="blocked",
                audit_summary=None,
                top10_concentration_bps=9_000,
                decimals=6,
            )
        assert mint not in {str(row["mint"]) for row in journal.discovery_candidates(limit=100)}
        journal.record_market_snapshot(
            mint=mint,
            pool=pool,
            price_usd="1",
            liquidity_usd="14000",
            volume_24h_usd=None,
            observed_at=int(now.timestamp()) - 2,
        )
        assert open_position_liquidity_status(settings, journal, {mint}, now=now) == ([mint], [])
        assert "open_position_liquidity_limit" in paper_entry_breakers(settings, journal)
        journal.record_market_snapshot(
            mint=mint,
            pool=pool,
            price_usd="1",
            liquidity_usd="50000",
            volume_24h_usd=None,
            observed_at=int(now.timestamp()) - 1,
        )
        assert open_position_liquidity_status(settings, journal, {mint}, now=now) == ([], [])
        assert not any(
            reason.startswith("open_position_liquidity")
            for reason in paper_entry_breakers(settings, journal)
        )
        journal.record_market_snapshot(
            mint=mint,
            pool=pool,
            price_usd="1",
            liquidity_usd="NaN",
            volume_24h_usd=None,
            observed_at=int(now.timestamp()),
        )
        assert open_position_liquidity_status(settings, journal, {mint}, now=now) == ([], [mint])
        assert "open_position_liquidity_unverified" in paper_entry_breakers(settings, journal)
        journal._connection.execute(
            "UPDATE market_snapshots SET liquidity_usd='50000', captured_at=? WHERE mint=?",
            (int((now - timedelta(minutes=10)).timestamp()), mint),
        )
        assert open_position_liquidity_status(settings, journal, {mint}, now=now) == ([], [mint])
        assert "open_position_liquidity_unverified" in paper_entry_breakers(settings, journal)


def test_worker_records_manual_cycle_and_pause() -> None:
    worker = PaperAutomationWorker(
        interval_seconds=30,
        cycle=lambda: {
            "paused": False,
            "scan": {"scanned": 4, "safe": 1},
            "decision": {"evaluated": 4, "opened": None},
            "exits": [],
            "rpc_request_counts": {"getMultipleAccounts": 2, "getTransaction": 3},
        },
    )
    worker.run_once()
    status = worker.status()
    assert status["state"] == "stopped"
    assert status["cycles_completed"] == 1
    assert status["last_result"] == {
        "paused": False,
        "entry_paused": False,
        "pause_reason": None,
        "scanned": 4,
        "batched_mint_accounts": 0,
        "safe": 1,
        "new_discoveries_scanned": 0,
        "cohort_reaudits": 0,
        "watchlist_reaudits": 0,
        "priority_watchlist_reaudits": 0,
        "evaluated": 4,
        "exits": 0,
        "exit_failures": 0,
        "quote_failures": 0,
        "opened_position_id": None,
        "rpc_request_counts": {"getMultipleAccounts": 2, "getTransaction": 3},
    }

    paused = PaperAutomationWorker(
        interval_seconds=30,
        cycle=lambda: {
            "paused": True,
            "pause_reason": "portfolio_drawdown_limit",
            "scan": None,
            "decision": None,
            "exits": [],
        },
    )
    paused.run_once()
    assert paused.status()["state"] == "paused"


def test_entry_breaker_still_records_candidate_decisions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _settings(tmp_path)
    paper = cast(
        PaperTradingService,
        SimpleNamespace(portfolio=lambda *, refresh: {"positions": [], "equity_lamports": 0}),
    )
    monkeypatch.setattr(
        automation,
        "scan_candidates",
        lambda **kwargs: {"scanned": 0, "safe": 0},
    )
    with TradeJournal(settings.state_path) as journal:
        journal.upsert_discovery_candidate(
            mint="11111111111111111111111111111111",
            source_program="test",
            audit_status="blocked",
            audit_summary="mint authority active",
            top10_concentration_bps=9_000,
            decimals=6,
        )
        result = automation.run_autonomous_paper_cycle(
            settings=settings,
            paper=paper,
            rpc=cast(RpcClient, object()),
            trading=cast(TradingService, object()),
            journal=journal,
        )
        decisions = journal.strategy_decisions()
    assert result["entry_paused"] is True
    assert result["pause_reason"] == "portfolio_drawdown_limit"
    assert result["decision"]["evaluated"] == 1
    assert result["decision"]["opened"] is None
    assert len(decisions) == 1
    assert decisions[0]["action"] == "rejected"


def test_position_limit_is_visible_in_worker_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _settings(tmp_path)
    positions = [
        {"mint": f"held-{index}", "status": "open"}
        for index in range(settings.strategy_max_open_positions)
    ]
    paper = cast(
        PaperTradingService,
        SimpleNamespace(
            portfolio=lambda *, refresh: {
                "positions": positions,
                "equity_lamports": settings.paper_starting_lamports,
            }
        ),
    )
    monkeypatch.setattr(automation, "scan_candidates", lambda **kwargs: {"scanned": 0, "safe": 0})
    with TradeJournal(settings.state_path) as journal:
        for index, position in enumerate(positions):
            journal.open_paper_position(
                position_id=f"held-{index}",
                mint=str(position["mint"]),
                entry_lamports=1_000_000,
                token_amount=1_000_000,
                simulated_units=0,
                starting_lamports=settings.paper_starting_lamports,
            )
        journal.upsert_discovery_candidate(
            mint="11111111111111111111111111111111",
            source_program="test",
            audit_status="blocked",
            audit_summary="mint authority active",
            top10_concentration_bps=9_000,
            decimals=6,
        )
        result = automation.run_autonomous_paper_cycle(
            settings=settings,
            paper=paper,
            rpc=cast(RpcClient, object()),
            trading=cast(TradingService, object()),
            journal=journal,
        )
        decisions = journal.strategy_decisions()
    assert result["entry_paused"] is True
    assert result["pause_reason"] == "position_limit"
    assert result["decision"]["evaluated"] == 1
    assert result["decision"]["opened"] is None
    assert len(decisions) == 1
    worker = PaperAutomationWorker(interval_seconds=30, cycle=lambda: result)
    worker.run_once()
    summary = worker.status()["last_result"]
    assert isinstance(summary, dict)
    assert summary["entry_paused"] is True
    assert summary["pause_reason"] == "position_limit"


def test_missing_open_position_liquidity_blocks_new_paper_entries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _settings(tmp_path)
    held_mint = str(Pubkey.new_unique())
    paper = cast(
        PaperTradingService,
        SimpleNamespace(
            portfolio=lambda *, refresh: {
                "positions": [{"mint": held_mint, "status": "open"}],
                "equity_lamports": settings.paper_starting_lamports,
            }
        ),
    )
    monkeypatch.setattr(automation, "scan_candidates", lambda **kwargs: {"scanned": 0, "safe": 0})
    with TradeJournal(settings.state_path) as journal:
        journal.open_paper_position(
            position_id="held-one",
            mint=held_mint,
            entry_lamports=1_000_000,
            token_amount=1_000_000,
            simulated_units=0,
            starting_lamports=settings.paper_starting_lamports,
        )
        result = automation.run_autonomous_paper_cycle(
            settings=settings,
            paper=paper,
            rpc=cast(RpcClient, object()),
            trading=cast(TradingService, object()),
            journal=journal,
        )
    assert result["entry_paused"] is True
    assert result["pause_reason"] == "open_position_liquidity_unverified"
    assert result["decision"]["opened"] is None


def test_market_collector_summary_keeps_counts_without_provider_payloads() -> None:
    worker = PaperAutomationWorker(
        interval_seconds=60,
        cycle=lambda: {
            "paused": False,
            "scan": {
                "tracked": 20,
                "dex_requests_attempted": 2,
                "dex_requests_succeeded": 1,
                "research_cohort_size": 18,
                "research_cohort_tracked": 18,
                "research_focus_size": 8,
                "inactive_5m_mints_deferred": 2,
                "research_active_5m_size": 2,
                "research_active_5m_eligible": 1,
                "research_active_5m_scheduled": 1,
                "research_active_focus_size": 1,
                "research_active_focus_scheduled": 1,
                "strategy_focus_size": 2,
                "strategy_focus_scheduled": 2,
                "strategy_focus_revision_excluded": 1,
                "strategy_focus_low_activity_excluded": 1,
                "strategy_focus_stale_audit_excluded": 1,
                "decision_followup_size": 1,
                "decision_followup_due": True,
                "decision_followup_attempted": 1,
                "decision_followup_succeeded": 1,
                "collection_lane": "active_research_1m",
                "safe_watchlist_tracked": 2,
                "active_market_ready_size": 1,
                "authentic_candles_refreshed": 42,
                "ohlcv_requests_attempted": 3,
                "ohlcv_requests_succeeded": 2,
                "ohlcv_cache_hits": 1,
                "ohlcv_request_budget_next": 6,
                "research_secondary_attempted": 1,
                "research_secondary_succeeded": 1,
                "ohlcv_empty_responses": 1,
                "ohlcv_retry_cooldown_size": 1,
                "ohlcv_rate_limited": True,
                "ohlcv_errors": ["provider returned a sensitive response"],
                "ohlcv_error_codes": {
                    "pool_not_found": 1,
                    "sensitive provider response": 1,
                },
            },
            "decision": None,
            "exits": [],
        },
    )
    worker.run_once()
    summary = worker.status()["last_result"]
    assert isinstance(summary, dict)
    assert summary["market_collection"] == {
        "tracked": 20,
        "dex_requests_attempted": 2,
        "dex_requests_succeeded": 1,
        "research_cohort_size": 18,
        "research_cohort_tracked": 18,
        "research_focus_size": 8,
        "inactive_5m_mints_deferred": 2,
        "research_active_5m_size": 2,
        "research_active_5m_eligible": 1,
        "research_active_5m_scheduled": 1,
        "research_active_focus_size": 1,
        "research_active_focus_scheduled": 1,
        "strategy_focus_size": 2,
        "strategy_focus_scheduled": 2,
        "strategy_focus_revision_excluded": 1,
        "strategy_focus_low_activity_excluded": 1,
        "strategy_focus_stale_audit_excluded": 1,
        "decision_followup_size": 1,
        "decision_followup_due": True,
        "decision_followup_attempted": 1,
        "decision_followup_succeeded": 1,
        "collection_lane": "active_research_1m",
        "safe_watchlist_tracked": 2,
        "priority_safe_tracked": 0,
        "active_market_ready_size": 1,
        "authentic_candles_refreshed": 42,
        "ohlcv_requests_attempted": 3,
        "ohlcv_requests_succeeded": 2,
        "ohlcv_cache_hits": 1,
        "ohlcv_request_budget_next": 6,
        "ohlcv_higher_budget_retry_after": None,
        "research_secondary_attempted": 1,
        "research_secondary_succeeded": 1,
        "ohlcv_empty_responses": 1,
        "ohlcv_stale_responses": 0,
        "ohlcv_retry_cooldown_size": 1,
        "rate_limited": True,
        "provider_error_count": 1,
        "provider_error_codes": {"pool_not_found": 1},
    }
    assert "sensitive" not in str(summary)
    expanded = PaperAutomationWorker._summarize(
        {
            "scan": {
                "authentic_candles_refreshed": 0,
                "ohlcv_error_codes": {"provider_http_error": 8, "sensitive provider response": 1},
                "ohlcv_request_budget_next": 8,
            }
        }
    )
    expanded_collection = expanded["market_collection"]
    assert isinstance(expanded_collection, dict)
    assert expanded_collection["provider_error_codes"] == {"provider_http_error": 8}
    assert expanded_collection["ohlcv_request_budget_next"] == 8

    narrative = PaperAutomationWorker._summarize(
        {
            "narrative": {
                "updated_at": "2026-11-03T17:00:00+00:00",
                "providers": {},
                "openai_research": {
                    "status": "degraded",
                    "error_code": "http_401",
                    "secret": "private-test-key",
                },
            }
        }
    )
    assert narrative["narrative"]["openai_research"]["error_code"] == "http_401"
    assert "private-test-key" not in str(narrative)


def test_worker_restores_and_persists_durable_state() -> None:
    saved: dict[str, object] = {}
    worker = PaperAutomationWorker(
        interval_seconds=30,
        cycle=lambda: {"paused": False, "scan": None, "decision": None, "exits": []},
        state_loader=lambda: {
            "state": "stopped",
            "cycles_completed": 4,
            "last_completed_at": "2026-11-02T12:00:00+00:00",
            "last_result": {"evaluated": 2},
        },
        state_saver=lambda payload: saved.update(payload),
    )

    assert worker.status()["cycles_completed"] == 4
    assert worker.status()["durable"] is True
    worker.run_once()
    assert saved["cycles_completed"] == 5
    assert saved["state"] == "stopped"


@pytest.mark.parametrize(
    ("from_start", "cycle_seconds", "expected_wait"),
    [(True, 20.0, 10.0), (True, 45.0, 1.0), (False, 20.0, 30.0)],
)
def test_worker_cycle_schedule_accounts_for_network_work(
    monkeypatch: pytest.MonkeyPatch,
    from_start: bool,
    cycle_seconds: float,
    expected_wait: float,
) -> None:
    waits: list[float] = []

    class StopAfterWait:
        def is_set(self) -> bool:
            return False

        def wait(self, seconds: float) -> bool:
            waits.append(seconds)
            return True

    ticks = iter((100.0, 100.0 + cycle_seconds))
    monkeypatch.setattr(automation.time, "monotonic", lambda: next(ticks))
    worker = PaperAutomationWorker(
        interval_seconds=30,
        cycle=lambda: {"paused": False, "scan": None, "decision": None, "exits": []},
        interval_from_start=from_start,
    )
    worker._stop_event = cast(Event, StopAfterWait())
    worker._loop()
    assert waits == [expected_wait]
    assert worker.status()["interval_from_start"] is from_start


def test_worker_persists_cycle_start_before_long_running_work() -> None:
    snapshots: list[dict[str, object]] = []

    def cycle() -> dict[str, object]:
        assert snapshots
        assert isinstance(snapshots[-1]["last_started_at"], str)
        assert snapshots[-1]["last_completed_at"] is None
        return {"paused": False, "scan": None, "decision": None, "exits": []}

    worker = PaperAutomationWorker(
        interval_seconds=30,
        cycle=cycle,
        state_saver=lambda payload: snapshots.append(dict(payload)),
    )
    worker.run_once()
    assert len(snapshots) == 2
    assert isinstance(snapshots[-1]["last_completed_at"], str)


def test_worker_start_cannot_overwrite_newer_cycle_start(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stale_save_entered = Event()
    release_stale_save = Event()
    cycle_started = Event()
    finish_cycle = Event()
    snapshots: list[dict[str, object]] = []

    def save(payload: dict[str, object]) -> None:
        if payload["last_started_at"] is None:
            stale_save_entered.set()
            assert release_stale_save.wait(3)
        snapshots.append(dict(payload))

    def cycle() -> dict[str, object]:
        cycle_started.set()
        assert finish_cycle.wait(3)
        return {"paused": False, "scan": None, "decision": None, "exits": []}

    worker = PaperAutomationWorker(interval_seconds=30, cycle=cycle, state_saver=save)
    original_loop = worker._loop

    def delayed_loop() -> None:
        stale_save_entered.wait(0.2)
        original_loop()

    monkeypatch.setattr(worker, "_loop", delayed_loop)
    starter = Thread(target=worker.start)
    starter.start()
    try:
        assert cycle_started.wait(3)
        release_stale_save.set()
        starter.join(timeout=3)
        assert not starter.is_alive()
        assert snapshots
        assert isinstance(snapshots[-1]["last_started_at"], str)
    finally:
        release_stale_save.set()
        finish_cycle.set()
        starter.join(timeout=3)
        worker.stop()


def test_worker_retries_transient_state_write_failure_before_cycle() -> None:
    attempts = 0
    saved: list[dict[str, object]] = []

    def save(payload: dict[str, object]) -> None:
        nonlocal attempts
        attempts += 1
        if attempts <= 2:
            raise sqlite3.OperationalError("database is locked")
        saved.append(dict(payload))

    def cycle() -> dict[str, object]:
        assert saved and isinstance(saved[0]["last_started_at"], str)
        return {"paused": False, "scan": None, "decision": None, "exits": []}

    worker = PaperAutomationWorker(interval_seconds=30, cycle=cycle, state_saver=save)
    worker.run_once()
    assert attempts == 4
    assert isinstance(saved[-1]["last_completed_at"], str)


def test_worker_restores_paused_state_without_running() -> None:
    worker = PaperAutomationWorker(
        interval_seconds=30,
        cycle=lambda: {"paused": False},
        state_loader=lambda: {"state": "paused", "cycles_completed": 3},
    )
    assert worker.status()["state"] == "paused"
    assert worker.status()["running"] is False
