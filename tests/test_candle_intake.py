from __future__ import annotations

from memecoin_bot.candle_intake import choose_candle_intake, current_intake_mints


def test_intake_keeps_eligible_members_and_rotates_after_an_hour() -> None:
    first = choose_candle_intake(
        {"alpha": 21, "beta": 12, "gamma": 40, "delta": 99, "epsilon": 100},
        previous={},
        preferred=["beta"],
        limit=4,
        now=10_000,
    )
    assert first.mints == ("beta", "epsilon", "delta", "gamma")

    retained = choose_candle_intake(
        {"alpha": 500, "beta": 12, "gamma": 40, "delta": 99, "epsilon": 100},
        previous=first.state(),
        preferred=["alpha"],
        limit=4,
        now=10_500,
    )
    assert retained.mints == first.mints
    assert retained.selected_at == first.selected_at

    lost_eligibility = choose_candle_intake(
        {"alpha": 500, "gamma": 40, "delta": 99, "epsilon": 100},
        previous=retained.state(),
        preferred=[],
        limit=4,
        now=10_600,
    )
    assert lost_eligibility.mints == ("epsilon", "delta", "gamma", "alpha")

    renewed = choose_candle_intake(
        {"alpha": 500, "beta": 12, "gamma": 40, "delta": 99, "epsilon": 100},
        previous=first.state(),
        preferred=[],
        limit=4,
        now=13_600,
    )
    assert renewed.mints == ("alpha", "epsilon", "delta", "gamma")
    assert renewed.selected_at == 13_600


def test_intake_membership_distinguishes_aged_state_from_valid_empty_state() -> None:
    assert current_intake_mints({"mints": ["alpha"], "updated_at": 100}, now=341) is None
    assert current_intake_mints({"mints": ["alpha"], "updated_at": 400}, now=399) is None
    assert current_intake_mints({"mints": [None], "updated_at": 100}, now=120) is None
    assert current_intake_mints({"mints": [], "updated_at": 100}, now=120) == frozenset()
