"""Create an isolated forward paper run without altering the prior journal."""

from __future__ import annotations

import uuid
from collections import defaultdict
from datetime import UTC, datetime
from itertools import pairwise
from pathlib import Path

from .forward_performance import _costed_execution
from .journal import TradeJournal
from .worker_lock import WorkerAlreadyRunningError, exclusive_worker_lock


class PaperCampaignError(RuntimeError):
    """Raised when a clean paper run cannot be created safely."""


# Copy observations that existed before this run for candle warmup and audit
# chronology. Prior positions, fills, decisions, calls, worker state, and saved
# backtests belong to the archived run and must not contaminate new performance.
RESEARCH_TABLES = (
    "discovery_candidates",
    "audit_observations",
    "market_snapshots",
    "pool_candles",
    "narrative_items",
    "narrative_signals",
    "narrative_signal_observations",
)


def interrupt_forward_paper_run(source_path: Path) -> dict[str, object]:
    """Archive an unmonitored campaign without fabricating position exits."""
    source = source_path.resolve()
    if not source.is_file():
        raise PaperCampaignError("The source journal does not exist")
    try:
        with exclusive_worker_lock(source):
            return _interrupt_forward_paper_run_locked(source)
    except WorkerAlreadyRunningError as exc:
        raise PaperCampaignError("Stop the paper dashboard before archiving its run") from exc


def _interrupt_forward_paper_run_locked(
    source: Path,
    *,
    reason: str = "paper_exit_monitor_offline",
    gap_started_at_override: str | None = None,
) -> dict[str, object]:
    with TradeJournal(source) as journal:
        connection = journal._connection
        connection.execute("BEGIN IMMEDIATE")
        try:
            campaign = journal.runtime_state("paper_campaign") or {}
            if campaign.get("mode") != "forward_paper_only":
                raise PaperCampaignError("Only a forward paper campaign can be interrupted")
            prior = campaign.get("interruption")
            if campaign.get("status") == "interrupted" and isinstance(prior, dict):
                connection.rollback()
                return prior
            last_exit = connection.execute(
                "SELECT completed_at FROM worker_cycle_history "
                "WHERE worker_key = 'paper_exit_monitor' AND outcome = 'completed' "
                "ORDER BY completed_at DESC LIMIT 1"
            ).fetchone()
            gap_started_at = gap_started_at_override or (
                str(last_exit[0]) if last_exit else str(campaign.get("started_at"))
            )
            if not gap_started_at or gap_started_at == "None":
                raise PaperCampaignError("Campaign monitoring start is missing")
            open_rows = connection.execute(
                "SELECT position_id, mint, last_quoted_at FROM paper_positions "
                "WHERE status = 'open' ORDER BY opened_at, position_id"
            ).fetchall()
            interrupted_at = datetime.now(UTC).isoformat()
            interruption: dict[str, object] = {
                "reason": reason,
                "gap_started_at": gap_started_at,
                "recorded_at": interrupted_at,
                "interrupted_open_positions": [
                    {"position_id": row[0], "mint": row[1], "last_quoted_at": row[2]}
                    for row in open_rows
                ],
            }
            journal.set_runtime_state(
                "paper_campaign",
                {**campaign, "status": "interrupted", "interruption": interruption},
            )
            journal.record_automation_event(
                event_type="paper_campaign_interrupted",
                level="warning",
                message="Archived paper campaign after an exit-monitoring gap",
                metadata={
                    "gap_started_at": gap_started_at,
                    "recorded_at": interrupted_at,
                    "open_position_count": len(open_rows),
                },
            )
            for position_id, mint, last_quoted_at in open_rows:
                journal.record_automation_event(
                    event_type="paper_position_interrupted",
                    level="warning",
                    message="Paper position was unmonitored; no exit fill was simulated",
                    metadata={
                        "position_id": position_id,
                        "mint": mint,
                        "last_quoted_at": last_quoted_at,
                        "gap_started_at": gap_started_at,
                    },
                )
            connection.commit()
            return interruption
        except Exception:
            connection.rollback()
            raise


def quarantine_unmonitored_forward_paper_run_locked(
    source: Path, *, maximum_gap_seconds: int, now: datetime | None = None
) -> dict[str, object] | None:
    """Fail closed before workers restart if an open position lost exit coverage.

    The caller must hold the journal's exclusive worker lock. A completed exit
    cycle alone is insufficient: every open position also needs a recent quote.
    """
    if maximum_gap_seconds <= 0:
        raise PaperCampaignError("The maximum monitoring gap must be positive")
    current = now or datetime.now(UTC)
    if current.tzinfo is None:
        raise PaperCampaignError("Monitoring time must include a time zone")
    with TradeJournal(source, readonly=True) as journal:
        campaign = journal.runtime_state("paper_campaign") or {}
        if campaign.get("mode") != "forward_paper_only":
            return None
        if campaign.get("status") == "interrupted":
            prior = campaign.get("interruption")
            return prior if isinstance(prior, dict) else {"reason": "paper_campaign_interrupted"}
        if campaign.get("status") not in (None, "active"):
            raise PaperCampaignError("Forward paper campaign status is invalid")
        open_rows = journal._connection.execute(
            "SELECT opened_at, last_quoted_at FROM paper_positions WHERE status = 'open'"
        ).fetchall()
        if not open_rows:
            return None
        row = journal._connection.execute(
            "SELECT completed_at FROM worker_cycle_history "
            "WHERE worker_key = 'paper_exit_monitor' AND outcome = 'completed' "
            "ORDER BY completed_at DESC LIMIT 1"
        ).fetchone()

        def age_seconds(raw: object) -> float | None:
            try:
                stamp = datetime.fromisoformat(str(raw))
            except (TypeError, ValueError):
                return None
            return (current - stamp).total_seconds() if stamp.tzinfo else None

        monitor_stamp = row[0] if row else None
        monitor_age = age_seconds(monitor_stamp)
        if monitor_age is None or not -30 <= monitor_age <= maximum_gap_seconds:
            reason = "paper_exit_monitor_offline"
            gap_start = str(monitor_stamp or campaign.get("started_at") or current.isoformat())
        else:
            stale_rows = [
                (opened_at, quoted_at)
                for opened_at, quoted_at in open_rows
                if (age := age_seconds(quoted_at or opened_at)) is None
                or not -30 <= age <= maximum_gap_seconds
            ]
            if not stale_rows:
                return None
            reason = "paper_position_quote_stale"
            gap_start = str(stale_rows[0][1] or stale_rows[0][0])
    return _interrupt_forward_paper_run_locked(
        source, reason=reason, gap_started_at_override=gap_start
    )


def archive_unmonitored_forward_paper_run(
    source_path: Path, *, maximum_mark_gap_seconds: int
) -> dict[str, object]:
    """Archive a closed-position campaign whose costed exit marks have a gap."""
    if maximum_mark_gap_seconds <= 0:
        raise PaperCampaignError("The maximum mark gap must be positive")
    source = source_path.resolve()
    if not source.is_file():
        raise PaperCampaignError("The source journal does not exist")
    try:
        with exclusive_worker_lock(source), TradeJournal(source) as journal:
            connection = journal._connection
            connection.execute("BEGIN IMMEDIATE")
            try:
                campaign = journal.runtime_state("paper_campaign") or {}
                if campaign.get("mode") != "forward_paper_only":
                    raise PaperCampaignError("Only a forward paper campaign can be archived")
                prior = campaign.get("interruption")
                if campaign.get("status") == "interrupted" and isinstance(prior, dict):
                    connection.rollback()
                    return prior
                open_count = connection.execute(
                    "SELECT COUNT(*) FROM paper_positions WHERE status = 'open'"
                ).fetchone()[0]
                if open_count:
                    raise PaperCampaignError("Interrupt open paper positions first")

                costed_marks: dict[str, list[str]] = defaultdict(list)
                for position_id, observed_at, value, execution in connection.execute(
                    "SELECT position_id, observed_at, value_lamports, execution_json "
                    "FROM paper_position_marks ORDER BY observed_at, rowid"
                ):
                    if _costed_execution(
                        execution, side="mark", amount_key="cash_credit_lamports", amount=value
                    ):
                        costed_marks[str(position_id)].append(str(observed_at))

                affected: list[dict[str, object]] = []
                for position_id, mint, opened_at, closed_at in connection.execute(
                    "SELECT position_id, mint, opened_at, closed_at FROM paper_positions "
                    "WHERE status = 'closed' ORDER BY opened_at, position_id"
                ):
                    boundaries = [str(opened_at), *costed_marks[str(position_id)], str(closed_at)]
                    try:
                        instants = [datetime.fromisoformat(stamp) for stamp in boundaries]
                        if any(instant.tzinfo is None for instant in instants):
                            raise ValueError("Missing time zone")
                        gaps = [
                            (
                                (end - start).total_seconds(),
                                boundaries[index],
                                boundaries[index + 1],
                            )
                            for index, (start, end) in enumerate(pairwise(instants))
                        ]
                    except (TypeError, ValueError) as exc:
                        raise PaperCampaignError("Position mark timestamps are invalid") from exc
                    if not gaps or any(seconds < 0 for seconds, _, _ in gaps):
                        raise PaperCampaignError("Position mark chronology is invalid")
                    longest_seconds, gap_start, gap_end = max(gaps)
                    if longest_seconds > maximum_mark_gap_seconds:
                        affected.append(
                            {
                                "position_id": str(position_id),
                                "mint": str(mint),
                                "gap_started_at": gap_start,
                                "gap_ended_at": gap_end,
                                "gap_seconds": round(longest_seconds),
                            }
                        )
                if not affected:
                    raise PaperCampaignError("No unmonitored closed paper positions were found")

                interruption: dict[str, object] = {
                    "reason": "unmonitored_closed_position_path",
                    "recorded_at": datetime.now(UTC).isoformat(),
                    "maximum_mark_gap_seconds": maximum_mark_gap_seconds,
                    "affected_closed_positions": affected,
                }
                journal.set_runtime_state(
                    "paper_campaign",
                    {**campaign, "status": "interrupted", "interruption": interruption},
                )
                journal.record_automation_event(
                    event_type="paper_campaign_interrupted",
                    level="warning",
                    message="Archived paper campaign with unmonitored closed positions",
                    metadata={
                        "recorded_at": interruption["recorded_at"],
                        "affected_closed_positions": len(affected),
                        "maximum_mark_gap_seconds": maximum_mark_gap_seconds,
                    },
                )
                connection.commit()
                return interruption
            except Exception:
                connection.rollback()
                raise
    except WorkerAlreadyRunningError as exc:
        raise PaperCampaignError("Stop the paper dashboard before archiving its run") from exc


def create_forward_paper_run(source_path: Path, target_path: Path) -> dict[str, object]:
    """Copy a consistent research snapshot into a new, empty paper journal."""
    source = source_path.resolve()
    target = target_path.resolve()
    if not source.is_file():
        raise PaperCampaignError("The source journal does not exist")
    if target == source:
        raise PaperCampaignError("The new run must use a different journal path")
    if target.exists():
        raise PaperCampaignError("The new run path already exists")
    if not target.parent.is_dir():
        raise PaperCampaignError("Create the destination folder before starting a new run")

    with TradeJournal(source, readonly=True) as previous:
        source_campaign = previous.runtime_state("paper_campaign") or {}
        if source_campaign.get("mode") == "forward_paper_only":
            open_count = previous._connection.execute(
                "SELECT COUNT(*) FROM paper_positions WHERE status = 'open'"
            ).fetchone()[0]
            if open_count and source_campaign.get("status") != "interrupted":
                raise PaperCampaignError(
                    "Interrupt open positions in the previous forward campaign first"
                )

    started_at = datetime.now(UTC).isoformat()
    run_id = str(uuid.uuid4())
    counts: dict[str, int] = {}
    try:
        with TradeJournal(source, readonly=True) as previous, TradeJournal(target) as fresh:
            previous._connection.execute("BEGIN")
            if previous._connection.execute("PRAGMA quick_check").fetchone() != ("ok",):
                raise PaperCampaignError("The source journal failed SQLite integrity checks")
            fresh._connection.execute("BEGIN IMMEDIATE")
            try:
                source_campaign = previous.runtime_state("paper_campaign") or {}
                if source_campaign.get("mode") == "forward_paper_only":
                    open_count = previous._connection.execute(
                        "SELECT COUNT(*) FROM paper_positions WHERE status = 'open'"
                    ).fetchone()[0]
                    if open_count and source_campaign.get("status") != "interrupted":
                        raise PaperCampaignError(
                            "Interrupt open positions in the previous forward campaign first"
                        )
                for table in RESEARCH_TABLES:
                    source_info = list(
                        previous._connection.execute(f'PRAGMA table_info("{table}")')
                    )
                    target_info = list(fresh._connection.execute(f'PRAGMA table_info("{table}")'))
                    columns = [row[1] for row in source_info]
                    source_types = {row[1]: row[2] for row in source_info}
                    target_types = {row[1]: row[2] for row in target_info}
                    if not columns or source_types != target_types:
                        raise PaperCampaignError(f"Research table schema differs: {table}")
                    placeholders = ",".join("?" for _ in columns)
                    column_list = ",".join(f'"{column}"' for column in columns)
                    # Identifiers come only from the fixed RESEARCH_TABLES tuple.
                    cursor = previous._connection.execute(
                        f'SELECT {column_list} FROM "{table}"'  # noqa: S608
                    )
                    copied = 0
                    while batch := cursor.fetchmany(1_000):
                        fresh._connection.executemany(
                            f'INSERT INTO "{table}" ({column_list}) VALUES ({placeholders})',  # noqa: S608
                            batch,
                        )
                        copied += len(batch)
                    counts[table] = copied
                metadata: dict[str, object] = {
                    "run_id": run_id,
                    "started_at": started_at,
                    "source_state_file": source.name,
                    "mode": "forward_paper_only",
                    "status": "active",
                    "source_campaign_status": source_campaign.get("status"),
                    "source_campaign_run_id": source_campaign.get("run_id"),
                    "research_rows_copied": counts,
                }
                fresh.set_runtime_state("paper_campaign", metadata)
                fresh._connection.commit()
            except Exception:
                fresh._connection.rollback()
                raise
            finally:
                previous._connection.rollback()
            if fresh._connection.execute("PRAGMA quick_check").fetchone() != ("ok",):
                raise PaperCampaignError("The new journal failed SQLite integrity checks")
            for check_sql in (
                "SELECT COUNT(*) FROM paper_positions",
                "SELECT COUNT(*) FROM paper_fills",
                "SELECT COUNT(*) FROM paper_position_marks",
                "SELECT COUNT(*) FROM strategy_decisions",
            ):
                if fresh._connection.execute(check_sql).fetchone()[0]:
                    raise PaperCampaignError("The new paper journal is not empty")
    except PaperCampaignError:
        raise
    except Exception as exc:
        raise PaperCampaignError(
            f"Could not create the new paper run ({type(exc).__name__})"
        ) from exc
    return {
        "run_id": run_id,
        "started_at": started_at,
        "path": str(target),
        "research_rows_copied": counts,
    }
