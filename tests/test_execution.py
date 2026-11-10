from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from memecoin_bot.config import Settings
from memecoin_bot.constants import WSOL_MINT
from memecoin_bot.execution import (
    PaperExecutionError,
    PaperExecutionModel,
    UneconomicPaperExitError,
    estimated_additional_exit_fee_bps,
    estimated_paper_swap_fraction_bps,
    estimated_replay_costs,
    estimated_round_trip_cost_bps,
    paper_swap_input_lamports,
)
from memecoin_bot.models import RouteQuote


def _settings(tmp_path: Path, **overrides: object) -> Settings:
    values: dict[str, object] = {
        "rpc_url": "https://rpc.example.test",
        "keypair_path": None,
        "jupiter_api_key": None,
        "live_trading_enabled": False,
        "max_trade_lamports": 100_000_000,
        "max_daily_lamports": 200_000_000,
        "max_slippage_bps": 300,
        "max_price_impact_bps": 500,
        "max_top10_concentration_bps": 7_000,
        "max_priority_fee_lamports": 1_000_000,
        "confirmation_timeout_seconds": 45,
        "allow_token_2022": False,
        "state_path": tmp_path / "state.sqlite3",
    }
    values.update(overrides)
    return Settings(**values)  # type: ignore[arg-type]


def _quote(*, swap_usd: str = "100") -> RouteQuote:
    return RouteQuote(
        input_mint="input",
        output_mint="output",
        in_amount=50_000_000,
        out_amount=1_000_000,
        price_impact_bps=75,
        router="test",
        fee_bps=0,
        raw={"swapUsdValue": swap_usd},
    )


def test_execution_model_applies_conservative_buy_costs(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    now = datetime.now(UTC)
    fill = PaperExecutionModel(settings).estimate(
        quote=_quote(),
        side="buy",
        slippage_bps=100,
        quoted_at=now - timedelta(milliseconds=250),
        liquidity_usd=Decimal("100000"),
        now=now,
    )

    assert fill.executed_out_amount == 997_000
    assert fill.cash_debit_lamports == 52_094_280
    assert fill.cash_credit_lamports == 0
    assert fill.explicit_cost_lamports == 2_094_280
    assert fill.quote_age_ms == 250
    assert fill.trade_liquidity_bps == 10

    measured_fill = PaperExecutionModel(settings).estimate(
        quote=_quote(),
        side="buy",
        slippage_bps=100,
        quoted_at=now,
        liquidity_usd=Decimal("100000"),
        now=now,
        ata_rent_override_lamports=1_513_840,
    )
    assert measured_fill.ata_rent_lamports == 1_513_840
    assert measured_fill.cash_debit_lamports == 51_568_840


def test_replay_cost_estimate_separates_refundable_reserve(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    costs = estimated_replay_costs(settings)
    assert costs.execution_cost_bps == estimated_round_trip_cost_bps(settings)
    assert 0 < costs.refundable_ata_reserve_bps < costs.fixed_cost_bps
    assert costs.fixed_cost_bps < costs.execution_cost_bps
    assert costs.swap_input_lamports == paper_swap_input_lamports(
        settings, settings.paper_starting_lamports
    )


def test_execution_model_values_sell_at_net_proceeds(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    now = datetime.now(UTC)
    fill = PaperExecutionModel(settings).estimate(
        quote=_quote(),
        side="sell",
        slippage_bps=100,
        quoted_at=now,
        now=now,
    )

    assert fill.executed_out_amount == 997_000
    assert fill.cash_credit_lamports == 942_000
    assert fill.ata_rent_lamports == 0


def test_execution_model_rejects_stale_and_oversized_routes(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    model = PaperExecutionModel(settings)
    now = datetime.now(UTC)

    with pytest.raises(PaperExecutionError, match="stale"):
        model.estimate(
            quote=_quote(),
            side="buy",
            slippage_bps=100,
            quoted_at=now - timedelta(milliseconds=5_001),
            now=now,
        )

    with pytest.raises(PaperExecutionError, match="pool liquidity"):
        model.estimate(
            quote=_quote(swap_usd="200"),
            side="buy",
            slippage_bps=100,
            quoted_at=now,
            liquidity_usd=Decimal("10000"),
            now=now,
        )


@pytest.mark.parametrize(
    ("swap_usd", "liquidity_usd"),
    [("", Decimal("100000")), ("0", Decimal("100000")), ("100", None)],
)
def test_paper_buy_requires_route_value_and_pool_liquidity(
    tmp_path: Path, swap_usd: str, liquidity_usd: Decimal | None
) -> None:
    with pytest.raises(PaperExecutionError, match="visible pool liquidity"):
        PaperExecutionModel(_settings(tmp_path)).estimate(
            quote=_quote(swap_usd=swap_usd),
            side="buy",
            slippage_bps=100,
            quoted_at=datetime.now(UTC),
            liquidity_usd=liquidity_usd,
        )


def test_sol_buy_can_value_missing_router_usd_with_sol_reference(tmp_path: Path) -> None:
    quote = replace(_quote(swap_usd=""), input_mint=WSOL_MINT, in_amount=10_000_000)
    fill = PaperExecutionModel(_settings(tmp_path)).estimate(
        quote=quote,
        side="buy",
        slippage_bps=100,
        quoted_at=datetime.now(UTC),
        liquidity_usd=Decimal("10000"),
        sol_usd_price=Decimal("120"),
    )
    assert fill.swap_usd == "1.2"
    assert fill.trade_liquidity_bps == 1
    with pytest.raises(PaperExecutionError, match="SOL USD reference"):
        PaperExecutionModel(_settings(tmp_path)).estimate(
            quote=quote,
            side="buy",
            slippage_bps=100,
            quoted_at=datetime.now(UTC),
            liquidity_usd=Decimal("10000"),
        )


def test_paper_exit_can_still_be_priced_without_usd_metadata(tmp_path: Path) -> None:
    fill = PaperExecutionModel(_settings(tmp_path)).estimate(
        quote=_quote(swap_usd=""),
        side="sell",
        slippage_bps=100,
        quoted_at=datetime.now(UTC),
    )
    assert fill.cash_credit_lamports > 0
    assert fill.trade_liquidity_bps is None


def test_quote_below_exit_fees_is_distinct_from_missing_route(tmp_path: Path) -> None:
    quote = RouteQuote("input", "output", 50_000_000, 1_344, 95, "test", 0, {})
    with pytest.raises(UneconomicPaperExitError, match="do not cover transaction fees"):
        PaperExecutionModel(_settings(tmp_path)).estimate(
            quote=quote,
            side="mark",
            slippage_bps=100,
            quoted_at=datetime.now(UTC),
        )


def test_execution_model_rejects_shock_beyond_order_tolerance(tmp_path: Path) -> None:
    settings = _settings(
        tmp_path,
        paper_execution_base_slippage_bps=30,
        paper_execution_liquidity_shock_bps=100,
    )
    with pytest.raises(PaperExecutionError, match="tolerance"):
        PaperExecutionModel(settings).estimate(
            quote=_quote(),
            side="mark",
            slippage_bps=100,
            quoted_at=datetime.now(UTC),
        )


def test_round_trip_cost_includes_fixed_and_variable_costs(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    assert paper_swap_input_lamports(settings, settings.paper_starting_lamports) == 7_905_720
    assert estimated_paper_swap_fraction_bps(settings) == 79
    assert estimated_round_trip_cost_bps(settings) == 2_778
    assert estimated_additional_exit_fee_bps(settings) == 70

    small_budget = _settings(tmp_path, paper_risk_per_trade_bps=1)
    assert estimated_paper_swap_fraction_bps(small_budget) == 0


def test_sized_paper_buy_debit_matches_total_cash_risk_budget(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    swap_input = paper_swap_input_lamports(settings, settings.paper_starting_lamports)
    now = datetime.now(UTC)
    fill = PaperExecutionModel(settings).estimate(
        quote=replace(_quote(swap_usd="1"), in_amount=swap_input),
        side="buy",
        slippage_bps=100,
        quoted_at=now,
        liquidity_usd=Decimal("100000"),
        now=now,
    )
    assert fill.cash_debit_lamports == (
        settings.paper_starting_lamports * settings.paper_risk_per_trade_bps // 10_000
    )
