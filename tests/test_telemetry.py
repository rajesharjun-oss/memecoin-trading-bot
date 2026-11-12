from __future__ import annotations

import sqlite3
import time
from collections import Counter
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest

from memecoin_bot import telemetry
from memecoin_bot.cli import build_parser


def _recorder(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> telemetry.HourlyTelemetry:
    recorder = telemetry.HourlyTelemetry(tmp_path / "paper.sqlite3")
    monkeypatch.setattr(telemetry, "_recorder", recorder)
    return recorder


def _counts(recorder: telemetry.HourlyTelemetry) -> Counter[tuple[str, str]]:
    output: Counter[tuple[str, str]] = Counter()
    for (_hour, metric, outcome), count in recorder._pending.items():
        output[(metric, outcome)] += count
    return output


def test_hourly_aggregation_pruning_and_secret_free_storage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    recorder = _recorder(tmp_path, monkeypatch)
    now = int(time.time())
    recorder.add("ohlcv", "ok", 2, at=now)
    recorder.add("ohlcv", "ok", 3, at=now + 2)
    recorder.add("ohlcv", "http_429", at=now)
    recorder.add("rpc", "timeout", at=now - 8 * 86_400)
    recorder.flush()
    with sqlite3.connect(recorder.state_path) as connection:
        rows = connection.execute(
            "SELECT hour_start, metric, outcome, count FROM telemetry_hourly ORDER BY outcome"
        ).fetchall()
        schema = connection.execute(
            "SELECT sql FROM sqlite_master WHERE name = 'telemetry_hourly'"
        ).fetchone()
    assert len(rows) == 2
    assert {(row[1], row[2], row[3]) for row in rows} == {
        ("ohlcv", "ok", 5),
        ("ohlcv", "http_429", 1),
    }
    assert schema is not None
    stored = repr((rows, schema))
    assert "https://" not in stored
    assert "api-key" not in stored
    assert "token" not in stored.lower()
    assert recorder._pending == Counter()


def test_flush_failure_does_not_discard_counts_or_raise(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    recorder = _recorder(tmp_path, monkeypatch)
    recorder.add("ohlcv", "ok")

    def fail_connect(*_args: object, **_kwargs: object) -> sqlite3.Connection:
        raise sqlite3.OperationalError("synthetic failure")

    with monkeypatch.context() as patcher:
        patcher.setattr(sqlite3, "connect", fail_connect)
        recorder.flush()
    assert sum(recorder._pending.values()) == 1
    recorder.flush()
    assert recorder._pending == Counter()


def test_http_classification_and_quote_only_filter(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    recorder = _recorder(tmp_path, monkeypatch)

    def respond(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/timeout":
            raise httpx.ReadTimeout("synthetic timeout", request=request)
        return httpx.Response(int(request.url.path.removeprefix("/")))

    with telemetry.http_client("ohlcv", transport=httpx.MockTransport(respond)) as client:
        for status in (200, 429, 503, 400):
            client.get(f"https://feed.example.test/{status}")
        with pytest.raises(httpx.ReadTimeout):
            client.get("https://feed.example.test/timeout")
    with telemetry.http_client(
        "jupiter_quote",
        transport=httpx.MockTransport(lambda _request: httpx.Response(200)),
    ) as client:
        client.get("https://api.example.test/swap/v2/build")
        client.get("https://api.example.test/swap/v2/order")
        client.post("https://api.example.test/swap/v2/execute")
    counts = _counts(recorder)
    assert [counts[("ohlcv", outcome)] for outcome in telemetry.HTTP_OUTCOMES] == [1, 1, 1, 1, 1]
    assert counts[("jupiter_quote", "ok")] == 2


def test_candidate_exit_and_market_cycle_counters(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    recorder = _recorder(tmp_path, monkeypatch)
    ready = {frame: {"status": "ready"} for frame in ("1m", "5m", "15m", "1h")}
    stale = {**ready, "5m": {"status": "stale"}}
    telemetry.record_candidate_evaluations(
        [
            {"market_gate_verdict": "eligible", "candle_signals": ready},
            {"market_gate_verdict": "eligible", "candle_signals": stale},
            {"market_gate_verdict": "eligible"},
            {"market_gate_verdict": "reject"},
        ]
    )
    now = datetime.now(UTC)
    telemetry.record_exit_marks(
        [
            {"status": "open", "quote_state": "live"},
            {
                "status": "open",
                "quote_state": "stale",
                "last_quoted_at": (now - timedelta(seconds=150)).isoformat(),
            },
            {
                "status": "open",
                "quote_state": "stale",
                "last_quoted_at": (now - timedelta(seconds=300)).isoformat(),
            },
            {"status": "closed"},
        ],
    )
    telemetry.record_mark_quote_attempt()
    telemetry.record_mark_quote_success()
    telemetry.record_market_cycle(3, 2, 1, 30)
    counts = _counts(recorder)
    assert [counts[("candles", outcome)] for outcome in ("fresh", "stale", "missing")] == [
        1,
        1,
        1,
    ]
    assert counts[("exit_marks", "fresh")] == 1
    assert counts[("exit_marks", "missed")] == 2
    assert counts[("mark_quotes", "attempted")] == 1
    assert counts[("mark_quotes", "succeeded")] == 1
    assert counts[("capacity", "ready_mint_seconds")] == 90
    assert counts[("capacity", "successful_calls")] == 1


def test_report_pass_fail_undefined_and_exact_window(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    recorder = _recorder(tmp_path, monkeypatch)
    end = int(time.time()) // 3_600 * 3_600
    for hour in range(end - 24 * 3_600, end, 3_600):
        recorder.add("capacity", "cycles", at=hour)
        recorder.add("capacity", "observed_cycle_seconds", 3_600, at=hour)
        recorder.add("capacity", "ready_mint_seconds", 120, at=hour)
        recorder.add("capacity", "successful_calls", at=hour)
        recorder.add("candles", "fresh", 19, at=hour)
        recorder.add("candles", "missing", at=hour)
        for endpoint in telemetry.ENDPOINTS:
            recorder.add(endpoint, "ok", 21, at=hour)
    recorder.flush()
    report = telemetry.telemetry_report(recorder.state_path, 24, now=end)
    assert report["window_start_utc"] == datetime.fromtimestamp(end - 86_400, UTC).isoformat()
    assert report["window_end_utc"] == datetime.fromtimestamp(end, UTC).isoformat()
    assert report["window_complete"] is True
    assert report["overall"] == "undefined"
    assert len(report["hourly"]) == 24  # type: ignore[arg-type]  # Typed JSON list.
    assert report["ohlcv_capacity"]["average_needed_calls_per_cycle"] == 1  # type: ignore[index]  # Typed JSON dict.
    assert report["gates"]["exit_marks"] == "undefined"  # type: ignore[index]  # Typed JSON dict.
    assert report["candidate_candles"]["skipped_missing_or_stale"] == 24  # type: ignore[index]  # Typed JSON dict.

    recorder.add("exit_marks", "fresh", 19, at=end - 3_600)
    recorder.add("exit_marks", "missed", at=end - 3_600)
    recorder.add("mark_quotes", "attempted", 20, at=end - 3_600)
    recorder.add("mark_quotes", "succeeded", 19, at=end - 3_600)
    recorder.add("ohlcv", "http_429", at=end - 3_600)
    recorder.flush()
    report = telemetry.telemetry_report(recorder.state_path, 24, now=end)
    assert report["overall"] == "pass"
    recorder.add("capacity", "ready_mint_seconds", 120, at=end - 3_600)
    recorder.add("candles", "missing", at=end - 3_600)
    recorder.add("exit_marks", "missed", at=end - 3_600)
    recorder.add("mark_quotes", "attempted", at=end - 3_600)
    recorder.add("ohlcv", "http_429", 30, at=end - 3_600)
    recorder.flush()
    report = telemetry.telemetry_report(recorder.state_path, 24, now=end)
    assert report["overall"] == "fail"
    assert report["gates"]["ohlcv_errors"] == "fail"  # type: ignore[index]  # Typed JSON dict.
    assert report["gates"]["candidate_candles"] == "fail"  # type: ignore[index]  # Typed JSON dict.
    assert report["gates"]["exit_marks"] == "fail"  # type: ignore[index]  # Typed JSON dict.
    assert report["gates"]["mark_quote_success"] == "fail"  # type: ignore[index]  # Typed JSON dict.
    assert report["gates"]["ohlcv_capacity"] == "fail"  # type: ignore[index]  # Typed JSON dict.


def test_report_is_read_only_and_rejects_bad_hours(tmp_path: Path) -> None:
    state_path = tmp_path / "missing.sqlite3"
    report = telemetry.telemetry_report(state_path, 24)
    assert report["overall"] == "insufficient_window"
    assert not state_path.exists()
    with pytest.raises(ValueError, match="between 1 and 48"):
        telemetry.telemetry_report(state_path, 0)


def test_telemetry_is_off_without_paper_worker_and_cli_is_registered(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(telemetry, "_recorder", None)
    with telemetry.http_client("rpc") as client:
        assert type(client) is httpx.Client
    args = build_parser().parse_args(["telemetry-report", "--hours", "24"])
    assert args.hours == 24
