from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

import pytest

from memecoin_bot.journal import TradeJournal
from memecoin_bot.opportunity import (
    decision_price_path,
    snapshot_decision_price_change,
    summarize_decision_followup_paths,
    summarize_recent_price_paths,
    summarize_snapshot_decision_paths,
)
from memecoin_bot.strategy_lab import Candle

BASE = 1_700_000_000 - 1_700_000_000 % 60


def _decision(*, pool: str = "pool") -> dict[str, object]:
    return {
        "decision_id": "one",
        "mint": "mint",
        "created_at": datetime.fromtimestamp(BASE + 15, UTC).isoformat(),
        "action": "rejected",
        "reasons": ["blocked by candle"],
        "evidence": {"pool": pool, "market_gate_reasons": ["volume below minimum"]},
    }


def _candles(count: int = 15) -> list[Candle]:
    return [
        Candle(
            opened_at=BASE + 60 + index * 60,
            open=1.0,
            high=1.1 if index == 2 else 1.02,
            low=0.9 if index == 3 else 0.99,
            close=1.01,
            volume_delta_usd=100,
            liquidity_usd=50_000,
            samples=1,
            source="geckoterminal_pool_trades",
            first_fetched_at=BASE + 60 + index * 60 + 80,
            first_observed_values_at=BASE + 60 + index * 60 + 80,
        )
        for index in range(count)
    ]


def _snapshot(at: int, price: str, *, pool: str = "pool") -> dict[str, object]:
    return {
        "captured_at": at,
        "observed_at": at,
        "pool": pool,
        "price_usd": price,
    }


def test_snapshot_price_change_uses_predecision_price_and_bounded_future_capture() -> None:
    decision = _decision()
    as_of = BASE + 15 + 5 * 60 + 120
    snapshots = [
        _snapshot(BASE + 10, "1"),
        _snapshot(BASE + 20, "100"),
        _snapshot(BASE + 330, "1.2"),
    ]
    assert snapshot_decision_price_change(decision, snapshots, horizon_minutes=5, as_of=as_of) == {
        "status": "observed_snapshot_proxy",
        "horizon_minutes": 5,
        "entry_captured_at": BASE + 10,
        "target_captured_at": BASE + 330,
        "entry_age_seconds": 5,
        "target_delay_seconds": 15,
        "price_change_pct": 20.0,
    }
    assert (
        snapshot_decision_price_change(decision, snapshots, horizon_minutes=5, as_of=BASE + 300)[
            "status"
        ]
        == "pending_horizon"
    )
    assert (
        snapshot_decision_price_change(decision, snapshots[1:], horizon_minutes=5, as_of=as_of)[
            "status"
        ]
        == "entry_snapshot_missing"
    )
    assert (
        snapshot_decision_price_change(
            decision,
            [_snapshot(BASE + 15, "1"), _snapshot(BASE + 330, "1.2")],
            horizon_minutes=5,
            as_of=as_of,
        )["status"]
        == "entry_snapshot_missing"
    )


def test_snapshot_price_change_rejects_stale_wrong_pool_and_missing_outcomes() -> None:
    decision = _decision()
    as_of = BASE + 15 + 5 * 60 + 120
    target = _snapshot(BASE + 330, "1.2")
    assert (
        snapshot_decision_price_change(
            decision, [_snapshot(BASE - 200, "1"), target], horizon_minutes=5, as_of=as_of
        )["status"]
        == "entry_snapshot_stale"
    )
    assert (
        snapshot_decision_price_change(
            decision,
            [_snapshot(BASE + 10, "1"), _snapshot(BASE + 200, "1", pool="other"), target],
            horizon_minutes=5,
            as_of=as_of,
        )["status"]
        == "pool_changed"
    )
    assert (
        snapshot_decision_price_change(
            decision, [_snapshot(BASE + 10, "1")], horizon_minutes=5, as_of=as_of
        )["status"]
        == "target_snapshot_missing"
    )
    assert (
        snapshot_decision_price_change(
            decision,
            [_snapshot(BASE + 10, "1"), _snapshot(BASE + 440, "1.2")],
            horizon_minutes=5,
            as_of=BASE + 600,
        )["status"]
        == "target_snapshot_late"
    )


def test_snapshot_summary_keeps_missing_decisions_in_denominator(tmp_path: Path) -> None:
    with TradeJournal(tmp_path / "snapshot-paths.sqlite3") as journal:
        for at, price in ((BASE + 10, "1"), (BASE + 330, "1.2"), (BASE + 930, "0.8")):
            journal.record_market_snapshot(
                mint="mint",
                pool="pool",
                price_usd=price,
                liquidity_usd="50000",
                volume_24h_usd="100000",
                observed_at=at,
            )
        missing = {**_decision(pool=""), "decision_id": "two", "mint": "other"}
        result = summarize_snapshot_decision_paths(
            journal,
            {5: [_decision(), missing], 15: [_decision(), missing]},
            as_of=BASE + 1_100,
        )
    assert result["horizons"]["5"]["decision_count"] == 2
    assert result["horizons"]["5"]["statuses"] == {
        "observed_snapshot_proxy": 1,
        "missing_pool": 1,
    }
    assert result["horizons"]["15"]["statuses"] == {
        "observed_snapshot_proxy": 1,
        "missing_pool": 1,
    }
    assert result["horizons"]["5"]["observed_final_reasons"] == {"blocked by candle": 1}
    assert result["horizons"]["5"]["final_reason_coverage"] == {
        "blocked by candle": {"decisions": 2, "observed": 1, "coverage_pct": 50.0}
    }


def test_snapshot_summary_attributes_rejection_to_market_gate(tmp_path: Path) -> None:
    decision = {
        **_decision(),
        "verdict": "reject",
        "reasons": ["liquidity is unavailable", "no token-linked narrative evidence (+0)"],
        "evidence": {"pool": "pool", "market_gate_reasons": ["liquidity is unavailable"]},
    }
    with TradeJournal(tmp_path / "rejection-reasons.sqlite3") as journal:
        result = summarize_snapshot_decision_paths(journal, {5: [decision]}, as_of=BASE + 600)
    row = result["horizons"]["5"]["rows"][0]
    assert row["final_reason"] == "liquidity is unavailable"
    assert (
        "no token-linked narrative evidence (+0)"
        not in result["horizons"]["5"]["final_reason_coverage"]
    )


def test_future_price_path_starts_strictly_after_saved_decision() -> None:
    past = replace(_candles(1)[0], opened_at=BASE, high=100.0)
    result = decision_price_path(
        _decision(),
        [past, *_candles()],
        horizon_minutes=5,
        as_of=BASE + 60 + 15 * 60 + 120,
    )
    assert result == {
        "status": "observed_timely",
        "proxy_entry_at": BASE + 60,
        "horizon_minutes": 5,
        "max_capture_delay_seconds": 20,
        "max_favorable_pct": 10.0,
        "max_adverse_pct": -10.0,
        "horizon_close_pct": 1.0,
    }


def test_future_price_path_censors_gaps_and_labels_backfilled_history() -> None:
    decision = _decision()
    as_of = BASE + 60 + 15 * 60 + 120
    assert (
        decision_price_path(decision, _candles(), horizon_minutes=5, as_of=BASE + 100)["status"]
        == "pending_horizon"
    )
    assert (
        decision_price_path(decision, _candles()[1:], horizon_minutes=5, as_of=as_of)["status"]
        == "entry_bar_missing"
    )
    assert (
        decision_price_path(
            decision, [_candles()[0], *_candles()[2:]], horizon_minutes=5, as_of=as_of
        )["status"]
        == "future_bar_gap"
    )
    late = _candles()
    late[2] = replace(late[2], first_observed_values_at=late[2].opened_at + 300)
    assert (
        decision_price_path(decision, late, horizon_minutes=5, as_of=as_of)["status"]
        == "observed_backfilled"
    )
    untrusted = _candles()
    untrusted[2] = replace(untrusted[2], first_observed_values_at=None)
    assert (
        decision_price_path(decision, untrusted, horizon_minutes=5, as_of=as_of)["status"]
        == "untrusted_capture"
    )
    assert (
        decision_price_path(_decision(pool=""), _candles(), horizon_minutes=5, as_of=as_of)[
            "status"
        ]
        == "missing_pool"
    )


def test_future_path_summary_keeps_missing_candidates_in_denominator(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "memecoin_bot.opportunity.load_verified_candles",
        lambda *_args, **_kwargs: _candles(),
    )
    missing = {**_decision(pool=""), "decision_id": "two", "mint": "other"}
    result = summarize_recent_price_paths(
        cast(TradeJournal, object()),
        {5: [_decision(), missing], 15: [_decision(), missing]},
        as_of=BASE + 60 + 15 * 60 + 120,
    )
    assert result["horizons"]["5"]["decision_count"] == 2
    assert result["horizons"]["5"]["unique_mints"] == 2
    assert result["horizons"]["5"]["rows"][0]["mint"] == "mint"
    assert result["horizons"]["5"]["statuses"] == {"observed_timely": 1, "missing_pool": 1}
    assert result["horizons"]["5"]["observed_final_reasons"] == {"blocked by candle": 1}
    assert result["horizons"]["15"]["statuses"] == {"observed_timely": 1, "missing_pool": 1}


def test_followup_summary_uses_only_pool_snapshot_known_at_selection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    requested: list[tuple[str, str]] = []

    def load(
        _journal: TradeJournal, mint: str, pool: str, _frame: str, **_: object
    ) -> list[Candle]:
        requested.append((mint, pool))
        return _candles()

    monkeypatch.setattr("memecoin_bot.opportunity.load_verified_candles", load)
    with TradeJournal(tmp_path / "followup.sqlite3") as journal:
        for mint in ("known", "missing", "stale"):
            journal.record_decision_followup_selection(
                mint,
                selected_at=BASE + 20,
                expires_at=BASE + 1_820,
                population_count=3,
                method="test",
            )
        journal.record_market_snapshot(
            mint="known",
            pool="original-pool",
            price_usd="1",
            liquidity_usd="50000",
            volume_24h_usd="100000",
            observed_at=BASE + 10,
        )
        journal.record_market_snapshot(
            mint="known",
            pool="future-pool",
            price_usd="2",
            liquidity_usd="50000",
            volume_24h_usd="100000",
            observed_at=BASE + 70,
        )
        journal.record_market_snapshot(
            mint="stale",
            pool="old-pool",
            price_usd="1",
            liquidity_usd="50000",
            volume_24h_usd="100000",
            observed_at=BASE - 300,
        )
        result = summarize_decision_followup_paths(journal, as_of=BASE + 60 + 15 * 60 + 120)
    assert result["sample_size"] == 3
    assert requested == [("known", "original-pool")]
    assert result["statuses"] == {
        "5m": {
            "observed_timely": 1,
            "pool_snapshot_unavailable_at_selection": 1,
            "pool_snapshot_stale_at_selection": 1,
        },
        "15m": {
            "observed_timely": 1,
            "pool_snapshot_unavailable_at_selection": 1,
            "pool_snapshot_stale_at_selection": 1,
        },
    }
    assert result["statuses_by_method"] == {"test": result["statuses"]}
