from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

CANDLE_INTAKE_STATE = "paper_candle_intake"
MAX_CANDLE_INTAKE = 4
_TENURE_SECONDS = 3_600
_STATE_MAX_AGE_SECONDS = 240


@dataclass(frozen=True, slots=True)
class CandleIntake:
    mints: tuple[str, ...]
    selected_at: int
    updated_at: int

    def state(self) -> dict[str, object]:
        return {
            "mints": list(self.mints),
            "selected_at": self.selected_at,
            "updated_at": self.updated_at,
        }


def choose_candle_intake(
    activities: Mapping[str, int],
    *,
    previous: Mapping[str, object],
    preferred: Sequence[str],
    limit: int,
    now: int,
) -> CandleIntake:
    """Keep an eligible allocation stable while it builds a candle history."""
    if not 1 <= limit <= MAX_CANDLE_INTAKE:
        raise ValueError("Invalid candle intake limit")
    selected_at = previous.get("selected_at")
    old_mints = previous.get("mints")
    retained = (
        [mint for mint in old_mints if isinstance(mint, str) and mint in activities]
        if type(selected_at) is int
        and 0 <= now - selected_at < _TENURE_SECONDS
        and isinstance(old_mints, list)
        else []
    )
    ranked = sorted(activities, key=lambda mint: (-activities[mint], mint))
    mints = tuple(
        dict.fromkeys([*retained, *(mint for mint in preferred if mint in activities), *ranked])
    )[:limit]
    return CandleIntake(mints, selected_at if retained and type(selected_at) is int else now, now)


def current_intake_mints(state: Mapping[str, object] | None, *, now: int) -> frozenset[str] | None:
    """Return None for unavailable membership and an empty set for a valid empty intake."""
    if state is None:
        return None
    updated_at = state.get("updated_at")
    raw_mints = state.get("mints")
    if (
        type(updated_at) is not int
        or not 0 <= now - updated_at <= _STATE_MAX_AGE_SECONDS
        or not isinstance(raw_mints, list)
        or len(raw_mints) > MAX_CANDLE_INTAKE
        or any(not isinstance(mint, str) or not mint for mint in raw_mints)
    ):
        return None
    return frozenset(raw_mints)
