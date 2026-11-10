from __future__ import annotations

import json
import sqlite3
import time
import uuid
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

from solders.pubkey import Pubkey

from .constants import WSOL_MINT
from .models import OriginEvidence

if TYPE_CHECKING:
    from .ohlcv import PoolCandle
    from .strategy import StrategyDecision


class JournalError(RuntimeError):
    """Raised when an order conflicts with local idempotency or risk controls."""


_BASE_MARKET_KEYS = frozenset(
    {
        "mint",
        "name",
        "symbol",
        "dex_id",
        "pair_address",
        "price_usd",
        "liquidity_usd",
        "volume_24h_usd",
        "price_change_24h",
        "pair_created_at",
    }
)


def _market_activity_json(values: dict[str, object]) -> str:
    return json.dumps(
        {key: value for key, value in values.items() if key not in _BASE_MARKET_KEYS},
        ensure_ascii=True,
    )


def _nonnegative_int(value: object) -> int:
    return value if isinstance(value, int) and value >= 0 else 0


def _positive_int_or_none(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else None


class TradeJournal:
    def __init__(self, path: Path, *, readonly: bool = False) -> None:
        self.path = path.resolve()
        if readonly:
            self._connection = sqlite3.connect(
                f"{self.path.as_uri()}?mode=ro",
                uri=True,
                timeout=10,
                isolation_level=None,
            )
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        self._connection.execute("PRAGMA journal_mode=WAL")
        self._connection.execute("PRAGMA synchronous=FULL")
        self._connection.execute(
            """
            CREATE TABLE IF NOT EXISTS orders (
                client_order_id TEXT PRIMARY KEY,
                created_at TEXT NOT NULL,
                input_mint TEXT NOT NULL,
                output_mint TEXT NOT NULL,
                input_amount INTEGER NOT NULL CHECK (input_amount > 0),
                status TEXT NOT NULL,
                signature TEXT UNIQUE,
                error TEXT
            )
            """
        )
        self._connection.execute(
            """
            CREATE TABLE IF NOT EXISTS positions (
                position_id TEXT PRIMARY KEY,
                mint TEXT NOT NULL,
                wallet TEXT NOT NULL,
                entry_lamports INTEGER NOT NULL CHECK (entry_lamports > 0),
                initial_token_amount INTEGER NOT NULL CHECK (initial_token_amount > 0),
                take_profit_taken INTEGER NOT NULL DEFAULT 0 CHECK (take_profit_taken IN (0, 1)),
                status TEXT NOT NULL DEFAULT 'open',
                updated_at TEXT NOT NULL
            )
            """
        )
        self._connection.execute(
            """
            CREATE TABLE IF NOT EXISTS discovery_candidates (
                mint TEXT PRIMARY KEY,
                source_program TEXT NOT NULL,
                first_seen TEXT NOT NULL,
                last_seen TEXT NOT NULL,
                audit_status TEXT NOT NULL,
                audit_summary TEXT,
                top10_concentration_bps INTEGER,
                decimals INTEGER,
                name TEXT,
                symbol TEXT,
                dex_id TEXT,
                pair_address TEXT,
                price_usd TEXT,
                liquidity_usd TEXT,
                volume_24h_usd TEXT,
                price_change_24h TEXT,
                pair_created_at INTEGER,
                market_activity_json TEXT NOT NULL DEFAULT '{}',
                hot_score INTEGER CHECK (hot_score IS NULL OR hot_score BETWEEN 0 AND 100),
                hot_state TEXT,
                hot_reasons_json TEXT NOT NULL DEFAULT '[]',
                hot_risk_flags_json TEXT NOT NULL DEFAULT '[]'
            )
            """
        )
        self._connection.execute(
            """
            CREATE TABLE IF NOT EXISTS audit_observations (
                observation_id TEXT PRIMARY KEY,
                mint TEXT NOT NULL,
                observed_at_us INTEGER NOT NULL CHECK (observed_at_us >= 0),
                audited_at TEXT NOT NULL,
                audit_status TEXT NOT NULL CHECK (audit_status IN ('safe', 'blocked', 'error')),
                audit_summary TEXT,
                top10_concentration_bps INTEGER,
                decimals INTEGER,
                pair_address TEXT,
                source_program TEXT NOT NULL
            )
            """
        )
        self._connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_audit_observations_mint_time "
            "ON audit_observations(mint, observed_at_us)"
        )
        self._connection.execute(
            """
            CREATE TABLE IF NOT EXISTS token_origin_cache (
                mint TEXT PRIMARY KEY,
                checked_at_us INTEGER NOT NULL CHECK (checked_at_us >= 0),
                origin_json TEXT
            )
            """
        )
        self._connection.execute(
            """
            CREATE TABLE IF NOT EXISTS research_cohort_memberships (
                cohort_started_at INTEGER NOT NULL,
                mint TEXT NOT NULL,
                selected_at INTEGER NOT NULL,
                expires_at INTEGER NOT NULL,
                population_count INTEGER NOT NULL,
                selection_method TEXT NOT NULL,
                PRIMARY KEY (cohort_started_at, mint)
            )
            """
        )
        self._connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_research_membership_mint_time "
            "ON research_cohort_memberships(mint, selected_at)"
        )
        self._connection.execute(
            """
            CREATE TABLE IF NOT EXISTS decision_followup_memberships (
                selected_at INTEGER NOT NULL,
                mint TEXT NOT NULL,
                expires_at INTEGER NOT NULL,
                population_count INTEGER NOT NULL,
                selection_method TEXT NOT NULL,
                PRIMARY KEY (selected_at, mint)
            )
            """
        )
        self._connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_followup_membership_mint_time "
            "ON decision_followup_memberships(mint, selected_at)"
        )
        self._connection.execute(
            """
            CREATE TABLE IF NOT EXISTS paper_positions (
                position_id TEXT PRIMARY KEY,
                mint TEXT NOT NULL,
                entry_lamports INTEGER NOT NULL CHECK (entry_lamports > 0),
                token_amount INTEGER NOT NULL CHECK (token_amount > 0),
                simulated_units INTEGER NOT NULL CHECK (simulated_units >= 0),
                opened_at TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'closed')),
                closed_lamports INTEGER CHECK (closed_lamports IS NULL OR closed_lamports > 0),
                closed_at TEXT,
                last_value_lamports INTEGER,
                last_quoted_at TEXT,
                peak_value_lamports INTEGER,
                remaining_token_amount INTEGER NOT NULL CHECK (remaining_token_amount >= 0),
                realized_lamports INTEGER NOT NULL DEFAULT 0 CHECK (realized_lamports >= 0),
                realized_cost_lamports INTEGER NOT NULL DEFAULT 0
                    CHECK (realized_cost_lamports >= 0),
                tp_stage INTEGER NOT NULL DEFAULT 0 CHECK (tp_stage >= 0),
                last_exit_reason TEXT,
                entry_execution_json TEXT NOT NULL DEFAULT '{}',
                last_mark_execution_json TEXT NOT NULL DEFAULT '{}'
            )
            """
        )
        self._connection.execute(
            """
            CREATE TABLE IF NOT EXISTS paper_fills (
                fill_id TEXT PRIMARY KEY,
                position_id TEXT NOT NULL,
                side TEXT NOT NULL CHECK (side IN ('buy', 'sell')),
                input_amount INTEGER NOT NULL CHECK (input_amount > 0),
                output_amount INTEGER NOT NULL CHECK (output_amount > 0),
                cost_lamports INTEGER NOT NULL DEFAULT 0 CHECK (cost_lamports >= 0),
                exit_reason TEXT,
                created_at TEXT NOT NULL,
                execution_json TEXT NOT NULL DEFAULT '{}',
                FOREIGN KEY (position_id) REFERENCES paper_positions(position_id)
            )
            """
        )
        self._connection.execute(
            """
            CREATE TABLE IF NOT EXISTS paper_position_marks (
                mark_id TEXT PRIMARY KEY,
                position_id TEXT NOT NULL,
                observed_at TEXT NOT NULL,
                remaining_token_amount INTEGER NOT NULL CHECK (remaining_token_amount > 0),
                value_lamports INTEGER NOT NULL CHECK (value_lamports > 0),
                performance_value_lamports INTEGER NOT NULL
                    CHECK (performance_value_lamports > 0),
                peak_value_lamports INTEGER NOT NULL CHECK (peak_value_lamports > 0),
                tp_stage INTEGER NOT NULL CHECK (tp_stage >= 0),
                execution_json TEXT NOT NULL,
                FOREIGN KEY (position_id) REFERENCES paper_positions(position_id)
            )
            """
        )
        self._connection.execute(
            "CREATE INDEX IF NOT EXISTS paper_position_marks_position_time "
            "ON paper_position_marks(position_id, observed_at)"
        )
        self._connection.execute(
            """
            CREATE TABLE IF NOT EXISTS market_snapshots (
                mint TEXT NOT NULL,
                observed_at INTEGER NOT NULL,
                captured_at INTEGER,
                pool TEXT,
                price_usd TEXT NOT NULL,
                liquidity_usd TEXT NOT NULL,
                volume_24h_usd TEXT,
                price_change_24h TEXT,
                pair_created_at INTEGER,
                source TEXT NOT NULL DEFAULT 'legacy_unknown',
                PRIMARY KEY (mint, observed_at)
            )
            """
        )
        self._connection.execute(
            "CREATE INDEX IF NOT EXISTS market_snapshots_mint_time "
            "ON market_snapshots(mint, observed_at)"
        )
        self._connection.execute(
            """CREATE TABLE IF NOT EXISTS pool_candles (
                mint TEXT NOT NULL, pool TEXT NOT NULL, timeframe TEXT NOT NULL,
                opened_at INTEGER NOT NULL, open_usd TEXT NOT NULL,
                high_usd TEXT NOT NULL, low_usd TEXT NOT NULL, close_usd TEXT NOT NULL,
                volume_usd TEXT NOT NULL, fetched_at INTEGER NOT NULL,
                first_fetched_at INTEGER,
                first_observed_values_at INTEGER,
                revision_detected_at INTEGER,
                max_price_revision_bps REAL,
                max_volume_revision_usd REAL,
                PRIMARY KEY (mint, pool, timeframe, opened_at)
            )"""
        )
        self._connection.execute(
            "CREATE INDEX IF NOT EXISTS pool_candles_lookup "
            "ON pool_candles(mint, pool, timeframe, opened_at DESC)"
        )
        self._connection.execute(
            """
            CREATE TABLE IF NOT EXISTS strategy_backtests (
                run_id TEXT PRIMARY KEY,
                created_at TEXT NOT NULL,
                mint TEXT NOT NULL,
                timeframe TEXT NOT NULL,
                strategy_name TEXT NOT NULL,
                result_json TEXT NOT NULL
            )
            """
        )
        self._connection.execute(
            """
            CREATE TABLE IF NOT EXISTS strategy_decisions (
                decision_id TEXT PRIMARY KEY,
                created_at TEXT NOT NULL,
                mint TEXT NOT NULL,
                score INTEGER NOT NULL CHECK (score BETWEEN 0 AND 100),
                verdict TEXT NOT NULL CHECK (verdict IN ('reject', 'observe', 'eligible')),
                reasons_json TEXT NOT NULL,
                evidence_json TEXT NOT NULL DEFAULT '{}',
                action TEXT NOT NULL,
                paper_position_id TEXT
            )
            """
        )
        self._connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_strategy_decisions_mint_time "
            "ON strategy_decisions(mint, created_at)"
        )
        self._connection.execute(
            """
            CREATE TABLE IF NOT EXISTS collector_signal_observations (
                observation_id TEXT PRIMARY KEY,
                created_at TEXT NOT NULL,
                mint TEXT NOT NULL,
                score INTEGER NOT NULL CHECK (score BETWEEN 0 AND 100),
                verdict TEXT NOT NULL CHECK (verdict IN ('reject', 'observe', 'eligible')),
                reasons_json TEXT NOT NULL,
                evidence_json TEXT NOT NULL
            )
            """
        )
        self._connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_collector_signal_time "
            "ON collector_signal_observations(created_at DESC)"
        )
        self._connection.execute(
            """
            CREATE TABLE IF NOT EXISTS paper_automation_events (
                event_id TEXT PRIMARY KEY,
                created_at TEXT NOT NULL,
                event_type TEXT NOT NULL,
                level TEXT NOT NULL CHECK (level IN ('info', 'warning', 'error')),
                message TEXT NOT NULL,
                metadata_json TEXT NOT NULL
            )
            """
        )
        self._connection.execute(
            """
            CREATE TABLE IF NOT EXISTS paper_calls (
                call_id TEXT PRIMARY KEY,
                created_at TEXT NOT NULL,
                mint TEXT NOT NULL,
                source_label TEXT,
                status TEXT NOT NULL CHECK (
                    status IN ('processing', 'rejected', 'observed', 'opened', 'error')
                ),
                score INTEGER CHECK (score IS NULL OR score BETWEEN 0 AND 100),
                verdict TEXT,
                reason TEXT,
                paper_position_id TEXT
            )
            """
        )
        self._connection.execute(
            """
            CREATE TABLE IF NOT EXISTS narrative_items (
                item_id TEXT PRIMARY KEY,
                observed_at TEXT NOT NULL,
                published_at TEXT NOT NULL,
                source TEXT NOT NULL,
                source_kind TEXT NOT NULL CHECK (source_kind IN ('news', 'social')),
                source_name TEXT NOT NULL,
                source_url TEXT,
                title TEXT NOT NULL,
                excerpt TEXT NOT NULL,
                engagement INTEGER NOT NULL CHECK (engagement >= 0),
                evidence_tier TEXT NOT NULL,
                stance TEXT NOT NULL,
                tags_json TEXT NOT NULL,
                risk_flags_json TEXT NOT NULL,
                matched_mints_json TEXT NOT NULL,
                explicit_mints_json TEXT NOT NULL
            )
            """
        )
        self._connection.execute(
            """
            CREATE INDEX IF NOT EXISTS narrative_items_published_at
            ON narrative_items(published_at DESC)
            """
        )
        self._connection.execute(
            """
            CREATE TABLE IF NOT EXISTS narrative_model_assessments (
                item_id TEXT PRIMARY KEY REFERENCES narrative_items(item_id),
                requested_at TEXT NOT NULL,
                completed_at TEXT,
                model TEXT NOT NULL,
                prompt_version TEXT NOT NULL,
                status TEXT NOT NULL CHECK (status IN ('pending', 'complete', 'failed')),
                result_json TEXT,
                input_tokens INTEGER,
                output_tokens INTEGER,
                error_code TEXT
            )
            """
        )
        self._connection.execute(
            "CREATE INDEX IF NOT EXISTS narrative_model_assessments_requested "
            "ON narrative_model_assessments(requested_at DESC)"
        )
        self._connection.execute(
            """
            CREATE TABLE IF NOT EXISTS narrative_signals (
                mint TEXT PRIMARY KEY,
                updated_at TEXT NOT NULL,
                latest_mention_at TEXT,
                score INTEGER NOT NULL CHECK (score BETWEEN 0 AND 100),
                state TEXT NOT NULL,
                confidence TEXT NOT NULL,
                mention_count INTEGER NOT NULL,
                unique_sources INTEGER NOT NULL,
                news_count INTEGER NOT NULL,
                social_count INTEGER NOT NULL,
                engagement INTEGER NOT NULL,
                explicit_contract_mentions INTEGER NOT NULL,
                tags_json TEXT NOT NULL,
                reasons_json TEXT NOT NULL,
                risk_flags_json TEXT NOT NULL
            )
            """
        )
        narrative_columns = {
            str(row[1]) for row in self._connection.execute("PRAGMA table_info(narrative_signals)")
        }
        if "latest_mention_at" not in narrative_columns:
            self._connection.execute(
                "ALTER TABLE narrative_signals ADD COLUMN latest_mention_at TEXT"
            )
        self._connection.execute(
            """
            CREATE TABLE IF NOT EXISTS narrative_signal_observations (
                refresh_id INTEGER PRIMARY KEY AUTOINCREMENT,
                captured_at_us INTEGER NOT NULL CHECK (captured_at_us >= 0),
                signals_json TEXT NOT NULL
            )
            """
        )
        self._connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_narrative_signal_observations_time "
            "ON narrative_signal_observations(captured_at_us, refresh_id)"
        )
        self._connection.execute(
            """
            CREATE TABLE IF NOT EXISTS runtime_state (
                state_key TEXT PRIMARY KEY,
                updated_at TEXT NOT NULL,
                payload_json TEXT NOT NULL
            )
            """
        )
        self._connection.execute(
            """
            CREATE TABLE IF NOT EXISTS worker_cycle_history (
                worker_key TEXT NOT NULL,
                started_at TEXT NOT NULL,
                completed_at TEXT NOT NULL,
                outcome TEXT NOT NULL CHECK (outcome IN ('completed', 'paused', 'error')),
                run_id TEXT,
                rpc_requests_json TEXT,
                PRIMARY KEY (worker_key, started_at, completed_at)
            )
            """
        )
        self._connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_worker_cycle_history_completed "
            "ON worker_cycle_history(worker_key, completed_at)"
        )
        worker_cycle_columns = {
            str(row[1])
            for row in self._connection.execute("PRAGMA table_info(worker_cycle_history)")
        }
        if "run_id" not in worker_cycle_columns:
            self._connection.execute("ALTER TABLE worker_cycle_history ADD COLUMN run_id TEXT")
        if "rpc_requests_json" not in worker_cycle_columns:
            self._connection.execute(
                "ALTER TABLE worker_cycle_history ADD COLUMN rpc_requests_json TEXT"
            )
        paper_columns = {
            str(row[1]) for row in self._connection.execute("PRAGMA table_info(paper_positions)")
        }
        candidate_columns = {
            str(row[1])
            for row in self._connection.execute("PRAGMA table_info(discovery_candidates)")
        }
        snapshot_columns = {
            str(row[1]) for row in self._connection.execute("PRAGMA table_info(market_snapshots)")
        }
        if "pool" not in snapshot_columns:
            self._connection.execute("ALTER TABLE market_snapshots ADD COLUMN pool TEXT")
        if "captured_at" not in snapshot_columns:
            self._connection.execute("ALTER TABLE market_snapshots ADD COLUMN captured_at INTEGER")
        if "price_change_24h" not in snapshot_columns:
            self._connection.execute(
                "ALTER TABLE market_snapshots ADD COLUMN price_change_24h TEXT"
            )
        if "pair_created_at" not in snapshot_columns:
            self._connection.execute(
                "ALTER TABLE market_snapshots ADD COLUMN pair_created_at INTEGER"
            )
        if "source" not in snapshot_columns:
            self._connection.execute(
                "ALTER TABLE market_snapshots ADD COLUMN source TEXT NOT NULL "
                "DEFAULT 'legacy_unknown'"
            )
        candle_columns = {
            str(row[1]) for row in self._connection.execute("PRAGMA table_info(pool_candles)")
        }
        if "first_fetched_at" not in candle_columns:
            # Old fetched_at values may have been overwritten by later refreshes.
            # Keep historical first-fetch provenance unknown rather than inventing it.
            self._connection.execute("ALTER TABLE pool_candles ADD COLUMN first_fetched_at INTEGER")
        if "first_observed_values_at" not in candle_columns:
            # Existing prices may have been revised after their first fetch.
            # Preserve them, but do not certify their original value provenance.
            self._connection.execute(
                "ALTER TABLE pool_candles ADD COLUMN first_observed_values_at INTEGER"
            )
        if "revision_detected_at" not in candle_columns:
            # Earlier refreshes were not compared, so legacy revisions are unknowable.
            self._connection.execute(
                "ALTER TABLE pool_candles ADD COLUMN revision_detected_at INTEGER"
            )
        if "max_price_revision_bps" not in candle_columns:
            self._connection.execute(
                "ALTER TABLE pool_candles ADD COLUMN max_price_revision_bps REAL"
            )
        if "max_volume_revision_usd" not in candle_columns:
            self._connection.execute(
                "ALTER TABLE pool_candles ADD COLUMN max_volume_revision_usd REAL"
            )
        decision_columns = {
            str(row[1]) for row in self._connection.execute("PRAGMA table_info(strategy_decisions)")
        }
        if "evidence_json" not in decision_columns:
            self._connection.execute(
                "ALTER TABLE strategy_decisions ADD COLUMN evidence_json TEXT NOT NULL DEFAULT '{}'"
            )
        if "last_exit_reason" not in paper_columns:
            self._connection.execute("ALTER TABLE paper_positions ADD COLUMN last_exit_reason TEXT")
        fill_columns = {
            str(row[1]) for row in self._connection.execute("PRAGMA table_info(paper_fills)")
        }
        if "exit_reason" not in fill_columns:
            self._connection.execute("ALTER TABLE paper_fills ADD COLUMN exit_reason TEXT")
        if "pair_address" not in candidate_columns:
            self._connection.execute(
                "ALTER TABLE discovery_candidates ADD COLUMN pair_address TEXT"
            )
        if "peak_value_lamports" not in paper_columns:
            self._connection.execute(
                "ALTER TABLE paper_positions ADD COLUMN peak_value_lamports INTEGER"
            )
        if "remaining_token_amount" not in paper_columns:
            self._connection.execute(
                "ALTER TABLE paper_positions ADD COLUMN remaining_token_amount INTEGER"
            )
            self._connection.execute(
                "UPDATE paper_positions SET remaining_token_amount = "
                "CASE WHEN status = 'closed' THEN 0 ELSE token_amount END"
            )
        if "realized_lamports" not in paper_columns:
            self._connection.execute(
                "ALTER TABLE paper_positions ADD COLUMN realized_lamports "
                "INTEGER NOT NULL DEFAULT 0"
            )
            self._connection.execute(
                "UPDATE paper_positions SET realized_lamports = COALESCE(closed_lamports, 0) "
                "WHERE status = 'closed'"
            )
        if "realized_cost_lamports" not in paper_columns:
            self._connection.execute(
                "ALTER TABLE paper_positions ADD COLUMN realized_cost_lamports "
                "INTEGER NOT NULL DEFAULT 0"
            )
            self._connection.execute(
                "UPDATE paper_positions SET realized_cost_lamports = entry_lamports "
                "WHERE status = 'closed'"
            )
        if "tp_stage" not in paper_columns:
            self._connection.execute(
                "ALTER TABLE paper_positions ADD COLUMN tp_stage INTEGER NOT NULL DEFAULT 0"
            )
        if "entry_execution_json" not in paper_columns:
            self._connection.execute(
                "ALTER TABLE paper_positions ADD COLUMN entry_execution_json "
                "TEXT NOT NULL DEFAULT '{}'"
            )
        if "last_mark_execution_json" not in paper_columns:
            self._connection.execute(
                "ALTER TABLE paper_positions ADD COLUMN last_mark_execution_json "
                "TEXT NOT NULL DEFAULT '{}'"
            )
        fill_columns = {
            str(row[1]) for row in self._connection.execute("PRAGMA table_info(paper_fills)")
        }
        if "cost_lamports" not in fill_columns:
            self._connection.execute(
                "ALTER TABLE paper_fills ADD COLUMN cost_lamports INTEGER NOT NULL DEFAULT 0"
            )
            self._connection.execute(
                "UPDATE paper_fills SET cost_lamports = input_amount WHERE side = 'buy'"
            )
            self._connection.execute(
                """
                UPDATE paper_fills
                SET cost_lamports = COALESCE((
                    SELECT entry_lamports * paper_fills.input_amount / token_amount
                    FROM paper_positions
                    WHERE paper_positions.position_id = paper_fills.position_id
                ), 0)
                WHERE side = 'sell'
                """
            )
        if "execution_json" not in fill_columns:
            self._connection.execute(
                "ALTER TABLE paper_fills ADD COLUMN execution_json TEXT NOT NULL DEFAULT '{}'"
            )
        discovery_columns = {
            str(row[1])
            for row in self._connection.execute("PRAGMA table_info(discovery_candidates)")
        }
        discovery_migrations = {
            "market_activity_json": "TEXT NOT NULL DEFAULT '{}'",
            "hot_score": "INTEGER",
            "hot_state": "TEXT",
            "hot_reasons_json": "TEXT NOT NULL DEFAULT '[]'",
            "hot_risk_flags_json": "TEXT NOT NULL DEFAULT '[]'",
        }
        for column, definition in discovery_migrations.items():
            if column not in discovery_columns:
                self._connection.execute(
                    f"ALTER TABLE discovery_candidates ADD COLUMN {column} {definition}"
                )

    def close(self) -> None:
        self._connection.close()

    def __enter__(self) -> TradeJournal:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def reserve(
        self,
        *,
        client_order_id: str,
        input_mint: str,
        output_mint: str,
        input_amount: int,
        max_daily_lamports: int,
    ) -> None:
        if not client_order_id or len(client_order_id) > 100:
            raise JournalError("client_order_id must contain 1 to 100 characters")
        now = datetime.now(UTC)
        day_prefix = now.date().isoformat() + "%"
        try:
            self._connection.execute("BEGIN IMMEDIATE")
            if input_mint == WSOL_MINT:
                used = self._connection.execute(
                    """
                    SELECT COALESCE(SUM(input_amount), 0)
                    FROM orders
                    WHERE created_at LIKE ? AND input_mint = ?
                      AND status IN ('reserved', 'submitted', 'confirmed')
                    """,
                    (day_prefix, WSOL_MINT),
                ).fetchone()[0]
                if int(used) + input_amount > max_daily_lamports:
                    raise JournalError("Daily SOL exposure limit would be exceeded")
            self._connection.execute(
                """
                INSERT INTO orders
                (client_order_id, created_at, input_mint, output_mint, input_amount, status)
                VALUES (?, ?, ?, ?, ?, 'reserved')
                """,
                (client_order_id, now.isoformat(), input_mint, output_mint, input_amount),
            )
            self._connection.execute("COMMIT")
        except sqlite3.IntegrityError as exc:
            self._connection.execute("ROLLBACK")
            raise JournalError("client_order_id has already been used") from exc
        except Exception:
            self._connection.execute("ROLLBACK")
            raise

    def update(
        self,
        client_order_id: str,
        status: str,
        *,
        signature: str | None = None,
        error: str | None = None,
    ) -> None:
        if status not in {"failed", "submitted", "confirmed"}:
            raise JournalError("Invalid journal status")
        self._connection.execute(
            "UPDATE orders SET status = ?, signature = COALESCE(?, signature), error = ? "
            "WHERE client_order_id = ?",
            (status, signature, error[:500] if error else None, client_order_id),
        )

    def ensure_position(
        self,
        *,
        position_id: str,
        mint: str,
        wallet: str,
        entry_lamports: int,
        initial_token_amount: int,
    ) -> dict[str, object]:
        if not position_id or len(position_id) > 100:
            raise JournalError("position_id must contain 1 to 100 characters")
        now = datetime.now(UTC).isoformat()
        self._connection.execute(
            """
            INSERT OR IGNORE INTO positions
            (position_id, mint, wallet, entry_lamports, initial_token_amount, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (position_id, mint, wallet, entry_lamports, initial_token_amount, now),
        )
        row = self._connection.execute(
            """
            SELECT mint, wallet, entry_lamports, initial_token_amount, take_profit_taken, status
            FROM positions WHERE position_id = ?
            """,
            (position_id,),
        ).fetchone()
        if row is None:
            raise JournalError("Position state could not be loaded")
        expected = (mint, wallet, entry_lamports, initial_token_amount)
        if tuple(row[:4]) != expected:
            raise JournalError("position_id already refers to different position parameters")
        return {
            "mint": row[0],
            "wallet": row[1],
            "entry_lamports": int(row[2]),
            "initial_token_amount": int(row[3]),
            "take_profit_taken": bool(row[4]),
            "status": row[5],
        }

    def update_position(self, position_id: str, *, status: str, take_profit_taken: bool) -> None:
        if status not in {"open", "partial", "closed"}:
            raise JournalError("Invalid position status")
        cursor = self._connection.execute(
            """
            UPDATE positions SET status = ?, take_profit_taken = ?, updated_at = ?
            WHERE position_id = ?
            """,
            (status, int(take_profit_taken), datetime.now(UTC).isoformat(), position_id),
        )
        if cursor.rowcount != 1:
            raise JournalError("Position state was not found")

    def recent_orders(self, *, limit: int = 50) -> list[dict[str, object]]:
        if not 1 <= limit <= 100:
            raise JournalError("Order history limit must be between 1 and 100")
        rows = self._connection.execute(
            """
            SELECT client_order_id, created_at, input_mint, output_mint,
                   input_amount, status, signature, error
            FROM orders ORDER BY created_at DESC LIMIT ?
            """,
            (limit,),
        ).fetchall()
        return [
            {
                "client_order_id": row[0],
                "created_at": row[1],
                "input_mint": row[2],
                "output_mint": row[3],
                "input_amount": int(row[4]),
                "status": row[5],
                "signature": row[6],
                "error": row[7],
            }
            for row in rows
        ]

    def positions(self, *, limit: int = 50) -> list[dict[str, object]]:
        if not 1 <= limit <= 100:
            raise JournalError("Position limit must be between 1 and 100")
        rows = self._connection.execute(
            """
            SELECT position_id, mint, wallet, entry_lamports, initial_token_amount,
                   take_profit_taken, status, updated_at
            FROM positions ORDER BY updated_at DESC LIMIT ?
            """,
            (limit,),
        ).fetchall()
        return [
            {
                "position_id": row[0],
                "mint": row[1],
                "wallet": row[2],
                "entry_lamports": int(row[3]),
                "initial_token_amount": int(row[4]),
                "take_profit_taken": bool(row[5]),
                "status": row[6],
                "updated_at": row[7],
            }
            for row in rows
        ]

    def daily_exposure_lamports(self) -> int:
        day_prefix = datetime.now(UTC).date().isoformat() + "%"
        row = self._connection.execute(
            """
            SELECT COALESCE(SUM(input_amount), 0) FROM orders
            WHERE created_at LIKE ? AND input_mint = ?
              AND status IN ('reserved', 'submitted', 'confirmed')
            """,
            (day_prefix, WSOL_MINT),
        ).fetchone()
        return int(row[0]) if row else 0

    def upsert_discovery_candidate(
        self,
        *,
        mint: str,
        source_program: str,
        audit_status: str,
        audit_summary: str | None,
        top10_concentration_bps: int | None,
        decimals: int | None,
        market: dict[str, object] | None = None,
        hot_signal: dict[str, object] | None = None,
    ) -> None:
        if audit_status not in {"safe", "blocked", "error"}:
            raise JournalError("Invalid discovery audit status")
        observed = datetime.now(UTC)
        now = observed.isoformat()
        values = market or {}
        self._connection.execute(
            """
            INSERT INTO discovery_candidates
            (mint, source_program, first_seen, last_seen, audit_status, audit_summary,
             top10_concentration_bps, decimals, name, symbol, dex_id, pair_address, price_usd,
             liquidity_usd, volume_24h_usd, price_change_24h, pair_created_at,
             market_activity_json, hot_score, hot_state, hot_reasons_json,
             hot_risk_flags_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(mint) DO UPDATE SET
                source_program = excluded.source_program,
                last_seen = excluded.last_seen,
                audit_status = excluded.audit_status,
                audit_summary = excluded.audit_summary,
                top10_concentration_bps = excluded.top10_concentration_bps,
                decimals = excluded.decimals,
                name = COALESCE(excluded.name, discovery_candidates.name),
                symbol = COALESCE(excluded.symbol, discovery_candidates.symbol),
                dex_id = COALESCE(excluded.dex_id, discovery_candidates.dex_id),
                pair_address = COALESCE(excluded.pair_address, discovery_candidates.pair_address),
                price_usd = COALESCE(excluded.price_usd, discovery_candidates.price_usd),
                liquidity_usd = COALESCE(
                    excluded.liquidity_usd, discovery_candidates.liquidity_usd
                ),
                volume_24h_usd = COALESCE(
                    excluded.volume_24h_usd, discovery_candidates.volume_24h_usd
                ),
                price_change_24h = COALESCE(
                    excluded.price_change_24h, discovery_candidates.price_change_24h
                ),
                pair_created_at = COALESCE(
                    excluded.pair_created_at, discovery_candidates.pair_created_at
                ),
                market_activity_json = excluded.market_activity_json,
                hot_score = excluded.hot_score,
                hot_state = excluded.hot_state,
                hot_reasons_json = excluded.hot_reasons_json,
                hot_risk_flags_json = excluded.hot_risk_flags_json
            """,
            (
                mint,
                source_program,
                now,
                now,
                audit_status,
                audit_summary[:500] if audit_summary else None,
                top10_concentration_bps,
                decimals,
                values.get("name"),
                values.get("symbol"),
                values.get("dex_id"),
                values.get("pair_address"),
                values.get("price_usd"),
                values.get("liquidity_usd"),
                values.get("volume_24h_usd"),
                values.get("price_change_24h"),
                values.get("pair_created_at"),
                _market_activity_json(values),
                (hot_signal or {}).get("score"),
                (hot_signal or {}).get("state"),
                json.dumps((hot_signal or {}).get("reasons", []), ensure_ascii=True),
                json.dumps((hot_signal or {}).get("risk_flags", []), ensure_ascii=True),
            ),
        )
        self._connection.execute(
            """INSERT INTO audit_observations
            (observation_id, mint, observed_at_us, audited_at, audit_status,
             audit_summary, top10_concentration_bps, decimals, pair_address, source_program)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                str(uuid.uuid4()),
                mint,
                int(observed.timestamp() * 1_000_000),
                now,
                audit_status,
                audit_summary[:500] if audit_summary else None,
                top10_concentration_bps,
                decimals,
                str(values["pair_address"]) if values.get("pair_address") else None,
                source_program,
            ),
        )
        price = values.get("price_usd")
        liquidity = values.get("liquidity_usd")
        if price not in (None, "") and liquidity not in (None, ""):
            self.record_market_snapshot(
                mint=mint,
                pool=str(values["pair_address"]) if values.get("pair_address") else None,
                price_usd=str(price),
                liquidity_usd=str(liquidity),
                volume_24h_usd=(
                    str(values["volume_24h_usd"])
                    if values.get("volume_24h_usd") not in (None, "")
                    else None
                ),
                price_change_24h=(
                    str(values["price_change_24h"])
                    if values.get("price_change_24h") not in (None, "")
                    else None
                ),
                pair_created_at=_positive_int_or_none(values.get("pair_created_at")),
            )

    def audit_observations(self, mint: str, *, limit: int = 10_000) -> list[dict[str, object]]:
        """Return recorded audit outcomes oldest first; never infer older states."""
        if not 1 <= limit <= 10_000:
            raise JournalError("Audit observation limit must be between 1 and 10000")
        rows = self._connection.execute(
            """SELECT observed_at_us, audited_at, audit_status, audit_summary,
                      top10_concentration_bps, decimals, pair_address, source_program
               FROM audit_observations WHERE mint=?
               ORDER BY observed_at_us DESC LIMIT ?""",
            (mint, limit),
        ).fetchall()
        keys = (
            "observed_at_us",
            "audited_at",
            "audit_status",
            "audit_summary",
            "top10_concentration_bps",
            "decimals",
            "pair_address",
            "source_program",
        )
        return [dict(zip(keys, row, strict=True)) for row in reversed(rows)]

    def update_candidate_market(
        self,
        *,
        mint: str,
        market: dict[str, object],
        hot_signal: dict[str, object],
    ) -> bool:
        cursor = self._connection.execute(
            """
            UPDATE discovery_candidates
            SET name = COALESCE(?, name), symbol = COALESCE(?, symbol),
                dex_id = COALESCE(?, dex_id), pair_address = COALESCE(?, pair_address),
                price_usd = COALESCE(?, price_usd),
                liquidity_usd = COALESCE(?, liquidity_usd),
                volume_24h_usd = COALESCE(?, volume_24h_usd),
                price_change_24h = COALESCE(?, price_change_24h),
                pair_created_at = COALESCE(?, pair_created_at),
                market_activity_json = ?, hot_score = ?, hot_state = ?,
                hot_reasons_json = ?, hot_risk_flags_json = ?
            WHERE mint = ?
            """,
            (
                market.get("name"),
                market.get("symbol"),
                market.get("dex_id"),
                market.get("pair_address"),
                market.get("price_usd"),
                market.get("liquidity_usd"),
                market.get("volume_24h_usd"),
                market.get("price_change_24h"),
                market.get("pair_created_at"),
                _market_activity_json(market),
                hot_signal.get("score"),
                hot_signal.get("state"),
                json.dumps(hot_signal.get("reasons", []), ensure_ascii=True),
                json.dumps(hot_signal.get("risk_flags", []), ensure_ascii=True),
                mint,
            ),
        )
        price = market.get("price_usd")
        liquidity = market.get("liquidity_usd")
        if cursor.rowcount and price not in (None, "") and liquidity not in (None, ""):
            self.record_market_snapshot(
                mint=mint,
                pool=str(market["pair_address"]) if market.get("pair_address") else None,
                price_usd=str(price),
                liquidity_usd=str(liquidity),
                volume_24h_usd=(
                    str(market["volume_24h_usd"])
                    if market.get("volume_24h_usd") not in (None, "")
                    else None
                ),
                price_change_24h=(
                    str(market["price_change_24h"])
                    if market.get("price_change_24h") not in (None, "")
                    else None
                ),
                pair_created_at=_positive_int_or_none(market.get("pair_created_at")),
                source="dexscreener",
            )
        return cursor.rowcount == 1

    def record_market_snapshot(
        self,
        *,
        mint: str,
        pool: str | None = None,
        price_usd: str,
        liquidity_usd: str,
        volume_24h_usd: str | None,
        price_change_24h: str | None = None,
        pair_created_at: int | None = None,
        observed_at: int | None = None,
        source: str = "legacy_unknown",
    ) -> None:
        timestamp = observed_at if observed_at is not None else int(datetime.now(UTC).timestamp())
        if type(timestamp) is not int or timestamp < 0:
            raise JournalError("Market snapshot time is invalid")
        if source not in {"legacy_unknown", "dexscreener", "geckoterminal_pool"}:
            raise JournalError("Market snapshot source is invalid")
        # Retain distinct second-level observations. Minute-bucket upserts lost
        # the earlier market gate input when a later poll arrived in that minute.
        # Same-second duplicates keep the first value for point-in-time replay.
        self._connection.execute(
            """
            INSERT INTO market_snapshots
            (mint, observed_at, captured_at, pool, price_usd, liquidity_usd, volume_24h_usd,
             price_change_24h, pair_created_at, source)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(mint, observed_at) DO NOTHING
            """,
            (
                mint,
                timestamp,
                timestamp,
                pool,
                price_usd,
                liquidity_usd,
                volume_24h_usd,
                price_change_24h,
                pair_created_at,
                source,
            ),
        )

    def market_snapshots(self, mint: str, *, limit: int = 10_000) -> list[dict[str, object]]:
        if not 1 <= limit <= 10_000:
            raise JournalError("Market snapshot limit must be between 1 and 10000")
        has_source = any(
            row[1] == "source"
            for row in self._connection.execute("PRAGMA table_info(market_snapshots)")
        )
        query_with_source = """
            SELECT observed_at, captured_at, pool, price_usd, liquidity_usd,
                   volume_24h_usd, price_change_24h, pair_created_at, source
            FROM market_snapshots WHERE mint = ?
            ORDER BY observed_at DESC LIMIT ?
            """
        query_without_source = """
            SELECT observed_at, captured_at, pool, price_usd, liquidity_usd,
                   volume_24h_usd, price_change_24h, pair_created_at,
                   'legacy_unknown' AS source
            FROM market_snapshots WHERE mint = ?
            ORDER BY observed_at DESC LIMIT ?
            """
        rows = self._connection.execute(
            query_with_source if has_source else query_without_source,
            (mint, limit),
        ).fetchall()
        return [
            {
                "observed_at": int(row[0]),
                "captured_at": int(row[1]) if row[1] is not None else None,
                "pool": row[2],
                "price_usd": row[3],
                "liquidity_usd": row[4],
                "volume_24h_usd": row[5],
                "price_change_24h": row[6],
                "pair_created_at": row[7],
                "source": row[8],
            }
            for row in reversed(rows)
        ]

    def market_snapshot_before(self, mint: str, *, captured_at: int) -> dict[str, object] | None:
        """Return a pool snapshot captured no later than an event's actual time."""
        row = self._connection.execute(
            """SELECT observed_at, captured_at, pool, price_usd, liquidity_usd,
                       volume_24h_usd, price_change_24h, pair_created_at
               FROM market_snapshots
               WHERE mint = ? AND captured_at <= ?
               ORDER BY captured_at DESC, observed_at DESC LIMIT 1""",
            (mint, captured_at),
        ).fetchone()
        if row is None:
            return None
        return {
            "observed_at": int(row[0]),
            "captured_at": int(row[1]),
            "pool": row[2],
            "price_usd": row[3],
            "liquidity_usd": row[4],
            "volume_24h_usd": row[5],
            "price_change_24h": row[6],
            "pair_created_at": row[7],
        }

    def record_pool_candles(self, candles: Sequence[PoolCandle], *, fetched_at: int) -> int:
        if not candles:
            return 0
        owns_transaction = not self._connection.in_transaction
        if owns_transaction:
            self._connection.execute("BEGIN IMMEDIATE")
        try:
            before = int(
                self._connection.execute("SELECT COUNT(*) FROM pool_candles").fetchone()[0]
            )
            self._connection.executemany(
                """INSERT INTO pool_candles
                (mint, pool, timeframe, opened_at, open_usd, high_usd, low_usd,
                 close_usd, volume_usd, fetched_at, first_fetched_at,
                 first_observed_values_at, max_price_revision_bps,
                 max_volume_revision_usd)
                 VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(mint, pool, timeframe, opened_at) DO UPDATE SET
                     max_price_revision_bps=MAX(
                         COALESCE(pool_candles.max_price_revision_bps, 0),
                         10000 * MAX(
                             COALESCE(ABS(CAST(excluded.open_usd AS REAL) -
                                          CAST(pool_candles.open_usd AS REAL)) /
                                      NULLIF(ABS(CAST(pool_candles.open_usd AS REAL)), 0), 0),
                             COALESCE(ABS(CAST(excluded.high_usd AS REAL) -
                                          CAST(pool_candles.high_usd AS REAL)) /
                                      NULLIF(ABS(CAST(pool_candles.high_usd AS REAL)), 0), 0),
                             COALESCE(ABS(CAST(excluded.low_usd AS REAL) -
                                          CAST(pool_candles.low_usd AS REAL)) /
                                      NULLIF(ABS(CAST(pool_candles.low_usd AS REAL)), 0), 0),
                             COALESCE(ABS(CAST(excluded.close_usd AS REAL) -
                                          CAST(pool_candles.close_usd AS REAL)) /
                                      NULLIF(ABS(CAST(pool_candles.close_usd AS REAL)), 0), 0)
                         )
                     ),
                     max_volume_revision_usd=MAX(
                         COALESCE(pool_candles.max_volume_revision_usd, 0),
                         ABS(CAST(excluded.volume_usd AS REAL) -
                             CAST(pool_candles.volume_usd AS REAL))
                     ),
                     revision_detected_at=CASE WHEN
                         CAST(pool_candles.open_usd AS REAL) != CAST(excluded.open_usd AS REAL) OR
                         CAST(pool_candles.high_usd AS REAL) != CAST(excluded.high_usd AS REAL) OR
                         CAST(pool_candles.low_usd AS REAL) != CAST(excluded.low_usd AS REAL) OR
                         CAST(pool_candles.close_usd AS REAL) != CAST(excluded.close_usd AS REAL) OR
                         CAST(pool_candles.volume_usd AS REAL) != CAST(excluded.volume_usd AS REAL)
                     THEN COALESCE(
                         pool_candles.revision_detected_at,
                         MAX(COALESCE(pool_candles.first_observed_values_at,
                                      excluded.fetched_at), excluded.fetched_at)
                     ) ELSE pool_candles.revision_detected_at END,
                    fetched_at=MAX(pool_candles.fetched_at, excluded.fetched_at)""",
                [
                    (
                        c.mint,
                        c.pool,
                        c.timeframe,
                        c.opened_at,
                        c.open,
                        c.high,
                        c.low,
                        c.close,
                        c.volume_usd,
                        fetched_at,
                        fetched_at,
                        fetched_at,
                        0.0,
                        0.0,
                    )
                    for c in candles
                ],
            )
            after = int(self._connection.execute("SELECT COUNT(*) FROM pool_candles").fetchone()[0])
            if owns_transaction:
                self._connection.commit()
        except Exception:
            if owns_transaction:
                self._connection.rollback()
            raise
        return after - before

    def pool_has_revision_since(
        self, mint: str, pool: str, *, opened_since: int, observed_by: int
    ) -> bool:
        return (
            self._connection.execute(
                """SELECT 1 FROM pool_candles
                   WHERE mint=? AND pool=? AND opened_at>=?
                   AND revision_detected_at<=? LIMIT 1""",
                (mint, pool, opened_since, observed_by),
            ).fetchone()
            is not None
        )

    def pool_candles(
        self, mint: str, pool: str, timeframe: str, *, limit: int = 10_000
    ) -> list[dict[str, object]]:
        if not 1 <= limit <= 10_000:
            raise JournalError("Candle limit must be between 1 and 10000")
        rows = self._connection.execute(
            """SELECT opened_at, open_usd, high_usd, low_usd, close_usd,
                        volume_usd, fetched_at, first_fetched_at,
                        first_observed_values_at, revision_detected_at FROM pool_candles
               WHERE mint=? AND pool=? AND timeframe=?
               ORDER BY opened_at DESC LIMIT ?""",
            (mint, pool, timeframe, limit),
        ).fetchall()
        return [
            dict(
                zip(
                    (
                        "opened_at",
                        "open",
                        "high",
                        "low",
                        "close",
                        "volume_usd",
                        "fetched_at",
                        "first_fetched_at",
                        "first_observed_values_at",
                        "revision_detected_at",
                    ),
                    row,
                    strict=True,
                )
            )
            for row in reversed(rows)
        ]

    def pool_candle_stats(self, mint: str, pool: str, timeframe: str) -> dict[str, int | None]:
        row = self._connection.execute(
            "SELECT COUNT(*), MAX(opened_at), "
            "SUM(CASE WHEN first_observed_values_at IS NOT NULL THEN 1 ELSE 0 END), "
            "MAX(CASE WHEN first_observed_values_at IS NOT NULL THEN opened_at END) "
            "FROM pool_candles "
            "WHERE mint=? AND pool=? AND timeframe=?",
            (mint, pool, timeframe),
        ).fetchone()
        return {
            "count": int(row[0]) if row else 0,
            "last_opened_at": int(row[1]) if row and row[1] is not None else None,
            "trusted_count": int(row[2]) if row and row[2] is not None else 0,
            "last_trusted_opened_at": int(row[3]) if row and row[3] is not None else None,
        }

    def record_backtest(
        self, *, mint: str, timeframe: str, strategy_name: str, result: dict[str, object]
    ) -> str:
        run_id = str(uuid.uuid4())
        self._connection.execute(
            """
            INSERT INTO strategy_backtests
            (run_id, created_at, mint, timeframe, strategy_name, result_json)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                run_id,
                datetime.now(UTC).isoformat(),
                mint,
                timeframe,
                strategy_name[:80],
                json.dumps(result, ensure_ascii=True),
            ),
        )
        return run_id

    def backtest_runs(self, *, limit: int = 50) -> list[dict[str, object]]:
        if not 1 <= limit <= 100:
            raise JournalError("Backtest limit must be between 1 and 100")
        rows = self._connection.execute(
            """
            SELECT run_id, created_at, mint, timeframe, strategy_name, result_json
            FROM strategy_backtests ORDER BY created_at DESC LIMIT ?
            """,
            (limit,),
        ).fetchall()
        output: list[dict[str, object]] = []
        for row in rows:
            try:
                result = json.loads(row[5])
            except (TypeError, json.JSONDecodeError):
                result = {}
            output.append(
                {
                    "run_id": row[0],
                    "created_at": row[1],
                    "mint": row[2],
                    "timeframe": row[3],
                    "strategy_name": row[4],
                    "result": result if isinstance(result, dict) else {},
                }
            )
        return output

    def discovery_candidates(self, *, limit: int = 50) -> list[dict[str, object]]:
        if not 1 <= limit <= 100:
            raise JournalError("Discovery limit must be between 1 and 100")
        rows = self._connection.execute(
            """
            SELECT mint, source_program, first_seen, last_seen, audit_status, audit_summary,
                   top10_concentration_bps, decimals, name, symbol, dex_id, pair_address, price_usd,
                   liquidity_usd, volume_24h_usd, price_change_24h, pair_created_at,
                   market_activity_json, hot_score, hot_state, hot_reasons_json,
                   hot_risk_flags_json
            FROM discovery_candidates
            ORDER BY last_seen DESC LIMIT ?
            """,
            (limit,),
        ).fetchall()
        keys = (
            "mint",
            "source_program",
            "first_seen",
            "last_seen",
            "audit_status",
            "audit_summary",
            "top10_concentration_bps",
            "decimals",
            "name",
            "symbol",
            "dex_id",
            "pair_address",
            "price_usd",
            "liquidity_usd",
            "volume_24h_usd",
            "price_change_24h",
            "pair_created_at",
        )
        output: list[dict[str, object]] = []
        for row in rows:
            item = dict(zip(keys, row[: len(keys)], strict=True))
            for index, name, fallback in (
                (17, "market_activity", {}),
                (20, "hot_reasons", []),
                (21, "hot_risk_flags", []),
            ):
                try:
                    parsed = json.loads(row[index])
                except (TypeError, json.JSONDecodeError):
                    parsed = fallback
                item[name] = parsed if isinstance(parsed, type(fallback)) else fallback
            item["hot_score"] = int(row[18]) if row[18] is not None else None
            item["hot_state"] = row[19]
            output.append(item)
        signals = self.narrative_signals(
            mints=[str(item["mint"]) for item in output], limit=min(100, limit)
        )
        signals_by_mint = {str(signal["mint"]): signal for signal in signals}
        for item in output:
            item["narrative_signal"] = signals_by_mint.get(str(item["mint"]))
        return output

    def recent_discovery_mints(self, *, limit: int = 100) -> list[str]:
        """Return a fixed recent population without filtering by score or audit result."""
        if not 1 <= limit <= 1_000:
            raise JournalError("Research population limit must be between 1 and 1000")
        rows = self._connection.execute(
            "SELECT mint FROM discovery_candidates ORDER BY first_seen DESC, mint LIMIT ?",
            (limit,),
        ).fetchall()
        return [str(row[0]) for row in rows]

    def discovery_candidate(self, mint: str) -> dict[str, object] | None:
        """Look up a saved research token even after it leaves the latest-candidate view."""
        row = self._connection.execute(
            """SELECT mint, first_seen, pair_address, symbol, name,
                      source_program, audit_status
               FROM discovery_candidates WHERE mint = ?""",
            (mint,),
        ).fetchone()
        return (
            {
                "mint": row[0],
                "first_seen": row[1],
                "pair_address": row[2],
                "symbol": row[3],
                "name": row[4],
                "source_program": row[5],
                "audit_status": row[6],
            }
            if row
            else None
        )

    def record_research_selection(
        self,
        mints: list[str],
        *,
        cohort_started_at: int,
        selected_at: int,
        expires_at: int,
        population_count: int,
        method: str,
    ) -> None:
        if not (0 <= cohort_started_at <= selected_at < expires_at and population_count >= 0):
            raise JournalError("Research cohort timing or population is invalid")
        if not method or len(method) > 100:
            raise JournalError("Research selection method is invalid")
        self._connection.executemany(
            """INSERT OR IGNORE INTO research_cohort_memberships
               (cohort_started_at, mint, selected_at, expires_at,
                population_count, selection_method)
               VALUES (?, ?, ?, ?, ?, ?)""",
            [
                (cohort_started_at, mint, selected_at, expires_at, population_count, method)
                for mint in dict.fromkeys(mints)
            ],
        )

    def record_decision_followup_selection(
        self,
        mint: str,
        *,
        selected_at: int,
        expires_at: int,
        population_count: int,
        method: str,
    ) -> None:
        if not mint or len(mint) > 100:
            raise JournalError("Follow-up mint is invalid")
        if not (0 <= selected_at < expires_at and population_count > 0):
            raise JournalError("Follow-up timing or population is invalid")
        if not method or len(method) > 100:
            raise JournalError("Follow-up selection method is invalid")
        self._connection.execute(
            """INSERT OR IGNORE INTO decision_followup_memberships
               (selected_at, mint, expires_at, population_count, selection_method)
               VALUES (?, ?, ?, ?, ?)""",
            (selected_at, mint, expires_at, population_count, method),
        )

    def decision_followup_selections(self, *, limit: int = 100) -> list[dict[str, object]]:
        if not 1 <= limit <= 1_000:
            raise JournalError("Follow-up selection limit must be between 1 and 1000")
        rows = self._connection.execute(
            """SELECT selected_at, mint, expires_at, population_count, selection_method
               FROM decision_followup_memberships ORDER BY selected_at DESC LIMIT ?""",
            (limit,),
        ).fetchall()
        return [
            dict(
                zip(
                    ("selected_at", "mint", "expires_at", "population_count", "selection_method"),
                    row,
                    strict=True,
                )
            )
            for row in rows
        ]

    def first_research_selected_at(
        self, mint: str, *, selection_method: str | None = None
    ) -> int | None:
        if selection_method is None:
            row = self._connection.execute(
                "SELECT MIN(selected_at) FROM research_cohort_memberships WHERE mint = ?",
                (mint,),
            ).fetchone()
        else:
            row = self._connection.execute(
                "SELECT MIN(selected_at) FROM research_cohort_memberships "
                "WHERE mint = ? AND selection_method = ?",
                (mint, selection_method),
            ).fetchone()
        return int(row[0]) if row and row[0] is not None else None

    def research_entry_windows(
        self, mint: str, *, selection_method: str | None = None
    ) -> tuple[tuple[int, int], ...]:
        if selection_method is None:
            rows = self._connection.execute(
                "SELECT selected_at, expires_at FROM research_cohort_memberships "
                "WHERE mint = ? ORDER BY selected_at",
                (mint,),
            ).fetchall()
        else:
            rows = self._connection.execute(
                "SELECT selected_at, expires_at FROM research_cohort_memberships "
                "WHERE mint = ? AND selection_method = ? ORDER BY selected_at",
                (mint, selection_method),
            ).fetchall()
        return tuple((int(start), int(end)) for start, end in rows)

    def upsert_narrative_items(self, items: list[dict[str, object]]) -> int:
        """Persist bounded, normalized narrative evidence without storing provider payloads."""
        now = datetime.now(UTC).isoformat()
        persisted = 0
        for item in items[:200]:
            item_id = str(item.get("item_id") or "")
            if len(item_id) != 64:
                continue
            self._connection.execute(
                """
                INSERT INTO narrative_items
                (item_id, observed_at, published_at, source, source_kind, source_name,
                 source_url, title, excerpt, engagement, evidence_tier, stance,
                 tags_json, risk_flags_json, matched_mints_json, explicit_mints_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(item_id) DO UPDATE SET
                    observed_at = excluded.observed_at,
                    engagement = MAX(narrative_items.engagement, excluded.engagement),
                    matched_mints_json = excluded.matched_mints_json,
                    explicit_mints_json = excluded.explicit_mints_json
                """,
                (
                    item_id,
                    now,
                    str(item.get("published_at") or now)[:40],
                    str(item.get("source") or "unknown")[:30],
                    str(item.get("source_kind") or "social")[:20],
                    str(item.get("source_name") or "unknown")[:120],
                    str(item["source_url"])[:2_048] if item.get("source_url") else None,
                    str(item.get("title") or "Untitled")[:300],
                    str(item.get("excerpt") or "")[:1_000],
                    _nonnegative_int(item.get("engagement")),
                    str(item.get("evidence_tier") or "unverified")[:40],
                    str(item.get("stance") or "neutral")[:20],
                    json.dumps(item.get("tags") or [], ensure_ascii=True),
                    json.dumps(item.get("risk_flags") or [], ensure_ascii=True),
                    json.dumps(item.get("matched_mints") or [], ensure_ascii=True),
                    json.dumps(item.get("explicit_mints") or [], ensure_ascii=True),
                ),
            )
            persisted += 1
        return persisted

    def update_narrative_item_matches(self, item_id: str, mints: tuple[str, ...]) -> bool:
        """Correct exact-address links without changing when an item was observed."""
        if len(item_id) != 64 or any(char not in "0123456789abcdef" for char in item_id):
            raise JournalError("Narrative item ID is invalid")
        if len(mints) > 100 or any(not 32 <= len(mint) <= 44 for mint in mints):
            raise JournalError("Narrative item matches are invalid")
        result = self._connection.execute(
            "UPDATE narrative_items SET matched_mints_json = ? WHERE item_id = ?",
            (json.dumps(mints, ensure_ascii=True), item_id),
        )
        return result.rowcount == 1

    def reserve_narrative_model_assessment(
        self, item_id: str, *, model: str, prompt_version: str, daily_limit: int
    ) -> bool:
        """Atomically reserve one public-item assessment within the daily request cap."""
        if (
            len(item_id) != 64
            or any(char not in "0123456789abcdef" for char in item_id)
            or not 1 <= daily_limit <= 100
            or not 1 <= len(model) <= 64
            or not 1 <= len(prompt_version) <= 40
        ):
            raise JournalError("Narrative model request is invalid")
        now = datetime.now(UTC).isoformat()
        self._connection.execute("BEGIN IMMEDIATE")
        try:
            existing = self._connection.execute(
                "SELECT 1 FROM narrative_model_assessments WHERE item_id=?", (item_id,)
            ).fetchone()
            source = self._connection.execute(
                "SELECT 1 FROM narrative_items WHERE item_id=?", (item_id,)
            ).fetchone()
            used = self._connection.execute(
                "SELECT COUNT(*) FROM narrative_model_assessments WHERE requested_at >= ?",
                (now[:10] + "T",),
            ).fetchone()
            if existing or not source or (used and int(used[0]) >= daily_limit):
                self._connection.execute("ROLLBACK")
                return False
            self._connection.execute(
                """INSERT INTO narrative_model_assessments
                   (item_id, requested_at, model, prompt_version, status)
                   VALUES (?, ?, ?, ?, 'pending')""",
                (item_id, now, model, prompt_version),
            )
            self._connection.execute("COMMIT")
            return True
        except Exception:
            self._connection.execute("ROLLBACK")
            raise

    def finish_narrative_model_assessment(
        self,
        item_id: str,
        *,
        result: dict[str, object] | None,
        input_tokens: int = 0,
        output_tokens: int = 0,
        error_code: str | None = None,
    ) -> None:
        """Store a bounded result or a safe error code, never a provider error body."""
        if result is not None and len(json.dumps(result, ensure_ascii=True)) > 2_000:
            raise JournalError("Narrative model result is too large")
        if not 0 <= input_tokens <= 100_000 or not 0 <= output_tokens <= 100_000:
            raise JournalError("Narrative model token count is invalid")
        if error_code is not None and (
            not 1 <= len(error_code) <= 40
            or any(char not in "abcdefghijklmnopqrstuvwxyz0123456789_" for char in error_code)
        ):
            raise JournalError("Narrative model error code is invalid")
        status = "complete" if result is not None else "failed"
        updated = self._connection.execute(
            """UPDATE narrative_model_assessments SET status=?, completed_at=?,
                      result_json=?, input_tokens=?, output_tokens=?, error_code=?
               WHERE item_id=? AND status='pending'""",
            (
                status,
                datetime.now(UTC).isoformat(),
                json.dumps(result, ensure_ascii=True) if result is not None else None,
                input_tokens,
                output_tokens,
                error_code,
                item_id,
            ),
        )
        if updated.rowcount != 1:
            raise JournalError("Narrative model request was not pending")

    def narrative_model_assessments(self, *, limit: int = 100) -> list[dict[str, object]]:
        if not 1 <= limit <= 100:
            raise JournalError("Narrative model result limit is invalid")
        rows = self._connection.execute(
            """SELECT item_id, requested_at, completed_at, model, prompt_version,
                      status, result_json, input_tokens, output_tokens, error_code
               FROM narrative_model_assessments ORDER BY requested_at DESC LIMIT ?""",
            (limit,),
        ).fetchall()
        assessments: list[dict[str, object]] = []
        for row in rows:
            try:
                result = json.loads(row[6]) if row[6] else None
            except (TypeError, json.JSONDecodeError):
                result = None
            assessments.append(
                {
                    "item_id": row[0],
                    "requested_at": row[1],
                    "completed_at": row[2],
                    "model": row[3],
                    "prompt_version": row[4],
                    "status": row[5],
                    "result": result if isinstance(result, dict) else None,
                    "input_tokens": row[7],
                    "output_tokens": row[8],
                    "error_code": row[9],
                }
            )
        return assessments

    def replace_narrative_signals(self, signals: list[dict[str, object]]) -> int:
        now = datetime.now(UTC).isoformat()
        self._connection.execute("SAVEPOINT narrative_refresh")
        try:
            self._connection.execute("DELETE FROM narrative_signals")
            for signal in signals[:100]:
                self._connection.execute(
                    """
                    INSERT INTO narrative_signals
                    (mint, updated_at, score, state, confidence, mention_count, unique_sources,
                     news_count, social_count, engagement, explicit_contract_mentions,
                     tags_json, reasons_json, risk_flags_json, latest_mention_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(mint) DO UPDATE SET
                        updated_at = excluded.updated_at,
                        score = excluded.score,
                        state = excluded.state,
                        confidence = excluded.confidence,
                        mention_count = excluded.mention_count,
                        unique_sources = excluded.unique_sources,
                        news_count = excluded.news_count,
                        social_count = excluded.social_count,
                        engagement = excluded.engagement,
                        explicit_contract_mentions = excluded.explicit_contract_mentions,
                        tags_json = excluded.tags_json,
                        reasons_json = excluded.reasons_json,
                        risk_flags_json = excluded.risk_flags_json,
                        latest_mention_at = excluded.latest_mention_at
                    """,
                    (
                        str(signal.get("mint") or "")[:64],
                        now,
                        min(100, _nonnegative_int(signal.get("score"))),
                        str(signal.get("state") or "quiet")[:20],
                        str(signal.get("confidence") or "low")[:20],
                        _nonnegative_int(signal.get("mention_count")),
                        _nonnegative_int(signal.get("unique_sources")),
                        _nonnegative_int(signal.get("news_count")),
                        _nonnegative_int(signal.get("social_count")),
                        _nonnegative_int(signal.get("engagement")),
                        _nonnegative_int(signal.get("explicit_contract_mentions")),
                        json.dumps(signal.get("tags") or [], ensure_ascii=True),
                        json.dumps(signal.get("reasons") or [], ensure_ascii=True),
                        json.dumps(signal.get("risk_flags") or [], ensure_ascii=True),
                        (
                            str(signal["latest_mention_at"])[:40]
                            if isinstance(signal.get("latest_mention_at"), str)
                            else None
                        ),
                    ),
                )
            observed = {
                str(signal["mint"]): {
                    "updated_at": signal["updated_at"],
                    "latest_mention_at": signal["latest_mention_at"],
                    "confidence": signal["confidence"],
                    "explicit_contract_mentions": signal["explicit_contract_mentions"],
                    "unique_sources": signal["unique_sources"],
                    "risk_flags": signal["risk_flags"],
                }
                for signal in self.narrative_signals()
            }
            self._connection.execute(
                "INSERT INTO narrative_signal_observations(captured_at_us, signals_json) "
                "VALUES (?, ?)",
                (time.time_ns() // 1_000, json.dumps(observed, ensure_ascii=True)),
            )
            self._connection.execute("RELEASE SAVEPOINT narrative_refresh")
        except Exception:
            self._connection.execute("ROLLBACK TO SAVEPOINT narrative_refresh")
            self._connection.execute("RELEASE SAVEPOINT narrative_refresh")
            raise
        return min(len(signals), 100)

    def narrative_signal_before(
        self, mint: str, *, decision_at_us: int
    ) -> dict[str, object] | None:
        """Return the last complete refresh, including an explicit absent signal."""
        if type(decision_at_us) is not int or decision_at_us < 0:
            raise JournalError("Narrative decision time is invalid")
        row = self._connection.execute(
            """SELECT captured_at_us, signals_json FROM narrative_signal_observations
               WHERE captured_at_us <= ?
               ORDER BY captured_at_us DESC, refresh_id DESC LIMIT 1""",
            (decision_at_us,),
        ).fetchone()
        if row is None:
            return None
        try:
            observed = json.loads(row[1])
        except (TypeError, json.JSONDecodeError) as exc:
            raise JournalError("Saved narrative observation is invalid") from exc
        if not isinstance(observed, dict):
            raise JournalError("Saved narrative observation is invalid")
        signal = observed.get(mint)
        return {
            "captured_at_us": int(row[0]),
            "signal": signal if isinstance(signal, dict) else None,
        }

    def narrative_items(self, *, limit: int = 100) -> list[dict[str, object]]:
        if not 1 <= limit <= 200:
            raise JournalError("Narrative item limit must be between 1 and 200")
        rows = self._connection.execute(
            """
            SELECT item_id, observed_at, published_at, source, source_kind, source_name,
                   source_url, title, excerpt, engagement, evidence_tier, stance,
                   tags_json, risk_flags_json, matched_mints_json, explicit_mints_json
            FROM narrative_items ORDER BY published_at DESC LIMIT ?
            """,
            (limit,),
        ).fetchall()
        keys = (
            "item_id",
            "observed_at",
            "published_at",
            "source",
            "source_kind",
            "source_name",
            "source_url",
            "title",
            "excerpt",
            "engagement",
            "evidence_tier",
            "stance",
        )
        output: list[dict[str, object]] = []
        for row in rows:
            item = dict(zip(keys, row[: len(keys)], strict=True))
            for index, name in (
                (12, "tags"),
                (13, "risk_flags"),
                (14, "matched_mints"),
                (15, "explicit_mints"),
            ):
                try:
                    parsed = json.loads(row[index])
                except (TypeError, json.JSONDecodeError):
                    parsed = []
                item[name] = parsed if isinstance(parsed, list) else []
            output.append(item)
        return output

    def narrative_signals(
        self, *, mints: list[str] | None = None, limit: int = 100
    ) -> list[dict[str, object]]:
        if not 1 <= limit <= 100:
            raise JournalError("Narrative signal limit must be between 1 and 100")
        rows = self._connection.execute(
            """
            SELECT mint, updated_at, score, state, confidence, mention_count,
                   unique_sources, news_count, social_count, engagement,
                   explicit_contract_mentions, tags_json, reasons_json, risk_flags_json,
                   latest_mention_at
            FROM narrative_signals ORDER BY score DESC LIMIT 100
            """
        ).fetchall()
        if mints:
            selected = set(mints[:100])
            rows = [row for row in rows if str(row[0]) in selected]
        rows = rows[:limit]
        keys = (
            "mint",
            "updated_at",
            "score",
            "state",
            "confidence",
            "mention_count",
            "unique_sources",
            "news_count",
            "social_count",
            "engagement",
            "explicit_contract_mentions",
        )
        output: list[dict[str, object]] = []
        for row in rows:
            signal = dict(zip(keys, row[: len(keys)], strict=True))
            signal["latest_mention_at"] = row[14]
            for index, name in ((11, "tags"), (12, "reasons"), (13, "risk_flags")):
                try:
                    parsed = json.loads(row[index])
                except (TypeError, json.JSONDecodeError):
                    parsed = []
                signal[name] = parsed if isinstance(parsed, list) else []
            output.append(signal)
        return output

    def paper_cash_lamports(self, starting_lamports: int) -> int:
        if starting_lamports <= 0:
            raise JournalError("Paper starting balance must be positive")
        row = self._connection.execute(
            """
            SELECT COALESCE(SUM(CASE WHEN side = 'buy' THEN input_amount ELSE 0 END), 0),
                   COALESCE(SUM(CASE WHEN side = 'sell' THEN output_amount ELSE 0 END), 0)
            FROM paper_fills
            """
        ).fetchone()
        spent, returned = (int(row[0]), int(row[1])) if row else (0, 0)
        return starting_lamports - spent + returned

    def open_paper_position(
        self,
        *,
        position_id: str,
        mint: str,
        entry_lamports: int,
        token_amount: int,
        simulated_units: int,
        starting_lamports: int,
        execution: dict[str, object] | None = None,
        decision: StrategyDecision | None = None,
        decision_action: str | None = None,
        max_open_positions: int | None = None,
        entry_guard: Callable[[], None] | None = None,
    ) -> dict[str, object]:
        if not position_id or len(position_id) > 100:
            raise JournalError("Paper position ID must contain 1 to 100 characters")
        if min(entry_lamports, token_amount) <= 0 or simulated_units < 0:
            raise JournalError("Paper fill amounts are invalid")
        if (decision is None) != (decision_action is None):
            raise JournalError("Paper entry decision and action must be recorded together")
        if decision is not None and decision.mint != mint:
            raise JournalError("Paper entry decision mint does not match the position")
        if max_open_positions is not None and not 1 <= max_open_positions <= 100:
            raise JournalError("Paper open-position limit is invalid")
        try:
            self._connection.execute("BEGIN IMMEDIATE")
            self._require_active_paper_campaign()
            if entry_guard is not None:
                entry_guard()
            if max_open_positions is not None:
                open_rows = self._connection.execute(
                    "SELECT mint FROM paper_positions WHERE status = 'open'"
                ).fetchall()
                if any(str(row[0]) == mint for row in open_rows):
                    raise JournalError("Paper position is already open for this token")
                if len(open_rows) >= max_open_positions:
                    raise JournalError("Paper open-position limit has been reached")
            if self.paper_cash_lamports(starting_lamports) < entry_lamports:
                raise JournalError("Paper portfolio has insufficient available SOL")
            now = datetime.now(UTC).isoformat()
            self._connection.execute(
                """
                INSERT INTO paper_positions
                (position_id, mint, entry_lamports, token_amount, simulated_units, opened_at,
                 peak_value_lamports, remaining_token_amount, entry_execution_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    position_id,
                    mint,
                    entry_lamports,
                    token_amount,
                    simulated_units,
                    now,
                    entry_lamports,
                    token_amount,
                    json.dumps(execution or {}, ensure_ascii=True),
                ),
            )
            self._connection.execute(
                """
                INSERT INTO paper_fills
                (fill_id, position_id, side, input_amount, output_amount,
                 cost_lamports, created_at, execution_json)
                VALUES (?, ?, 'buy', ?, ?, ?, ?, ?)
                """,
                (
                    f"{position_id}:buy",
                    position_id,
                    entry_lamports,
                    token_amount,
                    entry_lamports,
                    now,
                    json.dumps(execution or {}, ensure_ascii=True),
                ),
            )
            if decision is not None and decision_action is not None:
                self.record_strategy_decision(
                    decision, action=decision_action, position_id=position_id
                )
            self._connection.execute("COMMIT")
        except sqlite3.IntegrityError as exc:
            self._connection.execute("ROLLBACK")
            raise JournalError("Paper position ID has already been used") from exc
        except Exception:
            self._connection.execute("ROLLBACK")
            raise
        return self.paper_position(position_id)

    def paper_mint_has_position(self, mint: str) -> bool:
        """A paper ATA reserve is charged only for its first simulated creation."""
        return (
            self._connection.execute(
                "SELECT 1 FROM paper_positions WHERE mint = ? LIMIT 1", (mint,)
            ).fetchone()
            is not None
        )

    def _require_active_paper_campaign(self) -> None:
        """Check inside the write transaction so interruption wins any race."""
        campaign = self.runtime_state("paper_campaign") or {}
        if campaign.get("mode") == "forward_paper_only" and campaign.get("status") == "interrupted":
            raise JournalError("Paper campaign was interrupted; start a clean run")

    def paper_position(self, position_id: str) -> dict[str, object]:
        row = self._connection.execute(
            """
            SELECT position_id, mint, entry_lamports, token_amount, simulated_units,
                   opened_at, status, closed_lamports, closed_at,
                   last_value_lamports, last_quoted_at, peak_value_lamports,
                   remaining_token_amount, realized_lamports, realized_cost_lamports, tp_stage,
                    last_exit_reason, entry_execution_json, last_mark_execution_json
            FROM paper_positions WHERE position_id = ?
            """,
            (position_id,),
        ).fetchone()
        if row is None:
            raise JournalError("Paper position was not found")
        keys = (
            "position_id",
            "mint",
            "entry_lamports",
            "token_amount",
            "simulated_units",
            "opened_at",
            "status",
            "closed_lamports",
            "closed_at",
            "last_value_lamports",
            "last_quoted_at",
            "peak_value_lamports",
            "remaining_token_amount",
            "realized_lamports",
            "realized_cost_lamports",
            "tp_stage",
            "last_exit_reason",
            "entry_execution",
            "last_mark_execution",
        )
        output = dict(zip(keys, row, strict=True))
        for name in ("entry_execution", "last_mark_execution"):
            try:
                parsed = json.loads(str(output[name]))
            except (TypeError, json.JSONDecodeError):
                parsed = {}
            output[name] = parsed if isinstance(parsed, dict) else {}
        return output

    def paper_positions(self, *, limit: int = 100) -> list[dict[str, object]]:
        if not 1 <= limit <= 100:
            raise JournalError("Paper position limit must be between 1 and 100")
        rows = self._connection.execute(
            """
            SELECT position_id FROM paper_positions
            ORDER BY opened_at DESC LIMIT ?
            """,
            (limit,),
        ).fetchall()
        return [self.paper_position(str(row[0])) for row in rows]

    def closed_paper_position_count(self) -> int:
        row = self._connection.execute(
            "SELECT COUNT(*) FROM paper_positions WHERE status = 'closed'"
        ).fetchone()
        return int(row[0]) if row else 0

    def mark_paper_position(
        self,
        position_id: str,
        value_lamports: int,
        *,
        peak_reference_lamports: int | None = None,
        execution: dict[str, object] | None = None,
    ) -> None:
        if value_lamports <= 0:
            raise JournalError("Paper position value must be positive")
        peak_reference = peak_reference_lamports or value_lamports
        if peak_reference <= 0:
            raise JournalError("Paper position peak reference must be positive")
        observed_at = datetime.now(UTC).isoformat()
        execution_json = json.dumps(execution or {}, ensure_ascii=True)
        owns_transaction = not self._connection.in_transaction
        if owns_transaction:
            self._connection.execute("BEGIN IMMEDIATE")
        try:
            self._require_active_paper_campaign()
            cursor = self._connection.execute(
                """
                UPDATE paper_positions
                SET last_value_lamports = ?, last_quoted_at = ?,
                    peak_value_lamports = MAX(COALESCE(peak_value_lamports, 0), ?),
                    last_mark_execution_json = ?
                WHERE position_id = ? AND status = 'open'
                """,
                (value_lamports, observed_at, peak_reference, execution_json, position_id),
            )
            if cursor.rowcount != 1:
                raise JournalError("Open paper position was not found")
            self._connection.execute(
                """
                INSERT INTO paper_position_marks
                (mark_id, position_id, observed_at, remaining_token_amount,
                 value_lamports, performance_value_lamports, peak_value_lamports,
                 tp_stage, execution_json)
                SELECT ?, position_id, ?, remaining_token_amount, ?, ?,
                       peak_value_lamports, tp_stage, ?
                FROM paper_positions WHERE position_id = ? AND status = 'open'
                """,
                (
                    str(uuid.uuid4()),
                    observed_at,
                    value_lamports,
                    peak_reference,
                    execution_json,
                    position_id,
                ),
            )
            if owns_transaction:
                self._connection.commit()
        except Exception:
            if owns_transaction:
                self._connection.rollback()
            raise

    def paper_position_marks(
        self, position_id: str, *, limit: int = 1_000
    ) -> list[dict[str, object]]:
        if not 1 <= limit <= 10_000:
            raise JournalError("Paper mark limit must be between 1 and 10000")
        rows = self._connection.execute(
            """SELECT observed_at, remaining_token_amount, value_lamports,
                      performance_value_lamports, peak_value_lamports, tp_stage,
                      execution_json
               FROM paper_position_marks WHERE position_id=?
               ORDER BY observed_at DESC, rowid DESC LIMIT ?""",
            (position_id, limit),
        ).fetchall()
        output: list[dict[str, object]] = []
        for row in reversed(rows):
            try:
                raw_execution = json.loads(str(row[6]))
            except (TypeError, json.JSONDecodeError):
                raw_execution = {}
            output.append(
                {
                    "observed_at": row[0],
                    "remaining_token_amount": row[1],
                    "value_lamports": row[2],
                    "performance_value_lamports": row[3],
                    "peak_value_lamports": row[4],
                    "tp_stage": row[5],
                    "execution": raw_execution if isinstance(raw_execution, dict) else {},
                }
            )
        return output

    def partial_close_paper_position(
        self,
        position_id: str,
        *,
        sold_token_amount: int,
        exit_lamports: int,
        advance_tp_stage: bool,
        exit_reason: str = "manual_partial",
        execution: dict[str, object] | None = None,
    ) -> dict[str, object]:
        if sold_token_amount <= 0:
            raise JournalError("Paper token sale amount must be positive")
        if exit_lamports <= 0:
            raise JournalError("Paper exit value must be positive")
        if not exit_reason or len(exit_reason) > 80 or not exit_reason.replace("_", "").isalnum():
            raise JournalError("Paper exit reason is invalid")
        now = datetime.now(UTC).isoformat()
        try:
            self._connection.execute("BEGIN IMMEDIATE")
            self._require_active_paper_campaign()
            row = self._connection.execute(
                """
                SELECT token_amount, remaining_token_amount, status, entry_lamports,
                       realized_lamports, realized_cost_lamports, tp_stage
                FROM paper_positions WHERE position_id = ?
                """,
                (position_id,),
            ).fetchone()
            if row is None:
                raise JournalError("Paper position was not found")
            if row[2] != "open":
                raise JournalError("Paper position is already closed")
            original_tokens = int(row[0])
            remaining_tokens = int(row[1])
            if sold_token_amount > remaining_tokens:
                raise JournalError("Paper sale exceeds the remaining token amount")
            entry_lamports = int(row[3])
            realized_lamports = int(row[4])
            realized_cost = int(row[5])
            tp_stage = int(row[6])
            remaining_after = remaining_tokens - sold_token_amount
            final_sale = remaining_after == 0
            allocated_cost = (
                entry_lamports - realized_cost
                if final_sale
                else entry_lamports * sold_token_amount // original_tokens
            )
            realized_after = realized_lamports + exit_lamports
            realized_cost_after = realized_cost + allocated_cost
            next_stage = tp_stage + int(advance_tp_stage)
            remaining_mark = (
                None if final_sale else max(1, exit_lamports * remaining_after // sold_token_amount)
            )
            self._connection.execute(
                """
                UPDATE paper_positions
                SET status = ?, remaining_token_amount = ?, realized_lamports = ?,
                    realized_cost_lamports = ?, tp_stage = ?, closed_lamports = ?,
                    closed_at = ?, last_value_lamports = ?, last_quoted_at = ?,
                    last_exit_reason = ?
                WHERE position_id = ?
                """,
                (
                    "closed" if final_sale else "open",
                    remaining_after,
                    realized_after,
                    realized_cost_after,
                    next_stage,
                    realized_after if final_sale else None,
                    now if final_sale else None,
                    realized_after if final_sale else remaining_mark,
                    now if final_sale else None,
                    exit_reason,
                    position_id,
                ),
            )
            self._connection.execute(
                """
                INSERT INTO paper_fills
                (fill_id, position_id, side, input_amount, output_amount,
                  cost_lamports, exit_reason, created_at, execution_json)
                VALUES (?, ?, 'sell', ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(uuid.uuid4()),
                    position_id,
                    sold_token_amount,
                    exit_lamports,
                    allocated_cost,
                    exit_reason,
                    now,
                    json.dumps(execution or {}, ensure_ascii=True),
                ),
            )
            self._connection.execute("COMMIT")
        except Exception:
            self._connection.execute("ROLLBACK")
            raise
        return self.paper_position(position_id)

    def close_paper_position(
        self,
        position_id: str,
        exit_lamports: int,
        *,
        exit_reason: str = "manual",
        execution: dict[str, object] | None = None,
    ) -> dict[str, object]:
        position = self.paper_position(position_id)
        remaining = position.get("remaining_token_amount")
        if not isinstance(remaining, int) or remaining <= 0:
            raise JournalError("Paper position is already closed")
        return self.partial_close_paper_position(
            position_id,
            sold_token_amount=remaining,
            exit_lamports=exit_lamports,
            advance_tp_stage=False,
            exit_reason=exit_reason,
            execution=execution,
        )

    def paper_fills(self, *, limit: int = 500) -> list[dict[str, object]]:
        if not 1 <= limit <= 1_000:
            raise JournalError("Paper fill limit must be between 1 and 1000")
        rows = self._connection.execute(
            """
            SELECT fill_id, position_id, side, input_amount, output_amount,
                   cost_lamports, exit_reason, created_at, execution_json
            FROM paper_fills ORDER BY rowid DESC LIMIT ?
            """,
            (limit,),
        ).fetchall()
        output: list[dict[str, object]] = []
        for row in rows:
            try:
                execution = json.loads(str(row[8]))
            except (TypeError, json.JSONDecodeError):
                execution = {}
            output.append(
                {
                    "fill_id": row[0],
                    "position_id": row[1],
                    "side": row[2],
                    "input_amount": int(row[3]),
                    "output_amount": int(row[4]),
                    "cost_lamports": int(row[5]),
                    "exit_reason": row[6],
                    "created_at": row[7],
                    "execution": execution if isinstance(execution, dict) else {},
                }
            )
        return output

    def paper_risk_metrics(self) -> dict[str, int]:
        day_prefix = datetime.now(UTC).date().isoformat() + "%"
        row = self._connection.execute(
            """
            SELECT COALESCE(SUM(output_amount - cost_lamports), 0)
            FROM paper_fills
            WHERE side = 'sell' AND created_at LIKE ?
            """,
            (day_prefix,),
        ).fetchone()
        daily_realized = int(row[0]) if row else 0
        closed = self._connection.execute(
            """
            SELECT p.closed_lamports - p.entry_lamports
            FROM paper_positions AS p
            JOIN paper_fills AS f ON f.position_id = p.position_id AND f.side = 'sell'
            WHERE p.status = 'closed'
              AND f.rowid = (
                  SELECT MAX(last_fill.rowid) FROM paper_fills AS last_fill
                  WHERE last_fill.position_id = p.position_id AND last_fill.side = 'sell'
              )
            ORDER BY f.rowid DESC LIMIT 100
            """
        ).fetchall()
        consecutive_losses = 0
        for result in closed:
            if int(result[0]) < 0:
                consecutive_losses += 1
            else:
                break
        return {
            "daily_realized_pnl_lamports": daily_realized,
            "consecutive_losses": consecutive_losses,
        }

    def record_strategy_decision(
        self,
        decision: StrategyDecision,
        *,
        action: str,
        position_id: str | None = None,
    ) -> None:
        if not action or len(action) > 100:
            raise JournalError("Strategy action must contain 1 to 100 characters")
        if decision.verdict not in ("reject", "observe", "eligible"):
            raise JournalError("Strategy verdict is invalid")
        self._connection.execute(
            """
            INSERT INTO strategy_decisions
            (decision_id, created_at, mint, score, verdict, reasons_json, evidence_json, action,
             paper_position_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                str(uuid.uuid4()),
                datetime.now(UTC).isoformat(),
                decision.mint,
                decision.score,
                decision.verdict,
                json.dumps(decision.reasons, ensure_ascii=True),
                json.dumps(decision.evidence, ensure_ascii=True),
                action,
                position_id,
            ),
        )

    def record_collector_signal_observation(self, decision: StrategyDecision) -> None:
        """Preserve a focused, prospective signal without treating it as a paper order."""
        if decision.verdict not in ("reject", "observe", "eligible"):
            raise JournalError("Collector signal verdict is invalid")
        self._connection.execute(
            """
            INSERT INTO collector_signal_observations
            (observation_id, created_at, mint, score, verdict, reasons_json, evidence_json)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                str(uuid.uuid4()),
                datetime.now(UTC).isoformat(),
                decision.mint,
                decision.score,
                decision.verdict,
                json.dumps(decision.reasons, ensure_ascii=True),
                json.dumps(decision.evidence, ensure_ascii=True),
            ),
        )

    def collector_signal_observations(
        self, *, limit: int = 100, before: int | None = None
    ) -> list[dict[str, object]]:
        if not 1 <= limit <= 100:
            raise JournalError("Collector signal limit must be between 1 and 100")
        if before is not None and (type(before) is not int or not 0 < before <= 4_102_444_800):
            raise JournalError("Collector signal cutoff is invalid")
        cutoff = datetime.fromtimestamp(before, UTC).isoformat() if before is not None else None
        if cutoff is None:
            rows = self._connection.execute(
                """
                SELECT observation_id, created_at, mint, score, verdict,
                       reasons_json, evidence_json
                FROM collector_signal_observations
                ORDER BY created_at DESC LIMIT ?
                """,
                (limit,),
            ).fetchall()
        else:
            rows = self._connection.execute(
                """
                SELECT observation_id, created_at, mint, score, verdict,
                       reasons_json, evidence_json
                FROM collector_signal_observations
                WHERE created_at <= ?
                ORDER BY created_at DESC LIMIT ?
                """,
                (cutoff, limit),
            ).fetchall()
        output: list[dict[str, object]] = []
        for row in rows:
            try:
                reasons = json.loads(row[5])
                evidence = json.loads(row[6])
            except (TypeError, json.JSONDecodeError):
                reasons, evidence = [], {}
            output.append(
                {
                    "decision_id": row[0],
                    "created_at": row[1],
                    "mint": row[2],
                    "score": row[3],
                    "verdict": row[4],
                    "reasons": reasons if isinstance(reasons, list) else [],
                    "evidence": evidence if isinstance(evidence, dict) else {},
                    "action": "collector_observation",
                }
            )
        return output

    def market_policy_before(self, mint: str, *, decision_at_us: int) -> dict[str, object] | None:
        """Read the latest policy logged for this mint by a prior paper decision."""
        if (
            not mint
            or len(mint) > 64
            or type(decision_at_us) is not int
            or not 0 < decision_at_us <= 4_102_444_800_000_000
        ):
            raise JournalError("Market policy lookup is invalid")
        cutoff = (
            datetime.fromtimestamp(decision_at_us // 1_000_000, UTC)
            .replace(microsecond=decision_at_us % 1_000_000)
            .isoformat()
        )
        row = self._connection.execute(
            """SELECT created_at, evidence_json FROM strategy_decisions
               WHERE mint = ? AND created_at <= ?
               ORDER BY created_at DESC, rowid DESC LIMIT 1""",
            (mint, cutoff),
        ).fetchone()
        if row is None:
            return None
        try:
            observed_at = datetime.fromisoformat(str(row[0]))
            evidence = json.loads(row[1])
        except (TypeError, ValueError) as exc:
            raise JournalError("Saved market policy evidence is invalid") from exc
        if observed_at.tzinfo is None or not isinstance(evidence, dict):
            raise JournalError("Saved market policy evidence is invalid")
        policy = evidence.get("market_policy")
        if not isinstance(policy, dict):
            return None
        return {
            "observed_at_us": int(observed_at.timestamp()) * 1_000_000 + observed_at.microsecond,
            "version": evidence.get("market_policy_version"),
            "policy": policy,
        }

    def strategy_decisions(
        self, *, limit: int = 100, before: int | None = None
    ) -> list[dict[str, object]]:
        if not 1 <= limit <= 100:
            raise JournalError("Strategy decision limit must be between 1 and 100")
        if before is not None:
            if type(before) is not int or not 0 < before <= 4_102_444_800:
                raise JournalError("Strategy decision cutoff is invalid")
            rows = self._connection.execute(
                """
                SELECT decision_id, created_at, mint, score, verdict, reasons_json,
                       evidence_json, action, paper_position_id
                FROM strategy_decisions WHERE created_at <= ?
                ORDER BY created_at DESC LIMIT ?
                """,
                (datetime.fromtimestamp(before, UTC).isoformat(), limit),
            ).fetchall()
        else:
            rows = self._connection.execute(
                """
                SELECT decision_id, created_at, mint, score, verdict, reasons_json,
                       evidence_json, action, paper_position_id
                FROM strategy_decisions ORDER BY created_at DESC LIMIT ?
                """,
                (limit,),
            ).fetchall()
        output: list[dict[str, object]] = []
        for row in rows:
            try:
                reasons = json.loads(row[5])
            except (TypeError, json.JSONDecodeError):
                reasons = []
            try:
                evidence = json.loads(row[6])
            except (TypeError, json.JSONDecodeError):
                evidence = {}
            output.append(
                {
                    "decision_id": row[0],
                    "created_at": row[1],
                    "mint": row[2],
                    "score": int(row[3]),
                    "verdict": row[4],
                    "reasons": reasons if isinstance(reasons, list) else [],
                    "evidence": evidence if isinstance(evidence, dict) else {},
                    "action": row[7],
                    "paper_position_id": row[8],
                }
            )
        return output

    def record_automation_event(
        self,
        *,
        event_type: str,
        message: str,
        level: str = "info",
        metadata: dict[str, object] | None = None,
    ) -> None:
        if not event_type or len(event_type) > 80:
            raise JournalError("Automation event type must contain 1 to 80 characters")
        if not message or len(message) > 500:
            raise JournalError("Automation event message must contain 1 to 500 characters")
        if level not in {"info", "warning", "error"}:
            raise JournalError("Automation event level is invalid")
        self._connection.execute(
            """
            INSERT INTO paper_automation_events
            (event_id, created_at, event_type, level, message, metadata_json)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                str(uuid.uuid4()),
                datetime.now(UTC).isoformat(),
                event_type,
                level,
                message,
                json.dumps(metadata or {}, ensure_ascii=True),
            ),
        )

    def automation_events(self, *, limit: int = 50) -> list[dict[str, object]]:
        if not 1 <= limit <= 100:
            raise JournalError("Automation event limit must be between 1 and 100")
        rows = self._connection.execute(
            """
            SELECT event_id, created_at, event_type, level, message, metadata_json
            FROM paper_automation_events ORDER BY created_at DESC, rowid DESC LIMIT ?
            """,
            (limit,),
        ).fetchall()
        output: list[dict[str, object]] = []
        for row in rows:
            try:
                metadata = json.loads(row[5])
            except (TypeError, json.JSONDecodeError):
                metadata = {}
            output.append(
                {
                    "event_id": row[0],
                    "created_at": row[1],
                    "event_type": row[2],
                    "level": row[3],
                    "message": row[4],
                    "metadata": metadata if isinstance(metadata, dict) else {},
                }
            )
        return output

    def create_paper_call(self, *, mint: str, source_label: str | None) -> str:
        if source_label is not None and len(source_label) > 80:
            raise JournalError("Call source label must be at most 80 characters")
        call_id = str(uuid.uuid4())
        self._connection.execute(
            """
            INSERT INTO paper_calls
            (call_id, created_at, mint, source_label, status)
            VALUES (?, ?, ?, ?, 'processing')
            """,
            (call_id, datetime.now(UTC).isoformat(), mint, source_label),
        )
        return call_id

    def update_paper_call(
        self,
        call_id: str,
        *,
        status: str,
        score: int | None = None,
        verdict: str | None = None,
        reason: str | None = None,
        position_id: str | None = None,
    ) -> None:
        if status not in {"rejected", "observed", "opened", "error"}:
            raise JournalError("Paper call status is invalid")
        if score is not None and not 0 <= score <= 100:
            raise JournalError("Paper call score is invalid")
        if reason is not None and len(reason) > 500:
            raise JournalError("Paper call reason is too long")
        cursor = self._connection.execute(
            """
            UPDATE paper_calls
            SET status = ?, score = ?, verdict = ?, reason = ?, paper_position_id = ?
            WHERE call_id = ? AND status = 'processing'
            """,
            (status, score, verdict, reason, position_id, call_id),
        )
        if cursor.rowcount != 1:
            raise JournalError("Processing paper call was not found")

    def paper_calls(self, *, limit: int = 50) -> list[dict[str, object]]:
        if not 1 <= limit <= 100:
            raise JournalError("Paper call limit must be between 1 and 100")
        rows = self._connection.execute(
            """
            SELECT call_id, created_at, mint, source_label, status, score, verdict,
                   reason, paper_position_id
            FROM paper_calls ORDER BY created_at DESC LIMIT ?
            """,
            (limit,),
        ).fetchall()
        keys = (
            "call_id",
            "created_at",
            "mint",
            "source_label",
            "status",
            "score",
            "verdict",
            "reason",
            "paper_position_id",
        )
        return [dict(zip(keys, row, strict=True)) for row in rows]

    def set_runtime_state(self, state_key: str, payload: dict[str, object]) -> None:
        if not state_key or len(state_key) > 80:
            raise JournalError("Runtime state key must contain 1 to 80 characters")
        self._connection.execute(
            """
            INSERT INTO runtime_state (state_key, updated_at, payload_json)
            VALUES (?, ?, ?)
            ON CONFLICT(state_key) DO UPDATE SET
                updated_at = excluded.updated_at,
                payload_json = excluded.payload_json
            """,
            (
                state_key,
                datetime.now(UTC).isoformat(),
                json.dumps(payload, ensure_ascii=True),
            ),
        )

    def save_worker_state(self, worker_key: str, payload: dict[str, object]) -> None:
        """Persist the latest state and a prospective completion record atomically."""
        if worker_key not in {
            "paper_automation",
            "paper_exit_monitor",
            "paper_watchdog",
            "market_collector",
            "narrative_collector",
        }:
            raise JournalError("Unknown worker key")
        started = payload.get("last_started_at")
        completed = payload.get("last_completed_at")
        cycle: tuple[str, str, str, str, str | None, str | None] | None = None
        if isinstance(started, str) and isinstance(completed, str):
            try:
                start_time = datetime.fromisoformat(started)
                end_time = datetime.fromisoformat(completed)
            except ValueError:
                pass
            else:
                if start_time.tzinfo and end_time.tzinfo and end_time >= start_time:
                    outcome = (
                        "error"
                        if payload.get("last_error")
                        else "paused"
                        if payload.get("state") == "paused"
                        else "completed"
                    )
                    run_id = payload.get("run_id")
                    last_result = payload.get("last_result")
                    request_counts = (
                        last_result.get("rpc_request_counts")
                        if isinstance(last_result, dict)
                        else None
                    )
                    counts_json = (
                        json.dumps(request_counts, sort_keys=True)
                        if isinstance(request_counts, dict)
                        else None
                    )
                    cycle = (
                        worker_key,
                        started,
                        completed,
                        outcome,
                        run_id if isinstance(run_id, str) and len(run_id) <= 80 else None,
                        counts_json,
                    )
        self._connection.execute("BEGIN IMMEDIATE")
        try:
            previous = self.runtime_state(worker_key)
            self.set_runtime_state(worker_key, payload)
            if cycle is not None and (
                previous is None or previous.get("last_completed_at") != completed
            ):
                self._connection.execute(
                    "INSERT OR IGNORE INTO worker_cycle_history "
                    "(worker_key, started_at, completed_at, outcome, run_id, rpc_requests_json) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    cycle,
                )
            self._connection.execute("COMMIT")
        except Exception:
            self._connection.execute("ROLLBACK")
            raise

    def worker_cycles_since(self, worker_key: str, since: str) -> list[dict[str, str | None]]:
        rows = self._connection.execute(
            "SELECT started_at, completed_at, outcome, run_id FROM worker_cycle_history "
            "WHERE worker_key = ? AND completed_at >= ? "
            "ORDER BY completed_at, started_at",
            (worker_key, since),
        ).fetchall()
        return [
            {
                "started_at": str(row[0]),
                "completed_at": str(row[1]),
                "outcome": str(row[2]),
                "run_id": str(row[3]) if row[3] is not None else None,
            }
            for row in rows
        ]

    def origin_trace_cache(self, mint: str) -> tuple[int, OriginEvidence | None] | None:
        """Read verified immutable origin evidence or a recent negative lookup."""
        row = self._connection.execute(
            "SELECT checked_at_us, origin_json FROM token_origin_cache WHERE mint=?", (mint,)
        ).fetchone()
        if row is None or type(row[0]) is not int or row[0] < 0:
            return None
        checked_at_us = int(row[0])
        if row[1] is None:
            return checked_at_us, None
        try:
            payload = json.loads(str(row[1]))
        except (TypeError, json.JSONDecodeError):
            return None
        if not isinstance(payload, dict):
            return None
        signer = payload.get("creation_signer")
        signature = payload.get("signature")
        slot = payload.get("slot")
        block_time = payload.get("block_time")
        history_complete = payload.get("history_complete")
        if (
            not isinstance(signer, str)
            or not 1 <= len(signer) <= 64
            or not isinstance(signature, str)
            or not 1 <= len(signature) <= 128
            or payload.get("confidence") != "high"
            or type(history_complete) is not bool
            or (slot is not None and (type(slot) is not int or slot < 0))
            or (block_time is not None and (type(block_time) is not int or block_time < 0))
        ):
            return None
        try:
            Pubkey.from_string(signer)
        except ValueError:
            return None
        return checked_at_us, OriginEvidence(
            creation_signer=signer,
            confidence="high",
            signature=signature,
            slot=slot,
            block_time=block_time,
            current_observed_bps=0,
            history_complete=history_complete,
        )

    def save_origin_trace_cache(self, mint: str, origin: OriginEvidence | None) -> None:
        """Cache the creation signer; current holdings are always recalculated."""
        payload = (
            {
                "creation_signer": origin.creation_signer,
                "confidence": origin.confidence,
                "signature": origin.signature,
                "slot": origin.slot,
                "block_time": origin.block_time,
                "history_complete": origin.history_complete,
            }
            if origin is not None
            else None
        )
        self._connection.execute(
            """
            INSERT INTO token_origin_cache (mint, checked_at_us, origin_json)
            VALUES (?, ?, ?)
            ON CONFLICT(mint) DO UPDATE SET
                checked_at_us=excluded.checked_at_us,
                origin_json=excluded.origin_json
            """,
            (mint, time.time_ns() // 1_000, json.dumps(payload) if payload is not None else None),
        )

    def runtime_state(self, state_key: str) -> dict[str, object] | None:
        row = self._connection.execute(
            "SELECT updated_at, payload_json FROM runtime_state WHERE state_key = ?",
            (state_key,),
        ).fetchone()
        if row is None:
            return None
        try:
            parsed = json.loads(str(row[1]))
        except (TypeError, json.JSONDecodeError):
            return None
        if not isinstance(parsed, dict):
            return None
        return {"persisted_at": row[0], **parsed}

    def quick_check(self) -> bool:
        row = self._connection.execute("PRAGMA quick_check").fetchone()
        return bool(row and str(row[0]).lower() == "ok")

    def backup_to(self, destination: Path) -> None:
        resolved = destination.resolve()
        if resolved == self.path.resolve():
            raise JournalError("Backup destination must differ from the live database")
        resolved.parent.mkdir(parents=True, exist_ok=True)
        destination_connection = sqlite3.connect(resolved)
        try:
            self._connection.backup(destination_connection)
            check = destination_connection.execute("PRAGMA quick_check").fetchone()
            if not check or str(check[0]).lower() != "ok":
                raise JournalError("Backup database integrity check failed")
        finally:
            destination_connection.close()
