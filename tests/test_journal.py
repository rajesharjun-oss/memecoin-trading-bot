from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path
from typing import cast

import pytest
from solders.pubkey import Pubkey

from memecoin_bot.constants import WSOL_MINT
from memecoin_bot.journal import JournalError, TradeJournal
from memecoin_bot.ohlcv import PoolCandle
from memecoin_bot.strategy import StrategyDecision


def test_interrupted_campaign_rejects_late_paper_writes_atomically(tmp_path: Path) -> None:
    with TradeJournal(tmp_path / "state.sqlite3") as journal:
        mint = str(Pubkey.new_unique())
        journal.set_runtime_state(
            "paper_campaign", {"mode": "forward_paper_only", "status": "active"}
        )
        journal.open_paper_position(
            position_id="open",
            mint=mint,
            entry_lamports=100,
            token_amount=1_000,
            simulated_units=1_000,
            starting_lamports=1_000,
        )
        original_fills = len(journal.paper_fills())
        journal.set_runtime_state(
            "paper_campaign", {"mode": "forward_paper_only", "status": "interrupted"}
        )
        with pytest.raises(JournalError, match="interrupted"):
            journal.open_paper_position(
                position_id="late",
                mint=str(Pubkey.new_unique()),
                entry_lamports=100,
                token_amount=1_000,
                simulated_units=1_000,
                starting_lamports=1_000,
            )
        with pytest.raises(JournalError, match="interrupted"):
            journal.mark_paper_position("open", 90)
        with pytest.raises(JournalError, match="interrupted"):
            journal.close_paper_position("open", 90)
        assert journal.paper_position("open")["status"] == "open"
        assert len(journal.paper_fills()) == original_fills
        assert journal.paper_position_marks("open") == []


def test_order_ids_are_idempotent(tmp_path: Path) -> None:
    with TradeJournal(tmp_path / "state.sqlite3") as journal:
        output_mint = str(Pubkey.new_unique())
        journal.reserve(
            client_order_id="same-id",
            input_mint=WSOL_MINT,
            output_mint=output_mint,
            input_amount=10,
            max_daily_lamports=100,
        )
        with pytest.raises(JournalError, match="already been used"):
            journal.reserve(
                client_order_id="same-id",
                input_mint=WSOL_MINT,
                output_mint=output_mint,
                input_amount=10,
                max_daily_lamports=100,
            )


def test_collector_signal_observations_are_separate_from_paper_decisions(tmp_path: Path) -> None:
    path = tmp_path / "signals.sqlite3"
    mint = str(Pubkey.new_unique())
    with TradeJournal(path) as journal:
        journal.record_collector_signal_observation(
            StrategyDecision(mint, 74, "observe", ("5m signal unavailable",), {"pool": "pool"})
        )
        rows = journal.collector_signal_observations()
        assert len(rows) == 1
        assert rows[0]["mint"] == mint
        assert rows[0]["reasons"] == ["5m signal unavailable"]
        assert rows[0]["evidence"] == {"pool": "pool"}
        assert journal.strategy_decisions() == []
        with pytest.raises(JournalError, match="limit"):
            journal.collector_signal_observations(limit=101)
    with TradeJournal(path, readonly=True) as journal:
        assert len(journal.collector_signal_observations()) == 1


def test_candle_first_fetch_preserves_provenance_and_legacy_unknown(tmp_path: Path) -> None:
    path = tmp_path / "legacy.sqlite3"
    mint = str(Pubkey.new_unique())
    pool = str(Pubkey.new_unique())
    with sqlite3.connect(path) as connection:
        connection.execute(
            """CREATE TABLE pool_candles (
                mint TEXT NOT NULL, pool TEXT NOT NULL, timeframe TEXT NOT NULL,
                opened_at INTEGER NOT NULL, open_usd TEXT NOT NULL,
                high_usd TEXT NOT NULL, low_usd TEXT NOT NULL, close_usd TEXT NOT NULL,
                volume_usd TEXT NOT NULL, fetched_at INTEGER NOT NULL,
                PRIMARY KEY (mint, pool, timeframe, opened_at)
            )"""
        )
        connection.execute(
            "INSERT INTO pool_candles VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (mint, pool, "1m", 60, "1", "1", "1", "1", "100", 120),
        )

    with TradeJournal(path) as journal:
        rows = journal.pool_candles(mint, pool, "1m")
        assert rows[0]["first_fetched_at"] is None
        assert rows[0]["first_observed_values_at"] is None
        candle = PoolCandle(mint, pool, "1m", 120, "1", "1", "1", "1", "100")
        assert journal.record_pool_candles([candle], fetched_at=180) == 1
        assert (
            journal.record_pool_candles(
                [PoolCandle(mint, pool, "1m", 120, "2", "2", "2", "2", "200")],
                fetched_at=240,
            )
            == 0
        )
        assert (
            journal.record_pool_candles(
                [PoolCandle(mint, pool, "1m", 60, "1", "1", "1", "1", "100")],
                fetched_at=240,
            )
            == 0
        )
        assert journal.record_pool_candles([], fetched_at=240) == 0
        rows = journal.pool_candles(mint, pool, "1m")
        assert rows[0]["first_fetched_at"] is None
        assert rows[0]["first_observed_values_at"] is None
        assert rows[0]["fetched_at"] == 240
        assert rows[1]["first_fetched_at"] == 180
        assert rows[1]["first_observed_values_at"] == 180
        assert rows[1]["fetched_at"] == 240
        assert rows[1]["close"] == "1"
        assert rows[1]["volume_usd"] == "100"


def test_market_snapshot_migration_preserves_unknown_historical_inputs(tmp_path: Path) -> None:
    path = tmp_path / "legacy-market.sqlite3"
    with sqlite3.connect(path) as connection:
        connection.execute(
            """CREATE TABLE market_snapshots (
                mint TEXT NOT NULL, observed_at INTEGER NOT NULL,
                captured_at INTEGER, pool TEXT,
                price_usd TEXT NOT NULL, liquidity_usd TEXT NOT NULL,
                volume_24h_usd TEXT,
                PRIMARY KEY (mint, observed_at)
            )"""
        )
        connection.execute(
            "INSERT INTO market_snapshots VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("mint", 60, 61, "pool", "1", "1000", "2000"),
        )

    with TradeJournal(path) as journal:
        old = journal.market_snapshot_before("mint", captured_at=61)
        assert old is not None
        assert old["price_change_24h"] is None
        assert old["pair_created_at"] is None
        journal.record_market_snapshot(
            mint="mint",
            pool="pool",
            price_usd="1.1",
            liquidity_usd="1200",
            volume_24h_usd="2400",
            price_change_24h="5.5",
            pair_created_at=1_700_000_000_000,
            observed_at=120,
            source="geckoterminal_pool",
        )
        new = journal.market_snapshot_before("mint", captured_at=120)
        assert new is not None
        assert new["price_change_24h"] == "5.5"
        assert new["pair_created_at"] == 1_700_000_000_000
        assert journal.market_snapshots("mint")[0]["price_change_24h"] is None
        assert [row["source"] for row in journal.market_snapshots("mint")] == [
            "legacy_unknown",
            "geckoterminal_pool",
        ]


def test_readonly_market_snapshots_support_pre_source_schema(tmp_path: Path) -> None:
    path = tmp_path / "pre-source.sqlite3"
    with sqlite3.connect(path) as connection:
        connection.execute(
            """CREATE TABLE market_snapshots (
                mint TEXT NOT NULL, observed_at INTEGER NOT NULL,
                captured_at INTEGER, pool TEXT, price_usd TEXT NOT NULL,
                liquidity_usd TEXT NOT NULL, volume_24h_usd TEXT,
                price_change_24h TEXT, pair_created_at INTEGER,
                PRIMARY KEY (mint, observed_at)
            )"""
        )
        connection.execute(
            "INSERT INTO market_snapshots VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("mint", 60, 60, "pool", "1", "1000", "2000", "5", 1),
        )

    with TradeJournal(path, readonly=True) as journal:
        assert journal.market_snapshots("mint") == [
            {
                "observed_at": 60,
                "captured_at": 60,
                "pool": "pool",
                "price_usd": "1",
                "liquidity_usd": "1000",
                "volume_24h_usd": "2000",
                "price_change_24h": "5",
                "pair_created_at": 1,
                "source": "legacy_unknown",
            }
        ]


def test_market_snapshots_preserve_within_minute_point_in_time_values(tmp_path: Path) -> None:
    with TradeJournal(tmp_path / "market-history.sqlite3") as journal:
        for captured_at, price in ((120, "1"), (150, "2"), (150, "999")):
            journal.record_market_snapshot(
                mint="mint",
                pool="pool",
                price_usd=price,
                liquidity_usd="30000",
                volume_24h_usd="100000",
                price_change_24h="5",
                pair_created_at=1_700_000_000_000,
                observed_at=captured_at,
            )
        rows = journal.market_snapshots("mint")
        early = journal.market_snapshot_before("mint", captured_at=130)
        later = journal.market_snapshot_before("mint", captured_at=160)

    assert [(row["observed_at"], row["price_usd"]) for row in rows] == [
        (120, "1"),
        (150, "2"),
    ]
    assert early is not None and early["price_usd"] == "1"
    assert later is not None and later["price_usd"] == "2"


def test_first_discovery_preserves_market_gate_inputs(tmp_path: Path) -> None:
    mint = str(Pubkey.new_unique())
    pool = str(Pubkey.new_unique())
    with TradeJournal(tmp_path / "discovery.sqlite3") as journal:
        journal.upsert_discovery_candidate(
            mint=mint,
            source_program="test",
            audit_status="safe",
            audit_summary=None,
            top10_concentration_bps=None,
            decimals=6,
            market={
                "pair_address": pool,
                "price_usd": "0.01",
                "liquidity_usd": "25000",
                "volume_24h_usd": "75000",
                "price_change_24h": "-12.5",
                "pair_created_at": 1_700_000_000_000,
            },
        )
        snapshot = journal.market_snapshots(mint)[-1]
        assert snapshot["pool"] == pool
        assert snapshot["price_change_24h"] == "-12.5"
        assert snapshot["pair_created_at"] == 1_700_000_000_000


def test_readonly_journal_rejects_writes(tmp_path: Path) -> None:
    path = tmp_path / "readonly.sqlite3"
    with TradeJournal(path) as journal:
        journal.set_runtime_state("probe", {"value": 1})
    with TradeJournal(path, readonly=True) as journal:
        assert (journal.runtime_state("probe") or {}).get("value") == 1
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            journal.set_runtime_state("probe", {"value": 2})
    with TradeJournal(path, readonly=True) as journal:
        assert (journal.runtime_state("probe") or {}).get("value") == 1


def test_daily_exposure_counts_buys_not_exits(tmp_path: Path) -> None:
    mint = str(Pubkey.new_unique())
    with TradeJournal(tmp_path / "state.sqlite3") as journal:
        journal.reserve(
            client_order_id="buy-1",
            input_mint=WSOL_MINT,
            output_mint=mint,
            input_amount=60,
            max_daily_lamports=100,
        )
        journal.reserve(
            client_order_id="sell-1",
            input_mint=mint,
            output_mint=WSOL_MINT,
            input_amount=10_000_000,
            max_daily_lamports=100,
        )
        with pytest.raises(JournalError, match="Daily SOL exposure"):
            journal.reserve(
                client_order_id="buy-2",
                input_mint=WSOL_MINT,
                output_mint=mint,
                input_amount=41,
                max_daily_lamports=100,
            )


def test_position_id_cannot_be_rebound(tmp_path: Path) -> None:
    mint = str(Pubkey.new_unique())
    wallet = str(Pubkey.new_unique())
    with TradeJournal(tmp_path / "state.sqlite3") as journal:
        journal.ensure_position(
            position_id="p1",
            mint=mint,
            wallet=wallet,
            entry_lamports=10,
            initial_token_amount=20,
        )
        with pytest.raises(JournalError, match="different position"):
            journal.ensure_position(
                position_id="p1",
                mint=mint,
                wallet=wallet,
                entry_lamports=11,
                initial_token_amount=20,
            )


def test_paper_portfolio_cash_and_close_are_atomic(tmp_path: Path) -> None:
    mint = str(Pubkey.new_unique())
    with TradeJournal(tmp_path / "state.sqlite3") as journal:
        assert not journal.paper_mint_has_position(mint)
        opened = journal.open_paper_position(
            position_id="paper-one",
            mint=mint,
            entry_lamports=60,
            token_amount=1_000,
            simulated_units=123,
            starting_lamports=100,
        )
        assert opened["status"] == "open"
        assert journal.paper_mint_has_position(mint)
        assert journal.paper_cash_lamports(100) == 40

        with pytest.raises(JournalError, match="insufficient"):
            journal.open_paper_position(
                position_id="paper-two",
                mint=mint,
                entry_lamports=41,
                token_amount=500,
                simulated_units=100,
                starting_lamports=100,
            )

        journal.mark_paper_position("paper-one", 75)
        assert journal.paper_position("paper-one")["peak_value_lamports"] == 75
        first_marks = journal.paper_position_marks("paper-one")
        assert len(first_marks) == 1
        assert first_marks[0]["value_lamports"] == 75
        assert first_marks[0]["performance_value_lamports"] == 75
        assert first_marks[0]["remaining_token_amount"] == 1_000
        assert first_marks[0]["tp_stage"] == 0
        partial = journal.partial_close_paper_position(
            "paper-one",
            sold_token_amount=500,
            exit_lamports=40,
            advance_tp_stage=True,
        )
        assert partial["status"] == "open"
        assert partial["remaining_token_amount"] == 500
        assert partial["realized_lamports"] == 40
        assert partial["realized_cost_lamports"] == 30
        assert partial["tp_stage"] == 1
        assert partial["last_exit_reason"] == "manual_partial"
        assert partial["last_value_lamports"] == 40
        assert journal.paper_cash_lamports(100) == 80

        journal.mark_paper_position(
            "paper-one", 38, peak_reference_lamports=76, execution={"side": "mark"}
        )
        second_marks = journal.paper_position_marks("paper-one")
        assert len(second_marks) == 2
        assert second_marks[-1]["remaining_token_amount"] == 500
        assert second_marks[-1]["performance_value_lamports"] == 76
        assert second_marks[-1]["peak_value_lamports"] == 76
        assert second_marks[-1]["tp_stage"] == 1
        assert second_marks[-1]["execution"] == {"side": "mark"}

        closed = journal.close_paper_position("paper-one", 70)
        assert closed["status"] == "closed"
        assert closed["closed_lamports"] == 110
        assert closed["remaining_token_amount"] == 0
        assert closed["realized_cost_lamports"] == 60
        assert closed["last_exit_reason"] == "manual"
        assert [fill["exit_reason"] for fill in journal.paper_fills()[:2]] == [
            "manual",
            "manual_partial",
        ]
        assert journal.paper_cash_lamports(100) == 150

        with pytest.raises(JournalError, match="already closed"):
            journal.close_paper_position("paper-one", 70)


def test_paper_mark_and_history_write_are_atomic(tmp_path: Path) -> None:
    mint = str(Pubkey.new_unique())
    with TradeJournal(tmp_path / "atomic-mark.sqlite3") as journal:
        journal.open_paper_position(
            position_id="paper-one",
            mint=mint,
            entry_lamports=60,
            token_amount=1_000,
            simulated_units=123,
            starting_lamports=100,
        )
        journal.mark_paper_position("paper-one", 75, execution={"side": "mark"})
        original = journal.paper_position("paper-one")
        journal._connection.execute(
            """CREATE TRIGGER reject_paper_mark BEFORE INSERT ON paper_position_marks
               BEGIN SELECT RAISE(ABORT, 'mark unavailable'); END"""
        )
        with pytest.raises(sqlite3.IntegrityError, match="mark unavailable"):
            journal.mark_paper_position("paper-one", 90)
        after = journal.paper_position("paper-one")
        assert after["last_value_lamports"] == original["last_value_lamports"]
        assert after["peak_value_lamports"] == original["peak_value_lamports"]
        assert after["last_quoted_at"] == original["last_quoted_at"]
        assert len(journal.paper_position_marks("paper-one")) == 1


def test_paper_entry_commits_position_fill_and_decision_together(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    mint = str(Pubkey.new_unique())
    other_mint = str(Pubkey.new_unique())
    decision = StrategyDecision(mint, 82, "eligible", ("confirmed candles",), {"source": "test"})
    with TradeJournal(tmp_path / "atomic-entry.sqlite3") as journal:
        opened = journal.open_paper_position(
            position_id="atomic-one",
            mint=mint,
            entry_lamports=50,
            token_amount=100,
            simulated_units=1,
            starting_lamports=200,
            decision=decision,
            decision_action="paper_opened",
            max_open_positions=1,
        )
        assert opened["status"] == "open"
        assert len(journal.paper_fills()) == 1
        linked = journal.strategy_decisions()
        assert len(linked) == 1
        assert linked[0]["paper_position_id"] == "atomic-one"
        recorded_second = int(datetime.fromisoformat(str(linked[0]["created_at"])).timestamp())
        assert journal.strategy_decisions(before=recorded_second - 1) == []
        assert len(journal.strategy_decisions(before=recorded_second + 1)) == 1
        with pytest.raises(JournalError, match="cutoff"):
            journal.strategy_decisions(before=0)

        with pytest.raises(JournalError, match="already open"):
            journal.open_paper_position(
                position_id="atomic-duplicate",
                mint=mint,
                entry_lamports=50,
                token_amount=100,
                simulated_units=1,
                starting_lamports=200,
                max_open_positions=1,
            )
        with pytest.raises(JournalError, match="limit"):
            journal.open_paper_position(
                position_id="atomic-over-limit",
                mint=other_mint,
                entry_lamports=50,
                token_amount=100,
                simulated_units=1,
                starting_lamports=200,
                max_open_positions=1,
            )
        assert len(journal.paper_positions()) == 1
        assert len(journal.paper_fills()) == 1

        journal.close_paper_position("atomic-one", 50)

        def fail_decision(*_args: object, **_kwargs: object) -> None:
            raise JournalError("decision write failed")

        monkeypatch.setattr(journal, "record_strategy_decision", fail_decision)
        with pytest.raises(JournalError, match="decision write failed"):
            journal.open_paper_position(
                position_id="atomic-rolled-back",
                mint=other_mint,
                entry_lamports=50,
                token_amount=100,
                simulated_units=1,
                starting_lamports=200,
                decision=StrategyDecision(other_mint, 80, "eligible", ("confirmed",)),
                decision_action="paper_opened",
                max_open_positions=1,
            )
        assert {item["position_id"] for item in journal.paper_positions()} == {"atomic-one"}
        assert len(journal.paper_fills()) == 2

        def deny_inside_transaction() -> None:
            assert journal._connection.in_transaction
            raise JournalError("risk changed while quoting")

        with pytest.raises(JournalError, match="risk changed while quoting"):
            journal.open_paper_position(
                position_id="atomic-risk-denied",
                mint=other_mint,
                entry_lamports=50,
                token_amount=100,
                simulated_units=1,
                starting_lamports=200,
                max_open_positions=1,
                entry_guard=deny_inside_transaction,
            )
        assert len(journal.paper_fills()) == 2


def test_automation_events_and_call_inbox_are_persisted(tmp_path: Path) -> None:
    mint = str(Pubkey.new_unique())
    with TradeJournal(tmp_path / "state.sqlite3") as journal:
        journal.record_automation_event(
            event_type="cycle_completed",
            message="Cycle completed without an entry",
            metadata={"scanned": 3},
        )
        call_id = journal.create_paper_call(mint=mint, source_label="Watchlist")
        journal.update_paper_call(
            call_id,
            status="rejected",
            score=42,
            verdict="reject",
            reason="liquidity below minimum",
        )

        assert journal.automation_events()[0]["metadata"] == {"scanned": 3}
        call = journal.paper_calls()[0]
        assert call["mint"] == mint
        assert call["status"] == "rejected"
        assert call["score"] == 42


def test_discovery_candidate_upsert_keeps_latest_audit(tmp_path: Path) -> None:
    mint = str(Pubkey.new_unique())
    program = str(Pubkey.new_unique())
    with TradeJournal(tmp_path / "state.sqlite3") as journal:
        journal.upsert_discovery_candidate(
            mint=mint,
            source_program=program,
            audit_status="error",
            audit_summary="Temporary failure",
            top10_concentration_bps=None,
            decimals=None,
        )
        journal.upsert_discovery_candidate(
            mint=mint,
            source_program=program,
            audit_status="safe",
            audit_summary="No blocking findings",
            top10_concentration_bps=1_200,
            decimals=6,
            market={"symbol": "TEST", "liquidity_usd": "12500.25"},
        )
        candidates = journal.discovery_candidates()
        assert len(candidates) == 1
        assert candidates[0]["audit_status"] == "safe"
        assert candidates[0]["symbol"] == "TEST"
        assert candidates[0]["liquidity_usd"] == "12500.25"
        observations = journal.audit_observations(mint)
        assert [item["audit_status"] for item in observations] == ["error", "safe"]
        first_seen = observations[0]["observed_at_us"]
        second_seen = observations[1]["observed_at_us"]
        assert isinstance(first_seen, int) and isinstance(second_seen, int)
        assert first_seen <= second_seen
        assert observations[1]["top10_concentration_bps"] == 1_200
    with TradeJournal(tmp_path / "state.sqlite3") as journal:
        assert len(journal.audit_observations(mint)) == 2


def test_discovery_candidate_persists_hot_radar_evidence(tmp_path: Path) -> None:
    mint = str(Pubkey.new_unique())
    with TradeJournal(tmp_path / "state.sqlite3") as journal:
        journal.upsert_discovery_candidate(
            mint=mint,
            source_program="dexscreener_boost",
            audit_status="safe",
            audit_summary="No blocking findings",
            top10_concentration_bps=900,
            decimals=6,
            market={
                "symbol": "HOT",
                "liquidity_usd": "125000",
                "volume_5m_usd": "15000",
                "volume_1h_usd": "90000",
                "txns_5m_buys": 84,
                "txns_5m_sells": 36,
                "boosts_active": 12,
            },
            hot_signal={
                "score": 78,
                "state": "hot",
                "reasons": ["buyer flow accelerated"],
                "risk_flags": ["paid_boost_present"],
            },
        )

        candidate = journal.discovery_candidates()[0]

    activity = cast(dict[str, object], candidate["market_activity"])
    assert activity["volume_5m_usd"] == "15000"
    assert activity["txns_5m_buys"] == 84
    assert candidate["hot_score"] == 78
    assert candidate["hot_state"] == "hot"
    assert candidate["hot_reasons"] == ["buyer flow accelerated"]
    assert candidate["hot_risk_flags"] == ["paid_boost_present"]


def test_realized_paper_losses_include_allocated_cost_basis(tmp_path: Path) -> None:
    mint = str(Pubkey.new_unique())
    with TradeJournal(tmp_path / "state.sqlite3") as journal:
        journal.open_paper_position(
            position_id="loss-one",
            mint=mint,
            entry_lamports=100_000_000,
            token_amount=1_000,
            simulated_units=1,
            starting_lamports=1_000_000_000,
        )
        journal.partial_close_paper_position(
            "loss-one",
            sold_token_amount=200,
            exit_lamports=10_000_000,
            advance_tp_stage=False,
        )
        assert journal.paper_risk_metrics()["daily_realized_pnl_lamports"] == -10_000_000
        journal.close_paper_position("loss-one", 40_000_000)
        metrics = journal.paper_risk_metrics()
        assert metrics["daily_realized_pnl_lamports"] == -50_000_000
        assert metrics["consecutive_losses"] == 1
        assert journal.closed_paper_position_count() == 1
