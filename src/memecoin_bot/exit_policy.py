"""Deterministic exit rules shared by forward paper and historical research."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True, slots=True)
class ExitPolicy:
    tp_levels_bps: tuple[int, ...]
    tp_sell_bps: tuple[int, ...]
    stop_loss_bps: int
    trailing_activation_bps: int
    trailing_stop_bps: int
    max_hold_seconds: int


@dataclass(frozen=True, slots=True)
class ExitRule:
    reason: str
    action: Literal["close", "partial"]
    sell_bps: int | None = None
    tp_stage: int | None = None


def evaluate_exit_rule(
    *,
    entry_value: int,
    current_value: int,
    peak_value: int,
    tp_stage: int,
    age_seconds: int | None,
    policy: ExitPolicy,
) -> ExitRule | None:
    """Choose an exit from a current marked position; fills are modeled separately."""
    if entry_value <= 0 or current_value <= 0 or peak_value <= 0:
        raise ValueError("Exit values must be positive")
    peak = max(entry_value, current_value, peak_value)
    pnl_bps = (current_value - entry_value) * 10_000 // entry_value
    if pnl_bps <= -policy.stop_loss_bps:
        return ExitRule("stop_loss", "close")
    if not 0 <= tp_stage <= len(policy.tp_levels_bps):
        raise ValueError("Take-profit stage is invalid")
    if tp_stage < len(policy.tp_levels_bps) and pnl_bps >= policy.tp_levels_bps[tp_stage]:
        return ExitRule(
            f"take_profit_stage_{tp_stage + 1}",
            "partial",
            policy.tp_sell_bps[tp_stage],
            tp_stage + 1,
        )
    activation = entry_value * (10_000 + policy.trailing_activation_bps) // 10_000
    drawdown_bps = (peak - current_value) * 10_000 // peak
    if peak >= activation and drawdown_bps >= policy.trailing_stop_bps:
        return ExitRule("trailing_stop", "close")
    if age_seconds is None or age_seconds < 0:
        raise ValueError("Position age is invalid")
    if age_seconds >= policy.max_hold_seconds:
        return ExitRule("max_hold_time", "close")
    return None
