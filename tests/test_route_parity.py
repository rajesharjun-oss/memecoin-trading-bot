from __future__ import annotations

from pathlib import Path
from typing import Any, cast

import pytest
from solders.keypair import Keypair
from solders.pubkey import Pubkey

from memecoin_bot.config import Settings
from memecoin_bot.constants import WSOL_MINT
from memecoin_bot.journal import TradeJournal
from memecoin_bot.jupiter import JupiterClient
from memecoin_bot.models import RouteQuote, TradeResult
from memecoin_bot.paper import PaperTradingService
from memecoin_bot.service import RiskRejectedError, TradingService


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        rpc_url="https://rpc.example.test",
        keypair_path=None,
        jupiter_api_key=None,
        live_trading_enabled=False,
        max_trade_lamports=100_000_000,
        max_daily_lamports=200_000_000,
        max_slippage_bps=300,
        max_price_impact_bps=500,
        max_top10_concentration_bps=4_000,
        max_priority_fee_lamports=1_000_000,
        confirmation_timeout_seconds=30,
        allow_token_2022=False,
        state_path=tmp_path / "state.sqlite3",
    )


class _Router:
    def __init__(self, mint: str, *, impact: str = "0.002") -> None:
        self.mint = mint
        self.impact = impact
        self.calls: list[str] = []

    def quote(self, input_mint: str, output_mint: str, amount: int, _slippage: int) -> RouteQuote:
        self.calls.append("order")
        return RouteQuote(input_mint, output_mint, amount, 100, 20, "metis", 10, {}, "v2")

    def build(self, **kwargs: object) -> dict[str, Any]:
        self.calls.append("build")
        return {
            "inputMint": kwargs["input_mint"],
            "outputMint": kwargs["output_mint"],
            "inAmount": str(kwargs["amount"]),
            "outAmount": "110",
            "slippageBps": kwargs["slippage_bps"],
            "priceImpactPct": self.impact,
            "swapInstruction": {},
        }

    def quote_from_build(self, data: dict[str, Any], **kwargs: Any) -> RouteQuote:
        return JupiterClient.quote_from_build(data, **kwargs)

    def router_quote(
        self, input_mint: str, output_mint: str, amount: int, *, taker: str, slippage_bps: int
    ) -> RouteQuote:
        assert taker
        self.calls.append("router_quote")
        return RouteQuote(input_mint, output_mint, amount, 120, 20, "metis", 0, {}, "v2-build")


def test_dry_run_uses_output_of_transaction_actually_built(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    mint = str(Pubkey.new_unique())
    router = _Router(mint)
    settings = _settings(tmp_path)
    monkeypatch.setattr(
        "memecoin_bot.service.build_simulate_and_submit",
        lambda *_args, **_kwargs: TradeResult(None, 100, False, None),
    )
    service = TradingService(
        settings=settings,
        rpc=cast(Any, None),
        jupiter=cast(JupiterClient, router),
        keypair=Keypair(),
        journal=cast(Any, None),
    )
    report = service.trade(
        input_mint=WSOL_MINT,
        output_mint=mint,
        amount_atomic=100,
        slippage_bps=100,
        execute=False,
        acknowledgement=None,
        audit_output=False,
    )
    assert router.calls == ["order", "build"]
    assert report.quote.out_amount == 110
    assert report.quote.api_version == "v2-build"
    assert report.quote.fee_bps == 0
    assert report.quote_requested_at is not None

    router.impact = "0.2"
    with pytest.raises(RiskRejectedError, match="Built route price impact"):
        service.trade(
            input_mint=WSOL_MINT,
            output_mint=mint,
            amount_atomic=100,
            slippage_bps=100,
            execute=False,
            acknowledgement=None,
            audit_output=False,
        )


def test_paper_exit_keeps_legacy_route_and_uses_router_for_new_positions(tmp_path: Path) -> None:
    mint = str(Pubkey.new_unique())
    router = _Router(mint)
    with TradeJournal(tmp_path / "state.sqlite3") as journal:
        paper = PaperTradingService(
            settings=_settings(tmp_path),
            trading=cast(Any, None),
            jupiter=cast(JupiterClient, router),
            journal=journal,
            taker_pubkey=str(Pubkey.new_unique()),
        )
        legacy = paper._exit_quote({"mint": mint, "entry_execution": {}}, 100, 100)
        new = paper._exit_quote(
            {"mint": mint, "entry_execution": {"quote_source": "v2-build"}}, 100, 100
        )
    assert legacy.out_amount == 100
    assert new.out_amount == 120
    assert router.calls == ["order", "router_quote"]
