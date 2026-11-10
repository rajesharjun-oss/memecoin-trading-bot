from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any


@dataclass(frozen=True, slots=True)
class HotRadarSignal:
    score: int
    state: str
    reasons: tuple[str, ...]
    risk_flags: tuple[str, ...]

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _decimal(value: object) -> Decimal | None:
    if value in (None, ""):
        return None
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return parsed if parsed.is_finite() else None


def _count(market: dict[str, object], key: str) -> int:
    value = market.get(key)
    return value if isinstance(value, int) and value >= 0 else 0


def evaluate_hot_market(market: dict[str, object], *, now_ms: int | None = None) -> HotRadarSignal:
    """Score observable activity without treating paid visibility as conviction."""
    reasons: list[str] = []
    risk_flags: list[str] = []
    score = 0

    liquidity = _decimal(market.get("liquidity_usd")) or Decimal(0)
    if liquidity >= 100_000:
        score += 20
        reasons.append("liquidity is above $100k (+20)")
    elif liquidity >= 25_000:
        score += 14
        reasons.append("liquidity is above $25k (+14)")
    elif liquidity >= 10_000:
        score += 6
        reasons.append("liquidity is thin but measurable (+6)")
    else:
        risk_flags.append("thin_liquidity")

    volume_5m = _decimal(market.get("volume_5m_usd")) or Decimal(0)
    volume_1h = _decimal(market.get("volume_1h_usd")) or Decimal(0)
    volume_velocity = volume_5m * 12 / volume_1h if volume_1h > 0 else Decimal(0)
    if volume_velocity >= Decimal("1.5") and volume_5m >= 2_500:
        score += 20
        reasons.append(f"5m volume pace is {volume_velocity:.2f}x the hourly pace (+20)")
    elif volume_velocity >= 1 and volume_5m >= 1_000:
        score += 12
        reasons.append(f"5m volume pace is {volume_velocity:.2f}x the hourly pace (+12)")
    else:
        reasons.append("short-term volume is not accelerating (+0)")

    buys_5m = _count(market, "txns_5m_buys")
    sells_5m = _count(market, "txns_5m_sells")
    txns_5m = buys_5m + sells_5m
    buys_1h = _count(market, "txns_1h_buys")
    sells_1h = _count(market, "txns_1h_sells")
    txns_1h = buys_1h + sells_1h
    buy_ratio = Decimal(buys_5m) / txns_5m if txns_5m else Decimal(0)
    txn_velocity = Decimal(txns_5m * 12) / txns_1h if txns_1h else Decimal(0)
    if txns_5m >= 10 and buy_ratio >= Decimal("0.60"):
        score += 15
        reasons.append(f"5m buyers lead at {buy_ratio * 100:.0f}% (+15)")
    elif txns_5m >= 5 and buy_ratio >= Decimal("0.52"):
        score += 8
        reasons.append(f"5m buyers lead at {buy_ratio * 100:.0f}% (+8)")
    elif txns_5m < 5:
        risk_flags.append("low_transaction_count")

    if txn_velocity >= Decimal("1.5") and txns_5m >= 10:
        score += 15
        reasons.append(f"transaction pace accelerated to {txn_velocity:.2f}x (+15)")
    elif txn_velocity >= 1 and txns_5m >= 5:
        score += 8
        reasons.append(f"transaction pace is {txn_velocity:.2f}x (+8)")

    change_5m = _decimal(market.get("price_change_5m"))
    change_1h = _decimal(market.get("price_change_1h"))
    if change_5m is not None and Decimal("1") <= change_5m <= Decimal("25"):
        score += 10
        reasons.append(f"5m momentum is constructive at {change_5m:+.1f}% (+10)")
    elif change_5m is not None and abs(change_5m) > 80:
        risk_flags.append("extreme_5m_move")
    if change_1h is not None and abs(change_1h) > 300:
        risk_flags.append("extreme_1h_move")

    market_cap = _decimal(market.get("market_cap_usd"))
    if market_cap is not None and 50_000 <= market_cap <= 10_000_000:
        score += 10
        reasons.append("market cap is inside the radar range (+10)")

    created_at = market.get("pair_created_at")
    if isinstance(created_at, int) and created_at > 0:
        current_ms = now_ms or int(datetime.now(UTC).timestamp() * 1_000)
        age_minutes = max(0, (current_ms - created_at) // 60_000)
        if 10 <= age_minutes <= 24 * 60:
            score += 10
            reasons.append("pair age is between 10 minutes and 24 hours (+10)")
        elif age_minutes < 10:
            risk_flags.append("very_new_pair")

    if liquidity > 0 and volume_5m / liquidity > 2:
        risk_flags.append("volume_liquidity_anomaly")
    boosts = _count(market, "boosts_active")
    if boosts:
        risk_flags.append("paid_boost_present")
        reasons.append(f"{boosts} active paid boosts add visibility but no score (+0)")

    if "extreme_5m_move" in risk_flags:
        score -= 20
    if "volume_liquidity_anomaly" in risk_flags:
        score -= 10
    score = max(0, min(score, 100))
    state = "hot" if score >= 65 else "warming" if score >= 45 else "quiet"
    return HotRadarSignal(score, state, tuple(reasons), tuple(risk_flags))
