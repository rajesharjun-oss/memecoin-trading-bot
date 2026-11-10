from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from solders.pubkey import Pubkey

from memecoin_bot.forward_performance import summarize_forward_paper
from memecoin_bot.journal import TradeJournal
from memecoin_bot.ohlcv import PoolCandle
from memecoin_bot.paper_campaign import (
    PaperCampaignError,
    archive_unmonitored_forward_paper_run,
    create_forward_paper_run,
    interrupt_forward_paper_run,
    quarantine_unmonitored_forward_paper_run_locked,
)
from memecoin_bot.worker_lock import exclusive_worker_lock


def test_startup_quarantine_requires_recent_monitor_and_position_quote(tmp_path: Path) -> None:
    source = tmp_path / "forward.sqlite3"
    now = datetime.now(UTC)
    mint = str(Pubkey.new_unique())
    with TradeJournal(source) as journal:
        journal.set_runtime_state(
            "paper_campaign",
            {"mode": "forward_paper_only", "status": "active", "started_at": now.isoformat()},
        )
        journal.open_paper_position(
            position_id="open-position",
            mint=mint,
            entry_lamports=100,
            token_amount=1_000,
            simulated_units=1_000,
            starting_lamports=1_000,
        )
        journal._connection.execute(
            "INSERT INTO worker_cycle_history "
            "(worker_key, started_at, completed_at, outcome, run_id) VALUES (?, ?, ?, ?, ?)",
            ("paper_exit_monitor", now.isoformat(), now.isoformat(), "completed", "worker"),
        )
        journal._connection.commit()

    with exclusive_worker_lock(source):
        assert (
            quarantine_unmonitored_forward_paper_run_locked(
                source, maximum_gap_seconds=300, now=now + timedelta(seconds=250)
            )
            is None
        )
    with TradeJournal(source) as journal:
        journal._connection.execute(
            "UPDATE paper_positions SET last_quoted_at = ? WHERE position_id = ?",
            ((now - timedelta(minutes=10)).isoformat(), "open-position"),
        )
        journal._connection.commit()
        fill_count = len(journal.paper_fills())
    with exclusive_worker_lock(source):
        interruption = quarantine_unmonitored_forward_paper_run_locked(
            source, maximum_gap_seconds=300, now=now + timedelta(seconds=250)
        )
        assert interruption is not None
        assert interruption["reason"] == "paper_position_quote_stale"
        assert (
            quarantine_unmonitored_forward_paper_run_locked(
                source, maximum_gap_seconds=300, now=now + timedelta(seconds=251)
            )
            == interruption
        )
    with TradeJournal(source, readonly=True) as journal:
        assert journal.runtime_state("paper_campaign")["status"] == "interrupted"
        assert journal.paper_position("open-position")["status"] == "open"
        assert len(journal.paper_fills()) == fill_count


def test_startup_quarantine_ignores_monitor_gap_without_open_positions(tmp_path: Path) -> None:
    source = tmp_path / "forward.sqlite3"
    now = datetime.now(UTC)
    with TradeJournal(source) as journal:
        journal.set_runtime_state(
            "paper_campaign",
            {"mode": "forward_paper_only", "status": "active", "started_at": now.isoformat()},
        )
    with exclusive_worker_lock(source):
        assert (
            quarantine_unmonitored_forward_paper_run_locked(
                source, maximum_gap_seconds=300, now=now + timedelta(hours=1)
            )
            is None
        )


def test_new_forward_run_keeps_research_provenance_and_isolates_positions(tmp_path: Path) -> None:
    source = tmp_path / "existing.sqlite3"
    target = tmp_path / "forward.sqlite3"
    mint = str(Pubkey.new_unique())
    pool = str(Pubkey.new_unique())
    # A prior schema added pair_address at the end, after the newer columns.
    with sqlite3.connect(source) as legacy:
        legacy.execute(
            """CREATE TABLE discovery_candidates (
                mint TEXT PRIMARY KEY, source_program TEXT NOT NULL,
                first_seen TEXT NOT NULL, last_seen TEXT NOT NULL,
                audit_status TEXT NOT NULL, audit_summary TEXT,
                top10_concentration_bps INTEGER, decimals INTEGER,
                name TEXT, symbol TEXT, dex_id TEXT, price_usd TEXT,
                liquidity_usd TEXT, volume_24h_usd TEXT,
                price_change_24h TEXT, pair_created_at INTEGER,
                market_activity_json TEXT NOT NULL DEFAULT '{}',
                hot_score INTEGER, hot_state TEXT,
                hot_reasons_json TEXT NOT NULL DEFAULT '[]',
                hot_risk_flags_json TEXT NOT NULL DEFAULT '[]'
            )"""
        )
    with TradeJournal(source) as existing:
        existing.upsert_discovery_candidate(
            mint=mint,
            source_program="test",
            audit_status="safe",
            audit_summary=None,
            top10_concentration_bps=100,
            decimals=6,
            market={"pair_address": pool, "price_usd": "1", "liquidity_usd": "100000"},
        )
        existing.record_market_snapshot(
            mint=mint,
            pool=pool,
            price_usd="1",
            liquidity_usd="100000",
            volume_24h_usd="50000",
            observed_at=120,
        )
        existing.record_pool_candles(
            [PoolCandle(mint, pool, "1m", 120, "1", "2", "1", "2", "100")],
            fetched_at=190,
        )
        existing.open_paper_position(
            position_id="prior-position",
            mint=mint,
            entry_lamports=100,
            token_amount=1_000,
            simulated_units=1_000,
            starting_lamports=1_000,
        )
        existing.set_runtime_state("paper_quote_health", {"unavailable": {"prior-position": {}}})

        result = create_forward_paper_run(source, target)
        assert result["research_rows_copied"]["pool_candles"] == 1
        assert len(existing.paper_positions()) == 1
        with TradeJournal(target) as fresh:
            assert fresh.paper_positions() == []
            assert fresh.paper_fills() == []
            assert fresh.strategy_decisions() == []
            assert fresh.runtime_state("paper_quote_health") is None
            assert fresh.discovery_candidate(mint) is not None
            assert fresh.market_snapshots(mint)[-1]["liquidity_usd"] == "100000"
            candles = fresh.pool_candles(mint, pool, "1m")
            assert candles[0]["first_fetched_at"] == 190
            assert candles[0]["first_observed_values_at"] == 190
            campaign = fresh.runtime_state("paper_campaign")
            assert campaign is not None
            assert campaign["mode"] == "forward_paper_only"
            assert campaign["status"] == "active"
            assert campaign["source_state_file"] == source.name
            assert campaign["run_id"] == result["run_id"]

    with pytest.raises(PaperCampaignError, match="already exists"):
        create_forward_paper_run(source, target)
    with pytest.raises(PaperCampaignError, match="does not exist"):
        create_forward_paper_run(tmp_path / "absent.sqlite3", tmp_path / "other.sqlite3")
    with pytest.raises(PaperCampaignError, match="different journal"):
        create_forward_paper_run(source, source)


def test_interrupted_forward_run_preserves_open_positions_and_excludes_them_from_new_run(
    tmp_path: Path,
) -> None:
    source = tmp_path / "interrupted.sqlite3"
    target = tmp_path / "new-forward.sqlite3"
    mint = str(Pubkey.new_unique())
    now = datetime.now(UTC).isoformat()
    with TradeJournal(source) as previous:
        previous.set_runtime_state(
            "paper_campaign",
            {"mode": "forward_paper_only", "run_id": "old-run", "started_at": now},
        )
        previous.open_paper_position(
            position_id="interrupted-position",
            mint=mint,
            entry_lamports=100,
            token_amount=1_000,
            simulated_units=1_000,
            starting_lamports=1_000,
        )
        previous._connection.execute(
            "INSERT INTO worker_cycle_history "
            "(worker_key, started_at, completed_at, outcome, run_id) "
            "VALUES (?, ?, ?, ?, ?)",
            ("paper_exit_monitor", now, now, "completed", "old-worker"),
        )
        original_fills = len(previous.paper_fills())

    with pytest.raises(PaperCampaignError, match="Interrupt open positions"):
        create_forward_paper_run(source, target)
    assert not target.exists()

    with (
        exclusive_worker_lock(source),
        pytest.raises(PaperCampaignError, match="Stop the paper dashboard"),
    ):
        interrupt_forward_paper_run(source)

    interruption = interrupt_forward_paper_run(source)
    assert interruption["gap_started_at"] == now
    assert interruption["interrupted_open_positions"] == [
        {"position_id": "interrupted-position", "mint": mint, "last_quoted_at": None}
    ]
    assert interrupt_forward_paper_run(source) == interruption
    with TradeJournal(source, readonly=True) as previous:
        assert previous.paper_position("interrupted-position")["status"] == "open"
        assert len(previous.paper_fills()) == original_fills
        assert [event["event_type"] for event in previous.automation_events()] == [
            "paper_position_interrupted",
            "paper_campaign_interrupted",
        ]
        summary = summarize_forward_paper(previous, 1_000)
        assert summary["campaign_status"] == "interrupted"
        assert summary["campaign_interruption"] == interruption

    create_forward_paper_run(source, target)
    with TradeJournal(target, readonly=True) as fresh:
        assert fresh.paper_positions() == []
        assert fresh.paper_fills() == []
        assert fresh.runtime_state("paper_campaign")["source_campaign_status"] == "interrupted"


def test_archive_closed_trade_with_unmonitored_mark_gap(tmp_path: Path) -> None:
    source = tmp_path / "historical-gap.sqlite3"
    mint = str(Pubkey.new_unique())
    with TradeJournal(source) as journal:
        journal.set_runtime_state(
            "paper_campaign",
            {"mode": "forward_paper_only", "run_id": "old-run", "status": "active"},
        )
        journal.open_paper_position(
            position_id="gap-position",
            mint=mint,
            entry_lamports=100,
            token_amount=1_000,
            simulated_units=1_000,
            starting_lamports=1_000,
        )
        journal.mark_paper_position("gap-position", 90)
        journal.close_paper_position("gap-position", 80, exit_reason="max_hold_time")
        journal._connection.execute(
            "UPDATE paper_positions SET opened_at = ?, closed_at = ? WHERE position_id = ?",
            ("2026-11-06T16:40:00+00:00", "2026-11-07T17:58:00+00:00", "gap-position"),
        )
        journal._connection.execute(
            "UPDATE paper_position_marks SET observed_at = ?, execution_json = ? "
            "WHERE position_id = ?",
            (
                "2026-11-06T16:45:00+00:00",
                '{"side":"mark","cash_credit_lamports":90,"network_fee_lamports":0,'
                '"priority_fee_lamports":0,"ata_rent_lamports":0,'
                '"modeled_slippage_lamports":0,"explicit_cost_lamports":0,"quote_age_ms":0}',
                "gap-position",
            ),
        )
        journal._connection.commit()

    with (
        exclusive_worker_lock(source),
        pytest.raises(PaperCampaignError, match="Stop the paper dashboard"),
    ):
        archive_unmonitored_forward_paper_run(source, maximum_mark_gap_seconds=360)

    interruption = archive_unmonitored_forward_paper_run(source, maximum_mark_gap_seconds=360)
    assert interruption["reason"] == "unmonitored_closed_position_path"
    assert interruption["affected_closed_positions"] == [
        {
            "position_id": "gap-position",
            "mint": mint,
            "gap_started_at": "2026-11-06T16:45:00+00:00",
            "gap_ended_at": "2026-11-07T17:58:00+00:00",
            "gap_seconds": 90_780,
        }
    ]
    assert (
        archive_unmonitored_forward_paper_run(source, maximum_mark_gap_seconds=360) == interruption
    )
    with TradeJournal(source, readonly=True) as journal:
        assert journal.paper_position("gap-position")["status"] == "closed"
        assert len(journal.paper_fills()) == 2
        assert journal.runtime_state("paper_campaign")["status"] == "interrupted"
