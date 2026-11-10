from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast

import pytest
from solders.pubkey import Pubkey

from memecoin_bot import calls
from memecoin_bot.calls import process_paper_call
from memecoin_bot.config import Settings
from memecoin_bot.constants import TOKEN_PROGRAM_ID
from memecoin_bot.journal import TradeJournal
from memecoin_bot.market_data import TokenMarket
from memecoin_bot.models import TokenAudit
from memecoin_bot.ohlcv import PoolCandle
from memecoin_bot.paper import PaperTradingService, StalePaperDecisionError
from memecoin_bot.service import TradingService
from memecoin_bot.strategy import StrategyDecision

PAIR = str(Pubkey.new_unique())


class FakeMarketClient:
    def __enter__(self) -> FakeMarketClient:
        return self

    def __exit__(self, *_: object) -> None:
        return None

    def tokens(self, mints: list[str]) -> dict[str, TokenMarket]:
        mint = mints[0]
        return {
            mint: TokenMarket(
                mint=mint,
                name="Strong Call",
                symbol="CALL",
                dex_id="raydium",
                pair_address=PAIR,
                price_usd="0.001",
                liquidity_usd="100000",
                volume_24h_usd="250000",
                price_change_24h="25",
                pair_created_at=int((datetime.now(UTC) - timedelta(hours=1)).timestamp() * 1_000),
            )
        }


class FakeTrading:
    def __init__(self, mint: str) -> None:
        self.mint = mint

    def audit(self, mint: str) -> TokenAudit:
        assert mint == self.mint
        return TokenAudit(mint, TOKEN_PROGRAM_ID, 6, 1_000_000, 1_000)


class FakePaper:
    def __init__(self, journal: TradeJournal) -> None:
        self.journal = journal

    def open(
        self,
        *,
        mint: str,
        lamports: int,
        slippage_bps: int,
        decision: StrategyDecision,
        entry_source: str,
    ) -> dict[str, object]:
        assert lamports > 0
        assert slippage_bps == 100
        assert entry_source == "call_inbox"
        self.journal.record_strategy_decision(
            decision,
            action="call_paper_opened",
            position_id="paper-00000000-0000-0000-0000-000000000001",
        )
        return {
            "position": {
                "position_id": "paper-00000000-0000-0000-0000-000000000001",
                "mint": mint,
            }
        }


def test_pasted_call_is_audited_scored_and_paper_opened(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(calls, "DexScreenerClient", FakeMarketClient)
    mint = str(Pubkey.new_unique())
    settings = Settings(
        rpc_url="https://rpc.example.test",
        keypair_path=None,
        jupiter_api_key=None,
        live_trading_enabled=False,
        max_trade_lamports=50_000_000,
        max_daily_lamports=100_000_000,
        max_slippage_bps=300,
        max_price_impact_bps=500,
        max_top10_concentration_bps=7_000,
        max_priority_fee_lamports=1_000_000,
        confirmation_timeout_seconds=45,
        allow_token_2022=False,
        state_path=tmp_path / "state.sqlite3",
    )
    with TradeJournal(settings.state_path) as journal:
        now = int(datetime.now(UTC).timestamp())
        first_five_minute = (now // 300 - 8) * 300
        for frame, seconds in (("1m", 60), ("5m", 300)):
            latest = (now // seconds - 1) * seconds
            candles = []
            for index in range(8):
                opened = latest - (7 - index) * seconds
                price = (
                    1 + index * 0.02
                    if frame == "5m"
                    else 1
                    + ((opened - first_five_minute) // 300) * 0.02
                    + ((opened % 300) // 60 - 4) * 0.003
                )
                opened_price = price - 0.012 if frame == "5m" else price
                candles.append(
                    PoolCandle(
                        mint=mint,
                        pool=PAIR,
                        timeframe=frame,
                        opened_at=opened,
                        open=str(opened_price),
                        high=str(price * 1.02),
                        low=str(opened_price * 0.99),
                        close=str(price * 1.01),
                        volume_usd=str(100 + index * 50),
                    )
                )
                journal.record_market_snapshot(
                    mint=mint,
                    pool=PAIR,
                    price_usd=str(price),
                    liquidity_usd="100000",
                    volume_24h_usd="250000",
                    observed_at=opened + seconds - 1,
                )
            journal.record_pool_candles(candles, fetched_at=now)
        result = process_paper_call(
            settings=settings,
            mint=mint,
            source_label="Watchlist",
            paper=cast(PaperTradingService, FakePaper(journal)),
            trading=cast(TradingService, FakeTrading(mint)),
            journal=journal,
        )

        assert result["status"] == "opened"
        assert result["verdict"] == "eligible"
        assert journal.paper_calls()[0]["status"] == "opened"
        assert journal.discovery_candidates()[0]["source_program"] == "manual_call"

        class ChangedPaper(FakePaper):
            def open(self, **_: object) -> dict[str, object]:
                raise StalePaperDecisionError("Paper entry signal is no longer eligible")

        changed = process_paper_call(
            settings=settings,
            mint=mint,
            source_label="Watchlist",
            paper=cast(PaperTradingService, ChangedPaper(journal)),
            trading=cast(TradingService, FakeTrading(mint)),
            journal=journal,
        )
        assert changed["status"] == "observed"
        assert "signal changed" in changed["reason"]
        assert journal.paper_calls()[0]["paper_position_id"] is None


def test_pasted_call_cannot_open_without_real_candles(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(calls, "DexScreenerClient", FakeMarketClient)
    mint = str(Pubkey.new_unique())
    settings = Settings(
        rpc_url="https://rpc.example.test",
        keypair_path=None,
        jupiter_api_key=None,
        live_trading_enabled=False,
        max_trade_lamports=50_000_000,
        max_daily_lamports=100_000_000,
        max_slippage_bps=300,
        max_price_impact_bps=500,
        max_top10_concentration_bps=7_000,
        max_priority_fee_lamports=1_000_000,
        confirmation_timeout_seconds=45,
        allow_token_2022=False,
        state_path=tmp_path / "state.sqlite3",
    )
    with TradeJournal(settings.state_path) as journal:
        result = process_paper_call(
            settings=settings,
            mint=mint,
            source_label="Watchlist",
            paper=cast(PaperTradingService, FakePaper(journal)),
            trading=cast(TradingService, FakeTrading(mint)),
            journal=journal,
        )
        saved = journal.strategy_decisions()[0]
    assert result["status"] == "observed"
    assert result["verdict"] == "observe"
    evidence = saved["evidence"]
    assert isinstance(evidence, dict)
    assert evidence["candle_signals"]["1m"]["status"] == "collecting"
