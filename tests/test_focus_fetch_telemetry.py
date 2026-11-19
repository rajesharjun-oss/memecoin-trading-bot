from __future__ import annotations

import sqlite3
import time
from pathlib import Path

import pytest
from solders.pubkey import Pubkey

from memecoin_bot import telemetry
from memecoin_bot.journal import TradeJournal
from memecoin_bot.market_data import TokenMarket
from memecoin_bot.ohlcv import OhlcvError, OhlcvRateLimited, PoolCandle
from memecoin_bot.strategy_lab import collect_market_snapshots


def test_focus_fetch_storage_is_bounded_aggregate_and_report_is_read_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "research.sqlite3"
    recorder = telemetry.HourlyTelemetry(path)
    monkeypatch.setattr(telemetry, "_recorder", recorder)
    hour = int(time.time()) // 3_600 * 3_600 - 3_600
    for activity in telemetry.FOCUS_FETCH_ACTIVITY:
        for outcome in telemetry.FOCUS_FETCH_OUTCOMES:
            recorder.add_focus_fetch(activity, outcome, at=hour)
    recorder.add_focus_fetch("eight_plus_5m", "late_bar", at=hour)
    recorder.add_focus_fetch("unknown", "empty", at=hour - 8 * 86_400)
    recorder.flush()
    with sqlite3.connect(path) as connection:
        rows = connection.execute(
            "SELECT activity, outcome, count FROM telemetry_focus_fetch_hourly"
        ).fetchall()
        schema = connection.execute(
            "SELECT sql FROM sqlite_master WHERE name='telemetry_focus_fetch_hourly'"
        ).fetchone()
    assert len(rows) == len(telemetry.FOCUS_FETCH_ACTIVITY) * len(telemetry.FOCUS_FETCH_OUTCOMES)
    assert ("eight_plus_5m", "late_bar", 2) in rows
    assert schema is not None
    assert "https://" not in repr((rows, schema))
    assert "api-key" not in repr((rows, schema))
    assert "mint" not in repr((rows, schema))
    report = telemetry.telemetry_report(path, 24)
    diagnostics = report["diagnostics"]
    assert isinstance(diagnostics, dict)
    focus = diagnostics["focus_fetch"]
    assert diagnostics["focus_fetch_available"] is True
    assert isinstance(focus, dict)
    assert focus["eight_plus_5m"]["late_bar"] == 2
    assert recorder._pending_focus_fetch == {}


def test_focus_fetch_classification_and_failure_isolation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    recorder = telemetry.HourlyTelemetry(tmp_path / "paper.sqlite3")
    monkeypatch.setattr(telemetry, "_recorder", recorder)
    for value in (0, 1, 7, 8, None, -1):
        telemetry.record_focus_fetch(value, "recent_bar")
    assert {
        (activity, outcome): count
        for (_hour, activity, outcome), count in recorder._pending_focus_fetch.items()
    } == {
        ("zero_5m", "recent_bar"): 1,
        ("one_to_seven_5m", "recent_bar"): 2,
        ("eight_plus_5m", "recent_bar"): 1,
        ("unknown", "recent_bar"): 2,
    }
    with pytest.raises(ValueError, match="Invalid focus fetch counter"):
        recorder.add_focus_fetch("mint-address", "recent_bar")
    with pytest.raises(ValueError, match="Invalid focus fetch counter"):
        recorder.add_focus_fetch("unknown", "secret-value")

    def fail(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("private-token-must-not-log")

    monkeypatch.setattr(recorder, "add_focus_fetch", fail)
    telemetry.record_focus_fetch(8, "recent_bar")
    assert "RuntimeError" in caplog.text
    assert "private-token-must-not-log" not in caplog.text


def test_focus_fetch_flush_retries_and_legacy_report_needs_no_migration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    legacy_path = tmp_path / "legacy.sqlite3"
    legacy = telemetry.telemetry_report(legacy_path, 24)
    diagnostics = legacy["diagnostics"]
    assert isinstance(diagnostics, dict)
    assert diagnostics["focus_fetch_available"] is False
    assert not legacy_path.exists()

    recorder = telemetry.HourlyTelemetry(tmp_path / "new.sqlite3")
    recorder.add_focus_fetch("zero_5m", "empty")

    def fail_connect(*_args: object, **_kwargs: object) -> sqlite3.Connection:
        raise sqlite3.OperationalError("private-token-must-not-log")

    with monkeypatch.context() as patcher:
        patcher.setattr(sqlite3, "connect", fail_connect)
        recorder.flush()
    assert sum(recorder._pending_focus_fetch.values()) == 1
    assert "private-token-must-not-log" not in caplog.text
    recorder.flush()
    assert recorder._pending_focus_fetch == {}
    with sqlite3.connect(recorder.state_path) as connection:
        assert connection.execute(
            "SELECT count FROM telemetry_focus_fetch_hourly "
            "WHERE activity='zero_5m' AND outcome='empty'"
        ).fetchone() == (1,)


@pytest.mark.parametrize(
    ("mode", "expected"),
    [
        ("recent", "recent_bar"),
        ("late", "late_bar"),
        ("empty", "empty"),
        ("error", "request_error"),
        ("quota", "rate_limited"),
        ("backoff", "not_attempted"),
    ],
)
def test_focused_collection_records_fetch_outcome_without_changing_decision(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
    expected: str,
) -> None:
    mint = str(Pubkey.new_unique())
    pool = str(Pubkey.new_unique())
    observed: list[tuple[int | None, str]] = []

    class FakeDex:
        def __enter__(self) -> FakeDex:
            return self

        def __exit__(self, *_: object) -> None:
            return None

        def tokens(self, requested: list[str]) -> dict[str, TokenMarket]:
            if mint not in requested:
                return {}
            return {
                mint: TokenMarket(
                    mint=mint,
                    name=None,
                    symbol=None,
                    dex_id="pumpswap",
                    pair_address=pool,
                    price_usd="1",
                    liquidity_usd="50000",
                    volume_24h_usd="100000",
                    price_change_24h="1",
                    pair_created_at=None,
                    volume_5m_usd="1000",
                    txns_5m_buys=8,
                )
            }

    class FakeOhlcv:
        def __enter__(self) -> FakeOhlcv:
            return self

        def __exit__(self, *_: object) -> None:
            return None

        def candles(self, token: str, pair: str, frame: str) -> list[PoolCandle]:
            if mode == "error":
                raise OhlcvError("provider unavailable")
            if mode == "quota":
                raise OhlcvRateLimited("quota", retry_after_seconds=90)
            if mode == "empty":
                return []
            opened_at = 60 if mode == "late" else (int(time.time()) // 60 - 2) * 60
            return [PoolCandle(token, pair, frame, opened_at, "1", "1", "1", "1", "100")]

    monkeypatch.setattr("memecoin_bot.strategy_lab.DexScreenerClient", FakeDex)
    monkeypatch.setattr("memecoin_bot.strategy_lab.GeckoTerminalOhlcvClient", FakeOhlcv)
    monkeypatch.setattr(
        "memecoin_bot.strategy_lab.record_focus_fetch",
        lambda txns, outcome: observed.append((txns, outcome)),
    )
    with TradeJournal(tmp_path / f"{mode}.sqlite3") as journal:
        journal.upsert_discovery_candidate(
            mint=mint,
            source_program="test",
            audit_status="safe",
            audit_summary=None,
            top10_concentration_bps=None,
            decimals=6,
            market={"pair_address": pool},
        )
        if mode == "backoff":
            journal.set_runtime_state("ohlcv_backoff", {"until": int(time.time()) + 120})
        result = collect_market_snapshots(
            journal, active_candidate_priority=lambda candidate: candidate["mint"] == mint
        )
    assert result["strategy_focus_scheduled"] == 1
    assert observed == [(8, expected)]
