from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
from solders.pubkey import Pubkey

from memecoin_bot.config import Settings
from memecoin_bot.constants import MARKET_POLICY_VERSION, TOKEN_PROGRAM_ID, WSOL_MINT
from memecoin_bot.execution import PaperExecutionError
from memecoin_bot.forward_performance import _entry_revalidation
from memecoin_bot.journal import TradeJournal
from memecoin_bot.jupiter import JupiterClient, JupiterError
from memecoin_bot.market_data import TokenMarket
from memecoin_bot.models import RouteQuote, TokenAudit, TradeReport, TradeResult
from memecoin_bot.ohlcv import PoolCandle
from memecoin_bot.paper import (
    PaperTradingError,
    PaperTradingService,
    StalePaperDecisionError,
    scan_candidates,
)
from memecoin_bot.rpc import RpcClient, RpcError
from memecoin_bot.service import TradingService
from memecoin_bot.strategy import (
    StrategyDecision,
    confirm_candidate_candles,
    evaluate_candidate,
    paper_entry_breakers,
)
from memecoin_bot.strategy_lab import RESEARCH_COHORT_KEY


class FakeTrading:
    def __init__(self, mint: str) -> None:
        self.mint = mint

    def buy(self, **_: object) -> TradeReport:
        audit = TokenAudit(self.mint, TOKEN_PROGRAM_ID, 6, 1_000_000, 1_000)
        quote = RouteQuote(WSOL_MINT, self.mint, 50, 1_000, 20, "test", 0, {"swapUsdValue": "1"})
        return TradeReport("paper-preview", audit, quote, TradeResult(None, 123, False, None))

    def sell(self, **_: object) -> TradeReport:
        quote = RouteQuote(self.mint, WSOL_MINT, 1_000, 65, 25, "test", 0, {})
        return TradeReport("paper-close", None, quote, TradeResult(None, 140, False, None))


class FakeJupiter:
    def __init__(self, mint: str) -> None:
        self.mint = mint

    def quote(
        self, input_mint: str, output_mint: str, amount: int, slippage_bps: int = 50
    ) -> RouteQuote:
        assert 1 <= slippage_bps <= 2_000
        assert input_mint == self.mint
        assert output_mint == WSOL_MINT
        return RouteQuote(input_mint, output_mint, amount, amount * 60 // 1_000, 30, "test", 0, {})


def _seed_confirmed_candles(
    journal: TradeJournal, settings: Settings, mint: str, pool: str
) -> None:
    """Create a current real-candle signal without synthetic snapshot candles."""
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
                    pool=pool,
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
                pool=pool,
                price_usd=str(price),
                liquidity_usd="100000",
                volume_24h_usd="250000",
                observed_at=opened + seconds - 1,
            )
        journal.record_pool_candles(candles, fetched_at=now)
    candidate = next(
        (row for row in journal.discovery_candidates(limit=100) if row["mint"] == mint), None
    )
    assert candidate is not None
    decision = confirm_candidate_candles(
        candidate, evaluate_candidate(candidate, settings), settings, journal
    )
    assert decision.verdict == "eligible", decision.as_dict()


def test_scan_rotates_safe_watchlist_audits_and_failed_refresh_stays_stale(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    mint = str(Pubkey.new_unique())
    blocked_mint = str(Pubkey.new_unique())
    pool = str(Pubkey.new_unique())
    checked: list[str] = []
    fail = False

    class FakeDex:
        def __enter__(self) -> FakeDex:
            return self

        def __exit__(self, *_: object) -> None:
            return None

        def tokens(self, _mints: list[str]) -> dict[str, object]:
            return {}

    class FakeAuditTrading:
        def audit(self, selected_mint: str) -> TokenAudit:
            checked.append(selected_mint)
            if fail:
                raise RuntimeError("RPC unavailable")
            return TokenAudit(selected_mint, TOKEN_PROGRAM_ID, 6, 10_000, 1_000)

    monkeypatch.setattr("memecoin_bot.paper.discover_recent_mints", lambda *_a, **_k: [])
    monkeypatch.setattr("memecoin_bot.paper.DexScreenerClient", FakeDex)
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
        state_path=tmp_path / "watchlist.sqlite3",
        dexscreener_discovery_enabled=False,
    )
    with TradeJournal(settings.state_path) as journal:
        for candidate_mint, status in ((mint, "safe"), (blocked_mint, "blocked")):
            journal.upsert_discovery_candidate(
                mint=candidate_mint,
                source_program="test",
                audit_status=status,
                audit_summary=None,
                top10_concentration_bps=1_000,
                decimals=6,
                market={"pair_address": pool, "price_usd": "1", "liquidity_usd": "50000"},
            )
        old_time = (datetime.now(UTC) - timedelta(minutes=20)).isoformat()
        journal._connection.execute("UPDATE discovery_candidates SET last_seen=?", (old_time,))
        trading = cast(TradingService, FakeAuditTrading())
        rpc = cast(RpcClient, object())
        result = scan_candidates(settings=settings, rpc=rpc, trading=trading, journal=journal)
        assert result["watchlist_reaudits"] == 1
        assert result["new_discoveries_scanned"] == 0
        assert checked == [mint]
        refreshed = next(c for c in journal.discovery_candidates(limit=100) if c["mint"] == mint)
        assert refreshed["last_seen"] != old_time

        journal._connection.execute(
            "UPDATE discovery_candidates SET last_seen=? WHERE mint=?", (old_time, mint)
        )
        fail = True
        result = scan_candidates(settings=settings, rpc=rpc, trading=trading, journal=journal)
        assert result["watchlist_reaudits"] == 1
        assert checked == [mint, mint]
        stale = next(c for c in journal.discovery_candidates(limit=100) if c["mint"] == mint)
        assert stale["audit_status"] == "safe"
        assert stale["last_seen"] == old_time
        assert "re-audit failed" in str(result["warning"])


def test_scan_prioritizes_fresh_market_candidates_within_spare_audit_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    older = [str(Pubkey.new_unique()) for _ in range(8)]
    priority = str(Pubkey.new_unique())
    pool = str(Pubkey.new_unique())
    checked: list[str] = []

    class FakeDex:
        def __enter__(self) -> FakeDex:
            return self

        def __exit__(self, *_: object) -> None:
            return None

        def tokens(self, _mints: list[str]) -> dict[str, object]:
            return {}

    class FakeAuditTrading:
        def audit(self, mint: str) -> TokenAudit:
            checked.append(mint)
            return TokenAudit(mint, TOKEN_PROGRAM_ID, 6, 10_000, 1_000)

    monkeypatch.setattr("memecoin_bot.paper.discover_recent_mints", lambda *_a, **_k: [])
    monkeypatch.setattr("memecoin_bot.paper.DexScreenerClient", FakeDex)
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
        state_path=tmp_path / "priority-audit.sqlite3",
        dexscreener_discovery_enabled=False,
    )
    now = datetime.now(UTC)
    with TradeJournal(settings.state_path) as journal:
        for index, mint in enumerate([*older, priority]):
            journal.upsert_discovery_candidate(
                mint=mint,
                source_program="test",
                audit_status="safe",
                audit_summary=None,
                top10_concentration_bps=1_000,
                decimals=6,
                market={"pair_address": pool, "price_usd": "1", "liquidity_usd": "50000"},
            )
            age_minutes = 30 - index if mint != priority else 3
            journal._connection.execute(
                "UPDATE discovery_candidates SET last_seen=? WHERE mint=?",
                ((now - timedelta(minutes=age_minutes)).isoformat(), mint),
            )
        kwargs = {
            "settings": settings,
            "rpc": cast(RpcClient, object()),
            "trading": cast(TradingService, FakeAuditTrading()),
            "journal": journal,
            "reaudit_priority": lambda item: item["mint"] == priority,
        }
        first = scan_candidates(**kwargs)
        assert first["watchlist_reaudits"] == 8
        assert first["priority_watchlist_reaudits"] == 1
        assert checked[0] == priority
        assert older[-1] not in checked

        checked.clear()
        journal._connection.execute(
            "UPDATE discovery_candidates SET last_seen=?",
            ((now - timedelta(minutes=20)).isoformat(),),
        )
        journal._connection.execute(
            "UPDATE market_snapshots SET captured_at=? WHERE mint=?",
            (int(now.timestamp()) - 600, priority),
        )
        second = scan_candidates(**kwargs)
        assert second["watchlist_reaudits"] == 8
        assert second["priority_watchlist_reaudits"] == 0
        assert checked == sorted([*older, priority])[:8]

        checked.clear()
        journal._connection.execute(
            "UPDATE discovery_candidates SET last_seen=?",
            ((now - timedelta(minutes=20)).isoformat(),),
        )
        journal._connection.execute(
            "UPDATE market_snapshots SET captured_at=? WHERE mint=?",
            (int(now.timestamp()), priority),
        )
        journal.set_runtime_state(
            "ohlcv_active_focus", {"mints": [priority], "selected_at": int(now.timestamp())}
        )
        kwargs["reaudit_priority"] = lambda _item: True
        focused = scan_candidates(**kwargs)
        assert focused["watchlist_reaudits"] == 8
        assert focused["priority_watchlist_reaudits"] == 8
        assert checked[0] == priority


def test_scan_refreshes_research_audit_after_token_leaves_recent_view(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cohort_mint = str(Pubkey.new_unique())
    checked: list[str] = []
    fail = False

    class FakeDex:
        def __enter__(self) -> FakeDex:
            return self

        def __exit__(self, *_: object) -> None:
            return None

        def tokens(self, mints: list[str]) -> dict[str, object]:
            assert len(mints) <= 30
            return {}

    class FakeAuditTrading:
        def audit(self, mint: str) -> TokenAudit:
            checked.append(mint)
            if fail:
                raise RuntimeError("RPC unavailable")
            return TokenAudit(mint, TOKEN_PROGRAM_ID, 6, 10_000, 1_000)

    monkeypatch.setattr("memecoin_bot.paper.discover_recent_mints", lambda *_a, **_k: [])
    monkeypatch.setattr("memecoin_bot.paper.DexScreenerClient", FakeDex)
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
        state_path=tmp_path / "research.sqlite3",
        dexscreener_discovery_enabled=False,
    )
    with TradeJournal(settings.state_path) as journal:
        journal.upsert_discovery_candidate(
            mint=cohort_mint,
            source_program="test_research_source",
            audit_status="safe",
            audit_summary=None,
            top10_concentration_bps=1_000,
            decimals=6,
        )
        for _ in range(101):
            journal.upsert_discovery_candidate(
                mint=str(Pubkey.new_unique()),
                source_program="other",
                audit_status="blocked",
                audit_summary="blocked",
                top10_concentration_bps=9_000,
                decimals=6,
            )
        old_time = (datetime.now(UTC) - timedelta(minutes=20)).isoformat()
        journal._connection.execute(
            "UPDATE discovery_candidates SET last_seen=? WHERE mint=?",
            (old_time, cohort_mint),
        )
        journal._connection.execute("DELETE FROM audit_observations WHERE mint=?", (cohort_mint,))
        journal.set_runtime_state(
            RESEARCH_COHORT_KEY,
            {"mints": [cohort_mint], "expires_at": int(datetime.now(UTC).timestamp()) + 3600},
        )
        assert cohort_mint not in {str(c["mint"]) for c in journal.discovery_candidates(limit=100)}

        trading = cast(TradingService, FakeAuditTrading())
        rpc = cast(RpcClient, object())
        result = scan_candidates(settings=settings, rpc=rpc, trading=trading, journal=journal)
        assert result["new_discoveries_scanned"] == 0
        assert result["cohort_reaudits"] == 1
        assert checked == [cohort_mint]
        assert journal.audit_observations(cohort_mint, limit=1)[0]["audit_status"] == "safe"

        journal._connection.execute(
            "UPDATE discovery_candidates SET last_seen=? WHERE mint=?",
            (old_time, cohort_mint),
        )
        journal._connection.execute(
            "UPDATE audit_observations SET observed_at_us=? WHERE mint=?",
            (int((datetime.now(UTC) - timedelta(minutes=20)).timestamp() * 1_000_000), cohort_mint),
        )
        fail = True
        result = scan_candidates(settings=settings, rpc=rpc, trading=trading, journal=journal)
        assert result["cohort_reaudits"] == 1
        assert checked == [cohort_mint, cohort_mint]
        candidate = journal.discovery_candidate(cohort_mint)
        assert candidate is not None and candidate["audit_status"] == "safe"
        assert journal.audit_observations(cohort_mint, limit=1)[0]["audit_status"] == "safe"
        assert "re-audit failed" in str(result["warning"])


def test_scan_bounds_research_refresh_and_batches_market_requests(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source_mints = [str(Pubkey.new_unique()) for _ in range(20)]
    cohort_mints = [str(Pubkey.new_unique()) for _ in range(11)]
    watchlist_mint = str(Pubkey.new_unique())
    requested_batch_sizes: list[int] = []

    class FakeDex:
        def __enter__(self) -> FakeDex:
            return self

        def __exit__(self, *_: object) -> None:
            return None

        def tokens(self, mints: list[str]) -> dict[str, object]:
            requested_batch_sizes.append(len(mints))
            assert len(mints) <= 30
            return {}

    class FakeAuditTrading:
        def audit(self, mint: str) -> TokenAudit:
            return TokenAudit(mint, TOKEN_PROGRAM_ID, 6, 10_000, 1_000)

    monkeypatch.setattr("memecoin_bot.paper.discover_recent_mints", lambda *_a, **_k: source_mints)
    monkeypatch.setattr("memecoin_bot.paper.DexScreenerClient", FakeDex)
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
        state_path=tmp_path / "budget.sqlite3",
        dexscreener_discovery_enabled=False,
    )
    with TradeJournal(settings.state_path) as journal:
        for mint in cohort_mints:
            journal.upsert_discovery_candidate(
                mint=mint,
                source_program="cohort_source",
                audit_status="blocked",
                audit_summary="blocked",
                top10_concentration_bps=9_000,
                decimals=6,
            )
            journal._connection.execute("DELETE FROM audit_observations WHERE mint=?", (mint,))
        journal.upsert_discovery_candidate(
            mint=watchlist_mint,
            source_program="watchlist_source",
            audit_status="safe",
            audit_summary=None,
            top10_concentration_bps=1_000,
            decimals=6,
            market={"pair_address": str(Pubkey.new_unique())},
        )
        journal._connection.execute(
            "UPDATE discovery_candidates SET last_seen=? WHERE mint=?",
            ((datetime.now(UTC) - timedelta(minutes=20)).isoformat(), watchlist_mint),
        )
        journal.set_runtime_state(
            RESEARCH_COHORT_KEY,
            {"mints": cohort_mints, "expires_at": int(datetime.now(UTC).timestamp()) + 3600},
        )
        result = scan_candidates(
            settings=settings,
            rpc=cast(RpcClient, object()),
            trading=cast(TradingService, FakeAuditTrading()),
            journal=journal,
        )
        assert result["new_discoveries_scanned"] == 20
        assert result["cohort_reaudits"] == 10
        assert result["watchlist_reaudits"] == 1
        assert result["scanned"] == 31
        assert requested_batch_sizes == [30, 1]


@pytest.mark.parametrize("batch_mode", ["valid", "malformed", "unavailable"])
def test_scan_batches_mint_accounts_and_falls_back_safely(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, batch_mode: str
) -> None:
    mints = [str(Pubkey.new_unique()) for _ in range(2)]
    authority = str(Pubkey.new_unique())
    calls: list[str] = []

    def mint_account(mint: str) -> dict[str, object]:
        return {
            "owner": TOKEN_PROGRAM_ID,
            "data": {
                "parsed": {
                    "type": "mint",
                    "info": {
                        "supply": "10000",
                        "decimals": 6,
                        "mintAuthority": authority if mint == mints[1] else None,
                        "freezeAuthority": None,
                    },
                }
            },
        }

    class AuditRpc:
        def call(self, method: str, params: list[Any], *, attempts: int = 3) -> Any:
            del attempts
            calls.append(method)
            if method == "getMultipleAccounts":
                if batch_mode == "unavailable":
                    raise RpcError("batch unavailable")
                if batch_mode == "malformed":
                    return {"value": [mint_account(mints[0])]}
                return {"value": [mint_account(str(mint)) for mint in params[0]]}
            if method == "getAccountInfo":
                return {"value": mint_account(str(params[0]))}
            if method == "getTokenLargestAccounts":
                return {"value": [{"amount": "100"} for _ in range(10)]}
            raise AssertionError(method)

    class FakeDex:
        def __enter__(self) -> FakeDex:
            return self

        def __exit__(self, *_: object) -> None:
            return None

        def tokens(self, _mints: list[str]) -> dict[str, object]:
            return {}

    monkeypatch.setattr("memecoin_bot.paper.discover_recent_mints", lambda *_a, **_k: mints)
    monkeypatch.setattr("memecoin_bot.paper.DexScreenerClient", FakeDex)
    settings = Settings(
        rpc_url="https://rpc.example.test",
        keypair_path=None,
        jupiter_api_key=None,
        live_trading_enabled=False,
        max_trade_lamports=100_000_000,
        max_daily_lamports=200_000_000,
        max_slippage_bps=300,
        max_price_impact_bps=500,
        max_top10_concentration_bps=2_000,
        max_priority_fee_lamports=1_000_000,
        confirmation_timeout_seconds=45,
        allow_token_2022=False,
        state_path=tmp_path / f"batch-{batch_mode}.sqlite3",
        dexscreener_discovery_enabled=False,
        discovery_candidate_limit=2,
        token_forensics_enabled=False,
    )
    rpc = cast(RpcClient, AuditRpc())
    with TradeJournal(settings.state_path) as journal:
        trading = TradingService(
            settings=settings,
            rpc=rpc,
            jupiter=cast(JupiterClient, object()),
            keypair=cast(Any, object()),
            journal=journal,
        )
        result = scan_candidates(settings=settings, rpc=rpc, trading=trading, journal=journal)
        candidates = {str(row["mint"]): row for row in journal.discovery_candidates(limit=10)}

    assert result["safe"] == 1
    assert result["blocked"] == 1
    assert candidates[mints[0]]["audit_status"] == "safe"
    assert candidates[mints[1]]["audit_status"] == "blocked"
    assert calls.count("getMultipleAccounts") == 1
    assert calls.count("getTokenLargestAccounts") == 2
    if batch_mode == "valid":
        assert result["batched_mint_accounts"] == 2
        assert "getAccountInfo" not in calls
    else:
        assert result["batched_mint_accounts"] == 0
        assert calls.count("getAccountInfo") == 2


def test_scan_spends_origin_history_only_on_market_eligible_mints(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    low_mint, ready_mint = (str(Pubkey.new_unique()) for _ in range(2))
    traced: dict[str, bool] = {}

    class FakeDex:
        def __enter__(self) -> FakeDex:
            return self

        def __exit__(self, *_: object) -> None:
            return None

        def tokens(self, _mints: list[str]) -> dict[str, TokenMarket]:
            return {
                mint: TokenMarket(
                    mint, None, None, None, None, None, liquidity, "20000", None, None
                )
                for mint, liquidity in ((low_mint, "10000"), (ready_mint, "50000"))
            }

    class RecordingTrading(TradingService):
        def __init__(self) -> None:
            pass

        def audit(
            self,
            mint: str,
            *,
            mint_account: dict[str, Any] | None = None,
            origin_trace_enabled: bool = True,
        ) -> TokenAudit:
            del mint_account
            traced[mint] = origin_trace_enabled
            return TokenAudit(mint, TOKEN_PROGRAM_ID, 6, 10_000, 1_000)

    class MalformedBatchRpc:
        def call(self, method: str, _params: list[Any], *, attempts: int = 3) -> Any:
            del attempts
            assert method == "getMultipleAccounts"
            return {"value": []}

    monkeypatch.setattr(
        "memecoin_bot.paper.discover_recent_mints", lambda *_a, **_k: [low_mint, ready_mint]
    )
    monkeypatch.setattr("memecoin_bot.paper.DexScreenerClient", FakeDex)
    settings = Settings(
        rpc_url="https://rpc.example.test",
        keypair_path=None,
        jupiter_api_key=None,
        live_trading_enabled=False,
        max_trade_lamports=100_000_000,
        max_daily_lamports=200_000_000,
        max_slippage_bps=300,
        max_price_impact_bps=500,
        max_top10_concentration_bps=2_000,
        max_priority_fee_lamports=1_000_000,
        confirmation_timeout_seconds=45,
        allow_token_2022=False,
        state_path=tmp_path / "origin-priority.sqlite3",
        dexscreener_discovery_enabled=False,
        discovery_candidate_limit=2,
    )
    with TradeJournal(settings.state_path) as journal:
        result = scan_candidates(
            settings=settings,
            rpc=cast(RpcClient, MalformedBatchRpc()),
            trading=RecordingTrading(),
            journal=journal,
        )
    assert result["scanned"] == 2
    assert traced == {low_mint: False, ready_mint: True}


def test_interrupted_campaign_blocks_all_paper_mutations(tmp_path: Path) -> None:
    mint = str(Pubkey.new_unique())
    settings = Settings(
        rpc_url="https://rpc.example.test",
        keypair_path=None,
        jupiter_api_key=None,
        live_trading_enabled=False,
        max_trade_lamports=100,
        max_daily_lamports=200,
        max_slippage_bps=300,
        max_price_impact_bps=500,
        max_top10_concentration_bps=7_000,
        max_priority_fee_lamports=1_000_000,
        confirmation_timeout_seconds=45,
        allow_token_2022=False,
        state_path=tmp_path / "state.sqlite3",
    )
    with TradeJournal(settings.state_path) as journal:
        journal.set_runtime_state(
            "paper_campaign",
            {"mode": "forward_paper_only", "status": "interrupted"},
        )
        service = PaperTradingService(
            settings=settings,
            trading=cast(TradingService, FakeTrading(mint)),
            jupiter=cast(JupiterClient, FakeJupiter(mint)),
            journal=journal,
            taker_pubkey=str(Pubkey.new_unique()),
        )
        with pytest.raises(PaperTradingError, match="interrupted"):
            service.open(mint=mint, lamports=50, slippage_bps=100)
        with pytest.raises(PaperTradingError, match="interrupted"):
            service.close(position_id="any", slippage_bps=100)
        with pytest.raises(PaperTradingError, match="interrupted"):
            service.take_partial(position_id="any", sell_bps_of_original=5_000, slippage_bps=100)
        with pytest.raises(PaperTradingError, match="interrupted"):
            service.portfolio(refresh=True)
        assert journal.paper_positions() == []
        assert journal.paper_fills() == []


def _seed_healthy_forward_workers(journal: TradeJournal) -> None:
    completed_at = datetime.now(UTC).isoformat()
    shared: dict[str, object] = {
        "state": "running",
        "running": True,
        "run_id": "test-run",
        "last_completed_run_id": "test-run",
        "last_completed_at": completed_at,
        "last_error": None,
    }
    journal.set_runtime_state(
        "paper_exit_monitor",
        {**shared, "last_result": {"quote_failures": 0, "exit_failures": 0}},
    )
    journal.set_runtime_state("paper_watchdog", shared)


def test_paper_service_open_mark_and_close(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    mint = str(Pubkey.new_unique())
    pool = str(Pubkey.new_unique())
    settings = Settings(
        rpc_url="https://rpc.example.test",
        keypair_path=None,
        jupiter_api_key=None,
        live_trading_enabled=False,
        max_trade_lamports=100,
        max_daily_lamports=200,
        max_slippage_bps=300,
        max_price_impact_bps=500,
        max_top10_concentration_bps=7_000,
        max_priority_fee_lamports=1_000_000,
        confirmation_timeout_seconds=45,
        allow_token_2022=False,
        state_path=tmp_path / "state.sqlite3",
        paper_starting_lamports=100,
        paper_execution_base_slippage_bps=0,
        paper_execution_network_fee_lamports=0,
        paper_execution_priority_fee_lamports=0,
        paper_execution_ata_rent_lamports=0,
    )
    with TradeJournal(settings.state_path) as journal:
        journal.upsert_discovery_candidate(
            mint=mint,
            source_program="test",
            audit_status="safe",
            audit_summary=None,
            top10_concentration_bps=1_000,
            decimals=6,
            market={
                "pair_address": pool,
                "price_usd": "1",
                "liquidity_usd": "100000",
            },
        )
        service = PaperTradingService(
            settings=settings,
            trading=cast(TradingService, FakeTrading(mint)),
            jupiter=cast(JupiterClient, FakeJupiter(mint)),
            journal=journal,
            taker_pubkey=str(Pubkey.new_unique()),
        )
        with pytest.raises(PaperTradingError, match="complete decision evidence"):
            service.open(
                mint=mint,
                lamports=50,
                slippage_bps=100,
                decision=StrategyDecision(mint, 80, "eligible", ("test",)),
                entry_source="autonomous",
            )
        assert journal.paper_positions() == []
        with monkeypatch.context() as patch:
            patch.setattr(
                "memecoin_bot.strategy.paper_entry_breakers",
                lambda _settings, _journal: ["daily_loss_limit"],
            )
            with pytest.raises(PaperTradingError, match="daily_loss_limit"):
                service.open(mint=mint, lamports=50, slippage_bps=100)
        assert journal.paper_positions() == []
        assert journal.paper_fills() == []
        journal._connection.execute(
            "UPDATE market_snapshots SET captured_at=? WHERE mint=?",
            (int(datetime.now(UTC).timestamp()) - 600, mint),
        )
        with pytest.raises(PaperExecutionError, match="visible pool liquidity"):
            service.open(mint=mint, lamports=50, slippage_bps=100)
        assert journal.paper_positions() == []
        journal._connection.execute(
            "UPDATE market_snapshots SET captured_at=? WHERE mint=?",
            (int(datetime.now(UTC).timestamp()), mint),
        )
        opened = service.open(mint=mint, lamports=50, slippage_bps=100)
        position_id = str(opened["position"]["position_id"])
        assert opened["execution"]["quote_source"] == "v2"
        decisions = journal.strategy_decisions()
        assert len(decisions) == 1
        assert decisions[0]["action"] == "manual_paper_opened"
        assert decisions[0]["verdict"] == "observe"
        assert decisions[0]["paper_position_id"] == position_id

        unmarked = service.portfolio(refresh=False)
        assert unmarked["cash_lamports"] == 50
        assert unmarked["equity_lamports"] == 50  # Conservative lower bound for risk gates.
        assert unmarked["unvalued_position_count"] == 1
        assert unmarked["all_positions_have_marks"] is False
        assert unmarked["total_pnl_lamports"] is None
        assert unmarked["positions"][0]["current_value_lamports"] is None
        assert unmarked["positions"][0]["pnl_lamports"] is None
        assert unmarked["positions"][0]["pnl_bps"] is None
        assert "open_position_mark_stale" in paper_entry_breakers(settings, journal)
        with monkeypatch.context() as patch:

            def unavailable(*_args: object, **_kwargs: object) -> None:
                raise JupiterError("route unavailable")

            patch.setattr(service.jupiter, "quote", unavailable)
            failed_first_mark = service.portfolio(refresh=True)
            assert failed_first_mark["total_pnl_lamports"] is None
            assert failed_first_mark["unvalued_position_count"] == 1
            assert failed_first_mark["positions"][0]["quote_state"] == "stale"
            assert failed_first_mark["positions"][0]["quote_error"] == "JupiterError"

        marked = service.portfolio(refresh=True)
        assert marked["cash_lamports"] == 50
        assert marked["equity_lamports"] == 110
        assert marked["equity_if_stale_zero_lamports"] == 110
        assert marked["stale_mark_count"] == 0
        assert marked["total_pnl_lamports"] == 10
        assert marked["all_positions_have_marks"] is True
        assert marked["unvalued_position_count"] == 0
        assert marked["positions"][0]["quote_state"] == "live"
        assert marked["execution_summary"]["fill_count"] == 1
        first_mark = journal.paper_position_marks(position_id)
        assert len(first_mark) == 1
        assert first_mark[0]["value_lamports"] == 60
        assert first_mark[0]["execution"]["side"] == "mark"
        assert first_mark[0]["execution"]["cash_credit_lamports"] == 60
        assert first_mark[0]["execution"]["quote_source"] == "v2"
        assert first_mark[0]["execution"]["route_quote_attempts"] == 1
        assert service.portfolio(refresh=False)["positions"][0]["quote_state"] == "recent_mark"
        with monkeypatch.context() as patch:

            def unavailable(*_args: object, **_kwargs: object) -> None:
                raise JupiterError("route unavailable")

            patch.setattr(service.jupiter, "quote", unavailable)
            failed_mark = service.portfolio(refresh=True)["positions"][0]
            assert failed_mark["quote_state"] == "stale"
            assert failed_mark["quote_error"] == "JupiterError"
        with monkeypatch.context() as patch:
            original_estimate = service.execution.estimate
            attempts: list[int] = []

            def stale_once(**kwargs: Any) -> Any:
                attempts.append(1)
                if len(attempts) == 1:
                    raise PaperExecutionError("Route is stale (6000 ms; maximum 5000 ms)")
                return original_estimate(**kwargs)

            patch.setattr(service.execution, "estimate", stale_once)
            retried_mark = service.portfolio(refresh=True)["positions"][0]
            assert retried_mark["quote_state"] == "live"
            assert retried_mark["last_mark_execution"]["route_quote_attempts"] == 2
            assert len(attempts) == 2
        with monkeypatch.context() as patch:
            attempts = []

            def stale_route(*_args: object, **_kwargs: object) -> None:
                attempts.append(1)
                raise PaperExecutionError("Route is stale (6000 ms; maximum 5000 ms)")

            patch.setattr(service.execution, "estimate", stale_route)
            failed_mark = service.portfolio(refresh=True)["positions"][0]
            assert failed_mark["quote_error_reason"] == "stale_route"
            assert len(attempts) == 2
        with monkeypatch.context() as patch:
            patch.setattr(
                service.execution,
                "settings",
                replace(settings, paper_execution_network_fee_lamports=100),
            )
            uneconomic = service.portfolio(refresh=True)
            failed_mark = uneconomic["positions"][0]
            assert failed_mark["quote_state"] == "stale"
            assert failed_mark["quote_error_reason"] == "below_exit_fees"
            assert failed_mark["quote_gross_out_lamports"] == 60
            assert uneconomic["stale_mark_value_lamports"] == 60
        journal.set_runtime_state(
            "paper_quote_health",
            {
                "unavailable": {
                    position_id: {
                        "error_type": "UneconomicPaperExitError",
                        "error_reason": "below_exit_fees",
                        "quote_gross_out_lamports": 60,
                        "last_quoted_at": journal.paper_position(position_id)["last_quoted_at"],
                    }
                }
            },
        )
        assert service.portfolio(refresh=False)["positions"][0]["quote_state"] == "stale"
        assert len(journal.paper_position_marks(position_id)) == 2
        journal._connection.execute(
            "UPDATE paper_positions SET last_quoted_at=? WHERE position_id=?",
            ("2020-01-01T00:00:00+00:00", position_id),
        )
        stale_portfolio = service.portfolio(refresh=False)
        assert stale_portfolio["positions"][0]["quote_state"] == "stale"
        assert stale_portfolio["stale_mark_count"] == 1
        assert stale_portfolio["stale_mark_value_lamports"] == 60
        assert stale_portfolio["equity_if_stale_zero_lamports"] == 50
        journal.set_runtime_state(
            "paper_quote_health",
            {
                "unavailable": {
                    position_id: {
                        "error_type": "UneconomicPaperExitError",
                        "error_reason": "below_exit_fees",
                        "quote_gross_out_lamports": 60,
                        "last_quoted_at": "2020-01-01T00:00:00+00:00",
                    }
                }
            },
        )
        saved_failure = service.portfolio(refresh=False)["positions"][0]
        assert saved_failure["quote_error_reason"] == "below_exit_fees"
        assert saved_failure["quote_gross_out_lamports"] == 60
        journal.set_runtime_state(
            "paper_quote_health",
            {
                "unavailable": {
                    position_id: {
                        "error_type": "PaperExecutionError",
                        "error_reason": "stale_route",
                        "last_quoted_at": "2020-01-01T00:00:00+00:00",
                    }
                }
            },
        )
        assert service.portfolio(refresh=False)["positions"][0]["quote_error_reason"] == (
            "stale_route"
        )

        with monkeypatch.context() as patch:
            original_estimate = service.execution.estimate
            attempts = []

            def stale_first_sell(**kwargs: Any) -> Any:
                attempts.append(1)
                if len(attempts) == 1:
                    raise PaperExecutionError("Route is stale (6000 ms; maximum 5000 ms)")
                return original_estimate(**kwargs)

            patch.setattr(service.execution, "estimate", stale_first_sell)
            partial = service.take_partial(
                position_id=position_id,
                sell_bps_of_original=5_000,
                slippage_bps=100,
            )
            assert partial["execution"]["route_quote_attempts"] == 2
            assert len(attempts) == 2
        assert partial["sold_token_amount"] == 500
        assert journal.paper_position(position_id)["last_quoted_at"] is None
        assert service.portfolio(refresh=False)["positions"][0]["quote_state"] == "stale"
        assert "open_position_mark_stale" in paper_entry_breakers(settings, journal)
        after_partial = service.portfolio(refresh=True)
        assert after_partial["cash_lamports"] == 80
        assert after_partial["open_value_lamports"] == 30
        assert after_partial["equity_lamports"] == 110
        assert after_partial["realized_pnl_lamports"] == 5
        assert after_partial["positions"][0]["remaining_bps"] == 5_000
        assert after_partial["positions"][0]["tp_stage"] == 1
        last_mark = journal.paper_position_marks(position_id)[-1]
        assert last_mark["remaining_token_amount"] == 500
        assert last_mark["tp_stage"] == 1
        assert last_mark["performance_value_lamports"] == 60

        fills_before_failed_exit = len(journal.paper_fills())
        with monkeypatch.context() as patch:
            attempts = []

            def stale_both_sells(**_kwargs: Any) -> None:
                attempts.append(1)
                raise PaperExecutionError("Route is stale (6000 ms; maximum 5000 ms)")

            patch.setattr(service.execution, "estimate", stale_both_sells)
            with pytest.raises(PaperExecutionError, match="Route is stale"):
                service.close(position_id=position_id, slippage_bps=100)
            assert len(attempts) == 2
        assert journal.paper_position(position_id)["status"] == "open"
        assert len(journal.paper_fills()) == fills_before_failed_exit

        with monkeypatch.context() as patch:
            original_estimate = service.execution.estimate
            attempts = []

            def stale_first_close(**kwargs: Any) -> Any:
                attempts.append(1)
                if len(attempts) == 1:
                    raise PaperExecutionError("Route is stale (6000 ms; maximum 5000 ms)")
                return original_estimate(**kwargs)

            patch.setattr(service.execution, "estimate", stale_first_close)
            closed_result = service.close(position_id=position_id, slippage_bps=100)
            assert closed_result["execution"]["route_quote_attempts"] == 2
            assert len(attempts) == 2
        closed = service.portfolio(refresh=False)
        assert closed["cash_lamports"] == 110
        assert closed["realized_pnl_lamports"] == 10
        assert closed["positions"][0]["status"] == "closed"

        journal.upsert_discovery_candidate(
            mint=mint,
            source_program="test",
            audit_status="safe",
            audit_summary=None,
            top10_concentration_bps=1_000,
            decimals=6,
            market={
                "pair_address": pool,
                "price_usd": "1",
                "liquidity_usd": "100000",
                "volume_24h_usd": "250000",
                "price_change_24h": "25",
            },
        )
        _seed_confirmed_candles(journal, settings, mint, pool)

        decision_time = datetime.now(UTC)
        evidence: dict[str, object] = {
            "market_policy_version": MARKET_POLICY_VERSION,
            "market_evaluated_at_ms": int(decision_time.timestamp() * 1_000),
            "market_gate_verdict": "eligible",
            "market_gate_score": 80,
            "market_policy": {
                "minimum_score": 70,
                "minimum_liquidity_usd": 10_000,
                "minimum_volume_24h_usd": 50_000,
                "maximum_absolute_24h_change_pct": 250,
            },
            "market_gate_inputs": {
                "mint": mint,
                "audit_status": "safe",
                "liquidity_usd": "20000",
                "volume_24h_usd": "100000",
                "price_change_24h": "10",
                "pair_created_at": None,
                "narrative_signal": None,
            },
            "decision_as_of": int(decision_time.timestamp()),
            "market_captured_at": int(decision_time.timestamp()),
            "audit_last_seen": decision_time.isoformat(),
            "candle_policy": {"signal_threshold": 75, "minimum_liquidity_usd": "10000"},
            "candle_signals": {
                frame: {"status": "ready", "confirmed": True, "score": 80}
                for frame in ("1m", "5m", "15m", "1h")
            },
        }
        journal.set_runtime_state("paper_campaign", {"mode": "forward_paper_only"})
        _seed_healthy_forward_workers(journal)
        before_rejected_entry = len(journal.paper_positions())
        with pytest.raises(PaperTradingError, match="fee-free built router quote"):
            service.open(
                mint=mint,
                lamports=50,
                slippage_bps=100,
                decision=StrategyDecision(mint, 80, "eligible", ("confirmed candles",), evidence),
                entry_source="autonomous",
            )
        assert len(journal.paper_positions()) == before_rejected_entry
        monkeypatch.setattr(
            service.trading,
            "buy",
            lambda **_kwargs: TradeReport(
                "paper-unmeasured-preview",
                TokenAudit(mint, TOKEN_PROGRAM_ID, 6, 1_000_000, 1_000),
                RouteQuote(
                    WSOL_MINT,
                    mint,
                    50,
                    1_000,
                    20,
                    "test",
                    0,
                    {"swapUsdValue": "1"},
                    "v2-build",
                ),
                TradeResult(None, 123, False, None),
            ),
        )
        with pytest.raises(PaperTradingError, match="measured token-account reserve"):
            service.open(
                mint=mint,
                lamports=50,
                slippage_bps=100,
                decision=StrategyDecision(mint, 80, "eligible", ("confirmed candles",), evidence),
                entry_source="autonomous",
            )
        monkeypatch.setattr(
            service.trading,
            "buy",
            lambda **_kwargs: TradeReport(
                "paper-overbudget-preview",
                TokenAudit(mint, TOKEN_PROGRAM_ID, 6, 1_000_000, 1_000),
                RouteQuote(
                    WSOL_MINT,
                    mint,
                    50,
                    1_000,
                    20,
                    "test",
                    0,
                    {"swapUsdValue": "1"},
                    "v2-build",
                ),
                TradeResult(None, 123, False, None, 7),
            ),
        )
        with pytest.raises(PaperTradingError, match="measured risk budget"):
            service.open(
                mint=mint,
                lamports=50,
                slippage_bps=100,
                decision=StrategyDecision(mint, 80, "eligible", ("confirmed candles",), evidence),
                entry_source="autonomous",
            )
        assert len(journal.paper_positions()) == before_rejected_entry
        journal.set_runtime_state("paper_campaign", {"mode": "fixture"})
        monkeypatch.setattr(
            service.trading,
            "buy",
            lambda **_kwargs: TradeReport(
                "paper-repeat-preview",
                TokenAudit(mint, TOKEN_PROGRAM_ID, 6, 1_000_000, 1_000),
                RouteQuote(WSOL_MINT, mint, 50, 1_000, 20, "test", 0, {"swapUsdValue": "1"}),
                TradeResult(None, 123, False, None, 7),
            ),
        )
        automatic = service.open(
            mint=mint,
            lamports=50,
            slippage_bps=100,
            decision=StrategyDecision(mint, 80, "eligible", ("confirmed candles",), evidence),
            entry_source="autonomous",
        )
        assert automatic["execution"]["ata_rent_lamports"] == 0
        wrong_market_evidence = {
            **evidence,
            "market_gate_inputs": {
                **cast(dict[str, object], evidence["market_gate_inputs"]),
                "mint": str(Pubkey.new_unique()),
            },
        }
        with pytest.raises(PaperTradingError, match="market evidence token mismatch"):
            service.open(
                mint=mint,
                lamports=50,
                slippage_bps=100,
                decision=StrategyDecision(
                    mint, 80, "eligible", ("confirmed candles",), wrong_market_evidence
                ),
                entry_source="autonomous",
            )
        auto_id = str(automatic["position"]["position_id"])
        linked = [
            item for item in journal.strategy_decisions() if item["paper_position_id"] == auto_id
        ]
        assert len(linked) == 1
        assert linked[0]["action"] == "paper_opened"


@pytest.mark.parametrize(
    ("route_has_usd", "price_block_id", "expect_stale"),
    [(True, 995, False), (False, 995, False), (False, 800, True)],
)
def test_forward_paper_open_persists_built_router_quote_evidence(
    tmp_path: Path, route_has_usd: bool, price_block_id: int, expect_stale: bool
) -> None:
    mint = str(Pubkey.new_unique())
    pool = str(Pubkey.new_unique())
    settings = Settings(
        rpc_url="https://rpc.example.test",
        keypair_path=None,
        jupiter_api_key=None,
        live_trading_enabled=False,
        max_trade_lamports=100,
        max_daily_lamports=200,
        max_slippage_bps=300,
        max_price_impact_bps=500,
        max_top10_concentration_bps=7_000,
        max_priority_fee_lamports=1_000_000,
        confirmation_timeout_seconds=45,
        allow_token_2022=False,
        state_path=tmp_path / "forward-paper.sqlite3",
        paper_starting_lamports=10_000_000,
        paper_execution_base_slippage_bps=0,
        paper_execution_network_fee_lamports=0,
        paper_execution_priority_fee_lamports=0,
        paper_execution_ata_rent_lamports=0,
    )

    class BuiltTrading(FakeTrading):
        def __init__(self, token_mint: str) -> None:
            super().__init__(token_mint)
            self.rpc = SimpleNamespace(call=lambda *_a, **_k: 1_000)

        def buy(self, **_: object) -> TradeReport:
            return TradeReport(
                "built-router-preview",
                TokenAudit(mint, TOKEN_PROGRAM_ID, 6, 1_000_000, 1_000),
                RouteQuote(
                    WSOL_MINT,
                    mint,
                    50,
                    1_000,
                    20,
                    "test",
                    0,
                    {"swapUsdValue": "1"} if route_has_usd else {},
                    "v2-build",
                ),
                TradeResult(None, 123, False, None, 0),
            )

    class PriceJupiter(FakeJupiter):
        def sol_usd_reference(self) -> tuple[Decimal, int]:
            assert not route_has_usd
            return Decimal("100"), price_block_id

    decision_time = datetime.now(UTC)
    evidence = {
        "market_policy_version": MARKET_POLICY_VERSION,
        "market_evaluated_at_ms": int(decision_time.timestamp() * 1_000),
        "market_gate_verdict": "eligible",
        "market_gate_score": 80,
        "market_policy": {
            "minimum_score": 70,
            "minimum_liquidity_usd": 10_000,
            "minimum_volume_24h_usd": 50_000,
            "maximum_absolute_24h_change_pct": 250,
        },
        "market_gate_inputs": {
            "mint": mint,
            "audit_status": "safe",
            "liquidity_usd": "100000",
            "volume_24h_usd": "100000",
            "price_change_24h": "10",
            "pair_created_at": None,
            "narrative_signal": None,
        },
        "decision_as_of": int(decision_time.timestamp()),
        "market_captured_at": int(decision_time.timestamp()),
        "audit_last_seen": decision_time.isoformat(),
        "candle_policy": {"signal_threshold": 75, "minimum_liquidity_usd": "10000"},
        "candle_signals": {
            frame: {"status": "ready", "confirmed": True, "score": 80}
            for frame in ("1m", "5m", "15m", "1h")
        },
    }
    with TradeJournal(settings.state_path) as journal:
        journal.upsert_discovery_candidate(
            mint=mint,
            source_program="test",
            audit_status="safe",
            audit_summary=None,
            top10_concentration_bps=1_000,
            decimals=6,
            market={
                "pair_address": pool,
                "price_usd": "1",
                "liquidity_usd": "100000",
                "volume_24h_usd": "250000",
                "price_change_24h": "25",
            },
        )
        _seed_confirmed_candles(journal, settings, mint, pool)
        journal.set_runtime_state("paper_campaign", {"mode": "forward_paper_only"})
        _seed_healthy_forward_workers(journal)
        paper = PaperTradingService(
            settings=settings,
            trading=cast(TradingService, BuiltTrading(mint)),
            jupiter=cast(JupiterClient, PriceJupiter(mint)),
            journal=journal,
            taker_pubkey=str(Pubkey.new_unique()),
        )

        def open_confirmed() -> dict[str, Any]:
            return paper.open(
                mint=mint,
                lamports=50,
                slippage_bps=100,
                decision=StrategyDecision(mint, 80, "eligible", ("confirmed candles",), evidence),
                entry_source="autonomous",
            )

        if expect_stale:
            with pytest.raises(PaperExecutionError, match="SOL USD reference is stale"):
                open_confirmed()
            assert journal.paper_positions() == []
            return
        opened = open_confirmed()
        assert opened["execution"]["quote_source"] == "v2-build"
        assert opened["execution"]["quote_fee_bps"] == 0
        assert opened["execution"]["swap_usd_source"] == (
            "router_quote" if route_has_usd else "jupiter_price_v3_sol_input"
        )
        if not route_has_usd:
            assert opened["execution"]["sol_price_checked_slot"] == 1_000
        assert opened["execution"]["entry_revalidation"]["candle_signals"]["1m"]["confirmed"]
        assert opened["execution"]["entry_revalidation"]["market_gate_inputs"]["mint"] == mint
        assert opened["execution"]["entry_revalidation"]["market_gate_verdict"] == "eligible"
        assert journal.paper_positions()[0]["entry_execution"]["quote_source"] == "v2-build"
        assert journal.paper_fills()[0]["execution"]["quote_source"] == "v2-build"
        stored_entry = journal._connection.execute(
            "SELECT entry_execution_json, opened_at FROM paper_positions LIMIT 1"
        ).fetchone()
        assert stored_entry is not None
        assert _entry_revalidation(stored_entry[0], stored_entry[1], mint)
        stale_evidence = {**evidence, "decision_as_of": 1}
        with pytest.raises(StalePaperDecisionError, match="too old"):
            paper.open(
                mint=mint,
                lamports=50,
                slippage_bps=100,
                decision=StrategyDecision(
                    mint, 80, "eligible", ("confirmed candles",), stale_evidence
                ),
                entry_source="autonomous",
            )


@pytest.mark.parametrize("change", ["blocked_audit", "revised_1m"])
def test_automated_paper_entry_rechecks_signal_after_preview(tmp_path: Path, change: str) -> None:
    mint = str(Pubkey.new_unique())
    pool = str(Pubkey.new_unique())
    settings = Settings(
        rpc_url="https://rpc.example.test",
        keypair_path=None,
        jupiter_api_key=None,
        live_trading_enabled=False,
        max_trade_lamports=100,
        max_daily_lamports=200,
        max_slippage_bps=300,
        max_price_impact_bps=500,
        max_top10_concentration_bps=7_000,
        max_priority_fee_lamports=1_000_000,
        confirmation_timeout_seconds=45,
        allow_token_2022=False,
        state_path=tmp_path / "changed-signal.sqlite3",
        paper_starting_lamports=10_000_000,
        paper_execution_base_slippage_bps=0,
        paper_execution_network_fee_lamports=0,
        paper_execution_priority_fee_lamports=0,
        paper_execution_ata_rent_lamports=0,
    )
    with TradeJournal(settings.state_path) as journal:
        journal.upsert_discovery_candidate(
            mint=mint,
            source_program="test",
            audit_status="safe",
            audit_summary=None,
            top10_concentration_bps=1_000,
            decimals=6,
            market={
                "pair_address": pool,
                "price_usd": "1",
                "liquidity_usd": "100000",
                "volume_24h_usd": "250000",
                "price_change_24h": "25",
            },
        )
        _seed_confirmed_candles(journal, settings, mint, pool)
        candidate = journal.discovery_candidates(limit=1)[0]
        initial = confirm_candidate_candles(
            candidate, evaluate_candidate(candidate, settings), settings, journal
        )
        assert initial.verdict == "eligible"
        journal.set_runtime_state("paper_campaign", {"mode": "forward_paper_only"})
        _seed_healthy_forward_workers(journal)

        class ChangingTrading(FakeTrading):
            def buy(self, **_: object) -> TradeReport:
                if change == "blocked_audit":
                    journal._connection.execute(
                        "UPDATE discovery_candidates SET audit_status='blocked' WHERE mint=?",
                        (mint,),
                    )
                else:
                    last = journal.pool_candles(mint, pool, "1m", limit=1)[0]
                    opened_at = last["opened_at"]
                    assert isinstance(opened_at, int)
                    journal.record_pool_candles(
                        [
                            PoolCandle(
                                mint=mint,
                                pool=pool,
                                timeframe="1m",
                                opened_at=opened_at,
                                open=str(last["open"]),
                                high=str(last["high"]),
                                low=str(last["low"]),
                                close=str(float(str(last["close"])) * 1.005),
                                volume_usd=str(last["volume_usd"]),
                            )
                        ],
                        fetched_at=int(datetime.now(UTC).timestamp()),
                    )
                return TradeReport(
                    "built-router-preview",
                    TokenAudit(mint, TOKEN_PROGRAM_ID, 6, 1_000_000, 1_000),
                    RouteQuote(
                        WSOL_MINT,
                        mint,
                        50,
                        1_000,
                        20,
                        "test",
                        0,
                        {"swapUsdValue": "1"},
                        "v2-build",
                    ),
                    TradeResult(None, 123, False, None, 0),
                )

        paper = PaperTradingService(
            settings=settings,
            trading=cast(TradingService, ChangingTrading(mint)),
            jupiter=cast(JupiterClient, FakeJupiter(mint)),
            journal=journal,
            taker_pubkey=str(Pubkey.new_unique()),
        )
        with pytest.raises(StalePaperDecisionError, match="no longer eligible"):
            paper.open(
                mint=mint,
                lamports=50,
                slippage_bps=100,
                decision=initial,
                entry_source="autonomous",
            )
        assert journal.paper_positions() == []
        assert journal.paper_fills() == []
