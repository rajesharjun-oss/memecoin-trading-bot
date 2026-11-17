from __future__ import annotations

import sqlite3
import time
from collections import Counter
from pathlib import Path
from typing import Any, cast

import httpx
import pytest

from memecoin_bot import telemetry


def _recorder(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> telemetry.HourlyTelemetry:
    recorder = telemetry.HourlyTelemetry(tmp_path / "paper.sqlite3")
    monkeypatch.setattr(telemetry, "_recorder", recorder)
    return recorder


def _diagnostic_counts(
    recorder: telemetry.HourlyTelemetry,
) -> Counter[tuple[str, str, str]]:
    counts: Counter[tuple[str, str, str]] = Counter()
    for (_hour, category, label, outcome), count in recorder._pending_diagnostics.items():
        counts[(category, label, outcome)] += count
    return counts


def test_rpc_methods_are_counted_without_storing_request_data(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    recorder = _recorder(tmp_path, monkeypatch)

    def respond(request: httpx.Request) -> httpx.Response:
        payload = request.content
        return httpx.Response(429 if b"getMultipleAccounts" in payload else 200)

    with telemetry.http_client("rpc", transport=httpx.MockTransport(respond)) as client:
        for method in ("getMultipleAccounts", "getTransaction", "unrecognizedMethod"):
            client.post(
                "https://rpc.example.test/?api-key=DO_NOT_STORE",
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": method,
                    "params": [{"sentinel": "DO_NOT_STORE"}],
                },
            )
    counts = _diagnostic_counts(recorder)
    assert counts[("rpc_method", "getMultipleAccounts", "http_429")] == 1
    assert counts[("rpc_method", "getTransaction", "ok")] == 1
    assert counts[("rpc_method", "other", "ok")] == 1
    recorder.flush()
    with sqlite3.connect(recorder.state_path) as connection:
        rows = connection.execute(
            "SELECT category, label, outcome, count FROM telemetry_diagnostics_hourly"
        ).fetchall()
        schema = connection.execute(
            "SELECT sql FROM sqlite_master WHERE name = 'telemetry_diagnostics_hourly'"
        ).fetchone()
    stored = repr((rows, schema))
    assert "DO_NOT_STORE" not in stored
    assert "https://" not in stored
    assert "params" not in stored
    assert recorder._pending_diagnostics == Counter()


def test_focus_candle_diagnostics_are_bounded_and_expired_focus_is_ignored(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    recorder = _recorder(tmp_path, monkeypatch)
    now = int(time.time())
    rows: list[dict[str, object]] = [
        {
            "market_gate_verdict": "eligible",
            "market_gate_inputs": {"mint": "pinned"},
            "candle_signals": {"1m": {"status": "ready"}, "5m": {"status": "gap"}},
        },
        {
            "market_gate_verdict": "reject",
            "market_gate_inputs": {"mint": "pinned"},
        },
        {
            "market_gate_verdict": "eligible",
            "market_gate_inputs": {"mint": "not-pinned"},
            "candle_signals": {"1m": {"status": "stale"}, "5m": {"status": "ready"}},
        },
    ]
    telemetry.record_candidate_evaluations(
        rows, focus_state_loader=lambda: {"selected_at": now, "mints": ["pinned"]}
    )
    telemetry.record_focus_schedule(2, 1)
    telemetry.record_focus_schedule(1, 2)  # Invalid counts cannot enter storage.
    counts = _diagnostic_counts(recorder)
    assert counts[("focus_market", "eligible", "count")] == 1
    assert counts[("focus_market", "ineligible", "count")] == 1
    assert counts[("focus_candle", "1m", "ready")] == 1
    assert counts[("focus_candle", "5m", "gap")] == 1
    assert counts[("focus_schedule", "cycles", "count")] == 1
    assert counts[("focus_schedule", "selected", "count")] == 2
    assert counts[("focus_schedule", "scheduled", "count")] == 1

    telemetry.record_candidate_evaluations(
        rows, focus_state_loader=lambda: {"selected_at": now - 3_601, "mints": ["pinned"]}
    )
    assert _diagnostic_counts(recorder) == counts


def test_diagnostic_failures_cannot_change_network_or_candidate_flow(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    recorder = _recorder(tmp_path, monkeypatch)

    def fail_diagnostic(*_args: object, **_kwargs: object) -> None:
        raise ValueError("DO_NOT_LOG")

    monkeypatch.setattr(recorder, "add_diagnostic", fail_diagnostic)
    with telemetry.http_client(
        "rpc", transport=httpx.MockTransport(lambda _request: httpx.Response(429))
    ) as client:
        response = client.post(
            "https://rpc.example.test/?api-key=DO_NOT_LOG",
            json={"jsonrpc": "2.0", "id": 1, "method": "getBalance", "params": []},
        )
    assert response.status_code == 429
    assert "DO_NOT_LOG" not in caplog.text
    assert recorder._pending[(telemetry._hour(int(time.time())), "rpc", "http_429")] == 1

    telemetry.record_candidate_evaluations(
        [{"market_gate_verdict": "eligible", "candle_signals": {}}],
        focus_state_loader=lambda: (_ for _ in ()).throw(ValueError("DO_NOT_LOG")),
    )
    assert "DO_NOT_LOG" not in caplog.text
    assert (
        sum(
            count
            for (_hour, metric, _outcome), count in recorder._pending.items()
            if metric == "candles"
        )
        == 1
    )


def test_diagnostic_report_and_prune_do_not_change_readiness_gates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    recorder = _recorder(tmp_path, monkeypatch)
    end = int(time.time()) // 3_600 * 3_600
    recorder.add_diagnostic("rpc_method", "getTransaction", "http_429", at=end - 3_600)
    recorder.add_diagnostic("rpc_method", "getTransaction", "ok", 3, at=end - 3_600)
    recorder.add_diagnostic("focus_candle", "1m", "gap", 2, at=end - 3_600)
    recorder.add_diagnostic("focus_market", "eligible", "count", 2, at=end - 3_600)
    recorder.add_diagnostic("focus_schedule", "cycles", "count", at=end - 3_600)
    recorder.add_diagnostic("focus_schedule", "selected", "count", 2, at=end - 3_600)
    recorder.add_diagnostic("focus_schedule", "scheduled", "count", at=end - 3_600)
    recorder.add_diagnostic("rpc_method", "getBalance", "ok", at=end - 8 * 86_400)
    recorder.flush()
    report = telemetry.telemetry_report(recorder.state_path, 24, now=end)
    diagnostics = cast(dict[str, Any], report["diagnostics"])
    assert diagnostics["available"] is True
    assert diagnostics["observed_hours"] == 1
    assert diagnostics["rpc_methods"]["getTransaction"]["attempts"] == 4
    assert diagnostics["rpc_methods"]["getTransaction"]["http_429"] == 1
    assert diagnostics["rpc_methods"]["getBalance"]["attempts"] == 0
    assert diagnostics["focused_candles"]["1m"]["gap"] == 2
    assert diagnostics["focused_evaluations"]["eligible"] == 2
    assert diagnostics["focus_schedule"] == {"cycles": 1, "selected": 2, "scheduled": 1}
    assert report["overall"] == "insufficient_window"
    with sqlite3.connect(recorder.state_path) as connection:
        count = connection.execute(
            "SELECT COUNT(*) FROM telemetry_diagnostics_hourly WHERE label = 'getBalance'"
        ).fetchone()
    assert count == (0,)


def test_report_accepts_journal_without_diagnostics_table(tmp_path: Path) -> None:
    state_path = tmp_path / "old.sqlite3"
    with sqlite3.connect(state_path) as connection:
        connection.execute(
            "CREATE TABLE telemetry_hourly (hour_start INTEGER, metric TEXT, "
            "outcome TEXT, count INTEGER)"
        )
    report = telemetry.telemetry_report(state_path, 24)
    diagnostics = cast(dict[str, Any], report["diagnostics"])
    assert diagnostics["available"] is False
    assert diagnostics["observed_hours"] == 0
    assert diagnostics["rpc_methods"]["other"]["attempts"] == 0
    with sqlite3.connect(state_path) as connection:
        missing = connection.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE name = 'telemetry_diagnostics_hourly'"
        ).fetchone()
    assert missing == (0,)
