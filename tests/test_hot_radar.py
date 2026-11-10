from __future__ import annotations

from datetime import UTC, datetime, timedelta

from memecoin_bot.hot_radar import evaluate_hot_market


def _strong_market(**overrides: object) -> dict[str, object]:
    now = datetime(2026, 11, 2, tzinfo=UTC)
    market: dict[str, object] = {
        "liquidity_usd": "125000",
        "volume_5m_usd": "15000",
        "volume_1h_usd": "90000",
        "price_change_5m": "8.5",
        "price_change_1h": "24",
        "market_cap_usd": "850000",
        "pair_created_at": int((now - timedelta(hours=3)).timestamp() * 1_000),
        "txns_5m_buys": 84,
        "txns_5m_sells": 36,
        "txns_1h_buys": 430,
        "txns_1h_sells": 290,
        "boosts_active": 0,
    }
    market.update(overrides)
    return market


def test_strong_organic_activity_is_hot() -> None:
    now_ms = int(datetime(2026, 11, 2, tzinfo=UTC).timestamp() * 1_000)
    result = evaluate_hot_market(_strong_market(), now_ms=now_ms)

    assert result.state == "hot"
    assert result.score >= 65
    assert "paid_boost_present" not in result.risk_flags
    assert any("buyers lead" in reason for reason in result.reasons)


def test_paid_boost_is_disclosed_but_never_adds_score() -> None:
    now_ms = int(datetime(2026, 11, 2, tzinfo=UTC).timestamp() * 1_000)
    organic = evaluate_hot_market(_strong_market(), now_ms=now_ms)
    promoted = evaluate_hot_market(_strong_market(boosts_active=500), now_ms=now_ms)

    assert promoted.score == organic.score
    assert "paid_boost_present" in promoted.risk_flags
    assert any("no score" in reason for reason in promoted.reasons)


def test_extreme_move_and_volume_anomaly_are_penalized() -> None:
    now_ms = int(datetime(2026, 11, 2, tzinfo=UTC).timestamp() * 1_000)
    normal = evaluate_hot_market(_strong_market(), now_ms=now_ms)
    suspicious = evaluate_hot_market(
        _strong_market(
            liquidity_usd="10000",
            volume_5m_usd="50000",
            price_change_5m="150",
        ),
        now_ms=now_ms,
    )

    assert suspicious.score < normal.score
    assert "extreme_5m_move" in suspicious.risk_flags
    assert "volume_liquidity_anomaly" in suspicious.risk_flags
