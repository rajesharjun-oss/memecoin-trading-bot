from __future__ import annotations

import json
import re
from dataclasses import replace

from memecoin_bot.evaluation import run_walk_forward_evaluation
from memecoin_bot.strategy_lab import Candle, Timeframe


def _candle() -> Candle:
    return Candle(
        opened_at=1_700_000_000,
        open=1.0,
        high=1.2,
        low=0.9,
        close=1.1,
        volume_delta_usd=100.0,
        liquidity_usd=75_000.0,
        samples=1,
        source="geckoterminal_pool_trades",
        first_fetched_at=1_700_000_061,
        first_observed_values_at=1_700_000_061,
    )


def _provenance(
    candle: Candle,
    frames: dict[Timeframe, list[Candle]] | None = None,
) -> dict[str, object]:
    result = run_walk_forward_evaluation(
        [candle],
        minimum_liquidity_usd=25_000,
        tp_levels_bps=(2_000,),
        tp_sell_bps=(5_000,),
        stop_loss_bps=2_500,
        execution_cost_bps=100,
        confirmation_frames=frames,
    )
    assert result["status"] == "insufficient_history"
    provenance = result["ohlcv_provenance"]
    assert isinstance(provenance, dict)
    return provenance


def test_walk_forward_fingerprints_candle_values_and_observation_times() -> None:
    candle = _candle()
    original = _provenance(candle)
    assert original == _provenance(candle)
    entry = original["entry"]
    assert isinstance(entry, dict)
    assert entry["count"] == 1
    assert re.fullmatch(r"[0-9a-f]{64}", str(entry["sha256"]))
    assert _provenance(replace(candle, close=1.15))["entry"] != entry
    assert _provenance(replace(candle, first_fetched_at=1_700_000_062))["entry"] != entry
    assert "volume_delta_usd" not in json.dumps(original)


def test_walk_forward_fingerprints_confirmation_frames_independent_of_mapping_order() -> None:
    candle = _candle()
    five = replace(candle, opened_at=1_699_999_800)
    fifteen = replace(candle, opened_at=1_699_999_400)
    first = _provenance(candle, {"15m": [fifteen], "5m": [five]})
    second = _provenance(candle, {"5m": [five], "15m": [fifteen]})
    assert first == second
    confirmation = first["confirmation"]
    assert isinstance(confirmation, dict)
    assert set(confirmation) == {"5m", "15m"}
    changed = _provenance(candle, {"5m": [replace(five, high=2.0)], "15m": [fifteen]})
    assert changed["confirmation"] != confirmation


def test_completed_evaluation_includes_full_entry_series_fingerprint() -> None:
    initial = _candle()
    fetched_at = initial.first_fetched_at
    observed_at = initial.first_observed_values_at
    assert fetched_at is not None and observed_at is not None
    candles = [
        replace(
            initial,
            opened_at=initial.opened_at + index * 60,
            first_fetched_at=fetched_at + index * 60,
            first_observed_values_at=observed_at + index * 60,
        )
        for index in range(60)
    ]
    result = run_walk_forward_evaluation(
        candles,
        minimum_liquidity_usd=25_000,
        tp_levels_bps=(2_000,),
        tp_sell_bps=(5_000,),
        stop_loss_bps=2_500,
        execution_cost_bps=100,
    )
    assert result["status"] != "insufficient_history"
    provenance = result["ohlcv_provenance"]
    assert isinstance(provenance, dict)
    entry = provenance["entry"]
    assert isinstance(entry, dict)
    assert entry["count"] == len(candles)
    assert re.fullmatch(r"[0-9a-f]{64}", str(entry["sha256"]))
