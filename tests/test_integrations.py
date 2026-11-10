from __future__ import annotations

from pathlib import Path
from typing import cast

import httpx
import pytest
from solders.pubkey import Pubkey

from memecoin_bot import paper
from memecoin_bot.config import Settings
from memecoin_bot.journal import TradeJournal
from memecoin_bot.market_data import DexScreenerClient, MarketDataError
from memecoin_bot.pump_fun import PumpFunClient, PumpFunError
from memecoin_bot.rpc import RpcClient
from memecoin_bot.service import TradingService


def test_pump_fun_feed_keeps_only_valid_solana_pump_mints() -> None:
    pump_mint = str(Pubkey.new_unique())
    other_mint = str(Pubkey.new_unique())

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/home-feed/new"
        assert request.url.params["chain"] == "solana"
        assert request.headers["Authorization"] == "Bearer pf_live_test"
        return httpx.Response(
            200,
            json={
                "coins": [
                    {"coinMint": pump_mint, "chain": "solana", "program": "pump"},
                    {"coinMint": other_mint, "chain": "solana", "program": "meteora_dbc"},
                    {"coinMint": "not-a-key", "chain": "solana", "program": "pump"},
                ]
            },
        )

    client = PumpFunClient("pf_live_test")
    client._client.close()
    client._client = httpx.Client(
        base_url=PumpFunClient.BASE_URL,
        headers={"Authorization": "Bearer pf_live_test"},
        transport=httpx.MockTransport(handler),
    )
    with client:
        assert client.new_callout_mints(limit=10) == [pump_mint]


def test_pump_fun_timeout_becomes_provider_degradation() -> None:
    def timeout(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("provider timed out", request=request)

    client = PumpFunClient("test-key")
    client._client.close()
    client._client = httpx.Client(
        base_url=PumpFunClient.BASE_URL, transport=httpx.MockTransport(timeout)
    )
    with client, pytest.raises(PumpFunError, match="transport is unavailable"):
        client.new_callout_mints(limit=2)


def test_dexscreener_profiles_keep_only_valid_solana_mints() -> None:
    first = str(Pubkey.new_unique())
    second = str(Pubkey.new_unique())

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/token-profiles/latest/v1"
        return httpx.Response(
            200,
            json=[
                {"chainId": "solana", "tokenAddress": first},
                {"chainId": "base", "tokenAddress": second},
                {"chainId": "solana", "tokenAddress": "invalid"},
                {"chainId": "solana", "tokenAddress": second},
                {"chainId": "solana", "tokenAddress": first},
            ],
        )

    client = DexScreenerClient()
    client._client.close()
    client._client = httpx.Client(
        base_url=DexScreenerClient.BASE_URL,
        transport=httpx.MockTransport(handler),
    )
    with client:
        assert client.latest_solana_profiles(limit=2) == [first, second]


@pytest.mark.parametrize("request_kind", ["profile", "tokens"])
def test_dexscreener_timeout_becomes_provider_degradation(request_kind: str) -> None:
    mint = str(Pubkey.new_unique())

    def timeout(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("provider timed out", request=request)

    client = DexScreenerClient()
    client._client.close()
    client._client = httpx.Client(
        base_url=DexScreenerClient.BASE_URL, transport=httpx.MockTransport(timeout)
    )
    with client, pytest.raises(MarketDataError, match="transport is unavailable"):
        if request_kind == "profile":
            client.latest_solana_profiles(limit=2)
        else:
            client.tokens([mint])


def test_paper_scan_continues_when_dex_discovery_times_out(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def timeout(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("provider timed out", request=request)

    class TimedOutDex(DexScreenerClient):
        def __init__(self) -> None:
            super().__init__()
            self._client.close()
            self._client = httpx.Client(
                base_url=self.BASE_URL, transport=httpx.MockTransport(timeout)
            )

    monkeypatch.setattr(paper, "DexScreenerClient", TimedOutDex)
    settings = Settings(
        rpc_url="https://rpc.example.test",
        keypair_path=None,
        jupiter_api_key=None,
        live_trading_enabled=False,
        max_trade_lamports=100_000_000,
        max_daily_lamports=200_000_000,
        max_slippage_bps=300,
        max_price_impact_bps=500,
        max_top10_concentration_bps=7_000,
        max_priority_fee_lamports=1_000_000,
        confirmation_timeout_seconds=45,
        allow_token_2022=False,
        state_path=tmp_path / "timeout.sqlite3",
        discovery_program_ids=(),
    )
    with TradeJournal(settings.state_path) as journal:
        result = paper.scan_candidates(
            settings=settings,
            rpc=cast(RpcClient, object()),
            trading=cast(TradingService, object()),
            journal=journal,
        )
    assert result["scanned"] == 0
    assert result["providers"]["dexscreener"]["profiles"] == "degraded"
    assert result["providers"]["dexscreener"]["community_takeovers"] == "degraded"
    assert "temporarily unavailable" in result["warning"]


def test_dexscreener_parses_short_timeframe_activity() -> None:
    mint = str(Pubkey.new_unique())

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == f"/tokens/v1/solana/{mint}"
        return httpx.Response(
            200,
            json=[
                {
                    "chainId": "solana",
                    "dexId": "pumpfun",
                    "pairAddress": str(Pubkey.new_unique()),
                    "baseToken": {"address": mint, "name": "Radar", "symbol": "RDR"},
                    "quoteToken": {"address": str(Pubkey.new_unique())},
                    "priceUsd": "0.00125",
                    "liquidity": {"usd": 125000},
                    "volume": {"m5": 15000, "h1": 90000, "h6": 240000, "h24": 500000},
                    "priceChange": {"m5": 8.5, "h1": 24, "h6": 80, "h24": 140},
                    "txns": {
                        "m5": {"buys": 84, "sells": 36},
                        "h1": {"buys": 430, "sells": 290},
                        "h6": {"buys": 1600, "sells": 1100},
                        "h24": {"buys": 3200, "sells": 2300},
                    },
                    "marketCap": 850000,
                    "fdv": 900000,
                    "boosts": {"active": 12},
                    "pairCreatedAt": 1_762_000_000_000,
                }
            ],
        )

    client = DexScreenerClient()
    client._client.close()
    client._client = httpx.Client(
        base_url=DexScreenerClient.BASE_URL,
        transport=httpx.MockTransport(handler),
    )
    with client:
        market = client.tokens([mint])[mint]

    assert market.market_cap_usd == "850000"
    assert market.volume_5m_usd == "15000"
    assert market.price_change_5m == "8.5"
    assert market.txns_5m_buys == 84
    assert market.txns_1h_sells == 290
    assert market.boosts_active == 12


def test_dexscreener_discovery_feeds_filter_invalid_entries() -> None:
    mint = str(Pubkey.new_unique())

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/community-takeovers/latest/v1"
        return httpx.Response(
            200,
            json=[
                {"chainId": "base", "tokenAddress": mint},
                {"chainId": "solana", "tokenAddress": "invalid"},
                {"chainId": "solana", "tokenAddress": mint},
            ],
        )

    client = DexScreenerClient()
    client._client.close()
    client._client = httpx.Client(
        base_url=DexScreenerClient.BASE_URL,
        transport=httpx.MockTransport(handler),
    )
    with client:
        assert client.latest_solana_takeovers(limit=5) == [mint]
