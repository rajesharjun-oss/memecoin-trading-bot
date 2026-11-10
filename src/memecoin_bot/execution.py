from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Literal

from .config import Settings
from .constants import LAMPORTS_PER_SOL, WSOL_MINT
from .models import RouteQuote


class PaperExecutionError(RuntimeError):
    """Raised when a paper route cannot produce a credible executable fill."""


class UneconomicPaperExitError(PaperExecutionError):
    """A route exists, but its gross proceeds cannot pay the modeled exit fees."""


def paper_execution_failure_reason(error: PaperExecutionError) -> str:
    """Return a stable, non-sensitive explanation for a rejected paper quote."""
    if isinstance(error, UneconomicPaperExitError):
        return "below_exit_fees"
    message = str(error)
    if message.startswith("Route is stale"):
        return "stale_route"
    if message.startswith("Paper fill slippage exceeds"):
        return "slippage_limit"
    if message.startswith("Route price impact exceeds"):
        return "price_impact_limit"
    if message.startswith("Modeled adverse slippage exceeds"):
        return "modeled_slippage_limit"
    if message.startswith("Paper entry requires a positive"):
        return "entry_liquidity_unavailable"
    if message.startswith("SOL USD reference"):
        return "sol_usd_reference_unavailable"
    if message.startswith("Route size exceeds"):
        return "liquidity_share_limit"
    if message.startswith("Modeled fill output is zero"):
        return "zero_output"
    return "execution_model_rejected"


@dataclass(frozen=True, slots=True)
class PaperFillEstimate:
    side: Literal["buy", "sell", "mark"]
    quote_in_amount: int
    quote_out_amount: int
    executed_out_amount: int
    adverse_slippage_bps: int
    price_impact_bps: int | None
    network_fee_lamports: int
    priority_fee_lamports: int
    ata_rent_lamports: int
    modeled_slippage_lamports: int
    cash_debit_lamports: int
    cash_credit_lamports: int
    quote_age_ms: int
    liquidity_usd: str | None
    swap_usd: str | None
    trade_liquidity_bps: int | None
    stressed: bool

    @property
    def explicit_cost_lamports(self) -> int:
        return self.network_fee_lamports + self.priority_fee_lamports + self.ata_rent_lamports

    def as_dict(self) -> dict[str, object]:
        return {**asdict(self), "explicit_cost_lamports": self.explicit_cost_lamports}


def _decimal(value: object) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return parsed if parsed.is_finite() and parsed >= 0 else None


def quote_swap_usd(quote: RouteQuote) -> Decimal | None:
    for key in ("swapUsdValue", "usdValue", "inUsdValue"):
        value = _decimal(quote.raw.get(key))
        if value is not None:
            return value
    return None


class PaperExecutionModel:
    """Conservative deterministic paper fills derived from a fresh Jupiter quote."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def estimate(
        self,
        *,
        quote: RouteQuote,
        side: Literal["buy", "sell", "mark"],
        slippage_bps: int,
        quoted_at: datetime,
        liquidity_usd: Decimal | None = None,
        sol_usd_price: Decimal | None = None,
        now: datetime | None = None,
        ata_rent_override_lamports: int | None = None,
    ) -> PaperFillEstimate:
        observed_now = now or datetime.now(UTC)
        if quoted_at.tzinfo is None:
            quoted_at = quoted_at.replace(tzinfo=UTC)
        age_ms = max(0, int((observed_now - quoted_at).total_seconds() * 1_000))
        if age_ms > self.settings.paper_execution_max_quote_age_ms:
            raise PaperExecutionError(
                f"Route is stale ({age_ms} ms; maximum "
                f"{self.settings.paper_execution_max_quote_age_ms} ms)"
            )
        if not 1 <= slippage_bps <= self.settings.max_slippage_bps:
            raise PaperExecutionError("Paper fill slippage exceeds the configured maximum")
        price_impact = quote.price_impact_bps
        if price_impact is not None and price_impact > self.settings.max_price_impact_bps:
            raise PaperExecutionError("Route price impact exceeds the configured maximum")

        adverse_bps = (
            self.settings.paper_execution_base_slippage_bps
            + self.settings.paper_execution_liquidity_shock_bps
        )
        if adverse_bps > slippage_bps:
            raise PaperExecutionError(
                "Modeled adverse slippage exceeds this order's slippage tolerance"
            )

        swap_usd = quote_swap_usd(quote)
        if swap_usd is None and side == "buy" and quote.input_mint == WSOL_MINT:
            if sol_usd_price is None or not sol_usd_price.is_finite() or sol_usd_price <= 0:
                raise PaperExecutionError("SOL USD reference is unavailable")
            swap_usd = Decimal(quote.in_amount) * sol_usd_price / LAMPORTS_PER_SOL
        if side == "buy" and (
            liquidity_usd is None or liquidity_usd <= 0 or swap_usd is None or swap_usd <= 0
        ):
            raise PaperExecutionError(
                "Paper entry requires a positive route USD value and visible pool liquidity"
            )
        trade_liquidity_bps: int | None = None
        if liquidity_usd is not None and liquidity_usd > 0 and swap_usd is not None:
            trade_liquidity_bps = int(swap_usd * 10_000 / liquidity_usd)
            if trade_liquidity_bps > self.settings.paper_execution_max_trade_liquidity_bps:
                raise PaperExecutionError(
                    "Route size exceeds the configured share of visible pool liquidity"
                )

        executed_out = quote.out_amount * (10_000 - adverse_bps) // 10_000
        if executed_out <= 0:
            raise PaperExecutionError("Modeled fill output is zero")

        network_fee = self.settings.paper_execution_network_fee_lamports
        priority_fee = self.settings.paper_execution_priority_fee_lamports
        if ata_rent_override_lamports is not None and (
            type(ata_rent_override_lamports) is not int
            or not 0 <= ata_rent_override_lamports <= 100_000_000
        ):
            raise PaperExecutionError("Output token-account reserve is invalid")
        ata_rent = (
            (
                self.settings.paper_execution_ata_rent_lamports
                if ata_rent_override_lamports is None
                else ata_rent_override_lamports
            )
            if side == "buy"
            else 0
        )
        if side == "buy":
            cash_debit = quote.in_amount + network_fee + priority_fee + ata_rent
            cash_credit = 0
            modeled_slippage_lamports = quote.in_amount * adverse_bps // 10_000
        else:
            total_fees = network_fee + priority_fee
            cash_debit = 0
            cash_credit = executed_out - total_fees
            modeled_slippage_lamports = quote.out_amount - executed_out
            if cash_credit <= 0:
                raise UneconomicPaperExitError(
                    "Modeled exit proceeds do not cover transaction fees"
                )

        return PaperFillEstimate(
            side=side,
            quote_in_amount=quote.in_amount,
            quote_out_amount=quote.out_amount,
            executed_out_amount=executed_out,
            adverse_slippage_bps=adverse_bps,
            price_impact_bps=price_impact,
            network_fee_lamports=network_fee,
            priority_fee_lamports=priority_fee,
            ata_rent_lamports=ata_rent,
            modeled_slippage_lamports=modeled_slippage_lamports,
            cash_debit_lamports=cash_debit,
            cash_credit_lamports=cash_credit,
            quote_age_ms=age_ms,
            liquidity_usd=str(liquidity_usd) if liquidity_usd is not None else None,
            swap_usd=str(swap_usd) if swap_usd is not None else None,
            trade_liquidity_bps=trade_liquidity_bps,
            stressed=self.settings.paper_execution_liquidity_shock_bps > 0,
        )


def paper_swap_input_lamports(settings: Settings, available_cash_lamports: int) -> int:
    """Cap the simulated swap so its full modeled buy debit fits the risk budget."""
    if available_cash_lamports < 0:
        raise ValueError("Available paper cash cannot be negative")
    cash_budget = available_cash_lamports * settings.paper_risk_per_trade_bps // 10_000
    entry_costs = (
        settings.paper_execution_network_fee_lamports
        + settings.paper_execution_priority_fee_lamports
        + settings.paper_execution_ata_rent_lamports
    )
    return min(max(0, cash_budget - entry_costs), settings.max_trade_lamports)


def estimated_paper_swap_fraction_bps(settings: Settings) -> int:
    """Approximate replay swap exposure at starting cash; zero means no viable entry."""
    swap_input = paper_swap_input_lamports(settings, settings.paper_starting_lamports)
    if swap_input < 1_000_000:
        return 0
    return max(1, swap_input * 10_000 // settings.paper_starting_lamports)


@dataclass(frozen=True, slots=True)
class ReplayCostEstimate:
    """Planning costs relative to the simulated swap input, not a live quote."""

    execution_cost_bps: int
    fixed_cost_bps: int
    refundable_ata_reserve_bps: int
    swap_input_lamports: int


def estimated_replay_costs(settings: Settings) -> ReplayCostEstimate:
    position_lamports = paper_swap_input_lamports(settings, settings.paper_starting_lamports)
    if position_lamports < 1_000_000:
        return ReplayCostEstimate(5_000, 5_000, 0, position_lamports)
    round_trip_fees = 2 * (
        settings.paper_execution_network_fee_lamports
        + settings.paper_execution_priority_fee_lamports
    )
    rent = settings.paper_execution_ata_rent_lamports
    fixed_bps = min(5_000, (round_trip_fees + rent) * 10_000 // position_lamports)
    rent_bps = min(fixed_bps, rent * 10_000 // position_lamports)
    slippage_bps = 2 * (
        settings.paper_execution_base_slippage_bps + settings.paper_execution_liquidity_shock_bps
    )
    total_bps = min(5_000, fixed_bps + slippage_bps)
    return ReplayCostEstimate(total_bps, fixed_bps, rent_bps, position_lamports)


def estimated_round_trip_cost_bps(settings: Settings) -> int:
    """Estimate total first-trade cash cost, including refundable ATA reserve."""
    return estimated_replay_costs(settings).execution_cost_bps


def estimated_additional_exit_fee_bps(settings: Settings) -> int:
    """Fixed fee for each sale after the first, relative to initial paper notional."""
    position_lamports = paper_swap_input_lamports(settings, settings.paper_starting_lamports)
    if position_lamports < 1_000_000:
        return 5_000
    fee_lamports = (
        settings.paper_execution_network_fee_lamports
        + settings.paper_execution_priority_fee_lamports
    )
    return min(5_000, (fee_lamports * 10_000 + position_lamports - 1) // position_lamports)
