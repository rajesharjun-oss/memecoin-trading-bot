"""Bounded, secret-free paper data-feed measurements.

Only fixed metric and outcome labels reach SQLite. Network hooks add counters in
memory; a daemon flushes them every five minutes without delaying paper exits.
"""

from __future__ import annotations

import atexit
import logging
import re
import sqlite3
import threading
import time
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

LOGGER = logging.getLogger(__name__)
FLUSH_SECONDS = 300
RETENTION_SECONDS = 7 * 24 * 3_600
TARGET_REFRESH_SECONDS = 120  # Existing operations.py minimum 1m-candle capacity rule.
ENDPOINTS = ("ohlcv", "dexscreener", "jupiter_quote", "rpc")
HTTP_OUTCOMES = ("ok", "http_429", "http_5xx", "timeout", "transport_error", "http_4xx")
GATED_FAILURES = ("http_429", "http_5xx", "timeout", "transport_error")
LABELS = {
    **{endpoint: frozenset(HTTP_OUTCOMES) for endpoint in ENDPOINTS},
    "candles": frozenset(("fresh", "stale", "missing")),
    "exit_marks": frozenset(("fresh", "missed")),
    "mark_quotes": frozenset(("attempted", "succeeded")),
    "capacity": frozenset(
        ("ready_mint_seconds", "successful_calls", "cycles", "observed_cycle_seconds")
    ),
}
RPC_METHODS = frozenset(
    (
        "getSignaturesForAddress",
        "getTransaction",
        "getMultipleAccounts",
        "getTokenLargestAccounts",
        "getAccountInfo",
        "getTokenAccountsByOwner",
        "getBalance",
        "other",
    )
)
FOCUS_STATUSES = frozenset(("ready", "stale", "gap", "collecting", "invalid", "missing"))
FOCUS_FETCH_ACTIVITY = ("zero_5m", "one_to_seven_5m", "eight_plus_5m", "unknown")
FOCUS_FETCH_OUTCOMES = (
    "recent_bar",
    "late_bar",
    "empty",
    "request_error",
    "rate_limited",
    "not_attempted",
)
DIAGNOSTIC_LABELS = {
    "rpc_method": RPC_METHODS,
    "focus_candle": frozenset(("1m", "5m")),
    "focus_market": frozenset(("eligible", "ineligible")),
    "focus_schedule": frozenset(("selected", "scheduled", "cycles")),
}
DIAGNOSTIC_OUTCOMES = frozenset((*HTTP_OUTCOMES, *FOCUS_STATUSES, "count"))
_RPC_METHOD_PREFIX = re.compile(
    rb'^\s*\{\s*"jsonrpc"\s*:\s*"2\.0"\s*,\s*"id"\s*:\s*\d+\s*,\s*"method"\s*:\s*"([A-Za-z][A-Za-z0-9]{0,39})"'
)
_CREATE_TABLE = """
CREATE TABLE IF NOT EXISTS telemetry_hourly (
    hour_start INTEGER NOT NULL,
    metric TEXT NOT NULL CHECK (metric IN (
        'ohlcv', 'dexscreener', 'jupiter_quote', 'rpc',
        'candles', 'exit_marks', 'mark_quotes', 'capacity'
    )),
    outcome TEXT NOT NULL CHECK (outcome IN (
        'ok', 'http_429', 'http_5xx', 'timeout', 'transport_error', 'http_4xx',
        'fresh', 'stale', 'missing', 'missed', 'attempted',
        'succeeded', 'ready_mint_seconds', 'successful_calls',
        'cycles', 'observed_cycle_seconds'
    )),
    count INTEGER NOT NULL CHECK (count >= 0),
    PRIMARY KEY (hour_start, metric, outcome)
)
"""
_CREATE_DIAGNOSTICS_TABLE = """
CREATE TABLE IF NOT EXISTS telemetry_diagnostics_hourly (
    hour_start INTEGER NOT NULL,
    category TEXT NOT NULL CHECK (category IN (
        'rpc_method', 'focus_candle', 'focus_market', 'focus_schedule'
    )),
    label TEXT NOT NULL CHECK (label IN (
        'getSignaturesForAddress', 'getTransaction', 'getMultipleAccounts',
        'getTokenLargestAccounts', 'getAccountInfo', 'getTokenAccountsByOwner',
        'getBalance', 'other', '1m', '5m', 'eligible', 'ineligible',
        'selected', 'scheduled', 'cycles'
    )),
    outcome TEXT NOT NULL CHECK (outcome IN (
        'ok', 'http_429', 'http_5xx', 'timeout', 'transport_error', 'http_4xx',
        'ready', 'stale', 'gap', 'collecting', 'invalid', 'missing', 'count'
    )),
    count INTEGER NOT NULL CHECK (count >= 0),
    PRIMARY KEY (hour_start, category, label, outcome)
)
"""
_CREATE_FOCUS_FETCH_TABLE = """
CREATE TABLE IF NOT EXISTS telemetry_focus_fetch_hourly (
    hour_start INTEGER NOT NULL,
    activity TEXT NOT NULL CHECK (activity IN (
        'zero_5m', 'one_to_seven_5m', 'eight_plus_5m', 'unknown'
    )),
    outcome TEXT NOT NULL CHECK (outcome IN (
        'recent_bar', 'late_bar', 'empty', 'request_error', 'rate_limited',
        'not_attempted'
    )),
    count INTEGER NOT NULL CHECK (count >= 0),
    PRIMARY KEY (hour_start, activity, outcome)
)
"""


def _hour(epoch_seconds: int) -> int:
    return epoch_seconds // 3_600 * 3_600


def _stamp(epoch_seconds: int) -> str:
    return datetime.fromtimestamp(epoch_seconds, UTC).isoformat()


class HourlyTelemetry:
    """One process-local, bounded accumulator for the paper dashboard."""

    def __init__(self, state_path: Path) -> None:
        self.state_path = state_path
        self._pending: Counter[tuple[int, str, str]] = Counter()
        self._pending_diagnostics: Counter[tuple[int, str, str, str]] = Counter()
        self._pending_focus_fetch: Counter[tuple[int, str, str]] = Counter()
        self._lock = threading.Lock()
        self._flush_lock = threading.Lock()
        self._stop = threading.Event()
        self._last_market_sample_at: int | None = None

    def add(self, metric: str, outcome: str, count: int = 1, *, at: int | None = None) -> None:
        if outcome not in LABELS.get(metric, ()) or type(count) is not int or count < 0:
            raise ValueError("Invalid telemetry counter")
        if count == 0:
            return
        hour = _hour(int(time.time()) if at is None else at)
        with self._lock:
            self._pending[(hour, metric, outcome)] += count

    def add_diagnostic(
        self, category: str, label: str, outcome: str, count: int = 1, *, at: int | None = None
    ) -> None:
        allowed_outcomes = (
            HTTP_OUTCOMES
            if category == "rpc_method"
            else FOCUS_STATUSES
            if category == "focus_candle"
            else ("count",)
        )
        if (
            label not in DIAGNOSTIC_LABELS.get(category, ())
            or outcome not in allowed_outcomes
            or type(count) is not int
            or count < 0
        ):
            raise ValueError("Invalid diagnostic counter")
        if count == 0:
            return
        hour = _hour(int(time.time()) if at is None else at)
        with self._lock:
            self._pending_diagnostics[(hour, category, label, outcome)] += count

    def add_focus_fetch(self, activity: str, outcome: str, *, at: int | None = None) -> None:
        if activity not in FOCUS_FETCH_ACTIVITY or outcome not in FOCUS_FETCH_OUTCOMES:
            raise ValueError("Invalid focus fetch counter")
        hour = _hour(int(time.time()) if at is None else at)
        with self._lock:
            self._pending_focus_fetch[(hour, activity, outcome)] += 1

    def market_cycle(self, ready: int, achieved: int, interval_seconds: int) -> None:
        if min(ready, achieved, interval_seconds) < 0 or interval_seconds == 0:
            raise ValueError("Invalid market-cycle telemetry")
        now = int(time.time())
        hour = _hour(now)
        with self._lock:
            elapsed = (
                interval_seconds
                if self._last_market_sample_at is None
                else max(1, now - self._last_market_sample_at)
            )
            self._last_market_sample_at = now
            self._pending[(hour, "capacity", "ready_mint_seconds")] += ready * elapsed
            self._pending[(hour, "capacity", "successful_calls")] += achieved
            self._pending[(hour, "capacity", "cycles")] += 1
            self._pending[(hour, "capacity", "observed_cycle_seconds")] += min(
                elapsed, max(60, 2 * interval_seconds)
            )

    def flush(self) -> None:
        """Retry failed writes later; never raise into the trading worker."""
        if not self._flush_lock.acquire(blocking=False):
            return
        try:
            cutoff = _hour(int(time.time())) - RETENTION_SECONDS
            with self._lock:
                for key in list(self._pending):
                    if key[0] < cutoff:
                        del self._pending[key]
                batch = dict(self._pending)
                for diagnostic_key in list(self._pending_diagnostics):
                    if diagnostic_key[0] < cutoff:
                        del self._pending_diagnostics[diagnostic_key]
                diagnostics_batch = dict(self._pending_diagnostics)
                for focus_key in list(self._pending_focus_fetch):
                    if focus_key[0] < cutoff:
                        del self._pending_focus_fetch[focus_key]
                focus_batch = dict(self._pending_focus_fetch)
            connection = sqlite3.connect(self.state_path, timeout=0.2)
            try:
                with connection:
                    connection.execute(_CREATE_TABLE)
                    connection.execute(_CREATE_DIAGNOSTICS_TABLE)
                    connection.execute(_CREATE_FOCUS_FETCH_TABLE)
                    if batch:
                        connection.executemany(
                            "INSERT INTO telemetry_hourly (hour_start, metric, outcome, count) "
                            "VALUES (?, ?, ?, ?) ON CONFLICT(hour_start, metric, outcome) "
                            "DO UPDATE SET count = count + excluded.count",
                            (
                                (hour, metric, outcome, count)
                                for (hour, metric, outcome), count in batch.items()
                            ),
                        )
                    connection.execute(
                        "DELETE FROM telemetry_hourly WHERE hour_start < ?", (cutoff,)
                    )
                    if diagnostics_batch:
                        connection.executemany(
                            "INSERT INTO telemetry_diagnostics_hourly "
                            "(hour_start, category, label, outcome, count) "
                            "VALUES (?, ?, ?, ?, ?) "
                            "ON CONFLICT(hour_start, category, label, outcome) "
                            "DO UPDATE SET count = count + excluded.count",
                            (
                                (hour, category, label, outcome, count)
                                for (
                                    hour,
                                    category,
                                    label,
                                    outcome,
                                ), count in diagnostics_batch.items()
                            ),
                        )
                    connection.execute(
                        "DELETE FROM telemetry_diagnostics_hourly WHERE hour_start < ?", (cutoff,)
                    )
                    if focus_batch:
                        connection.executemany(
                            "INSERT INTO telemetry_focus_fetch_hourly "
                            "(hour_start, activity, outcome, count) VALUES (?, ?, ?, ?) "
                            "ON CONFLICT(hour_start, activity, outcome) "
                            "DO UPDATE SET count = count + excluded.count",
                            (
                                (hour, activity, outcome, count)
                                for (hour, activity, outcome), count in focus_batch.items()
                            ),
                        )
                    connection.execute(
                        "DELETE FROM telemetry_focus_fetch_hourly WHERE hour_start < ?", (cutoff,)
                    )
            finally:
                connection.close()
            with self._lock:
                for key, count in batch.items():
                    self._pending[key] -= count
                    if self._pending[key] <= 0:
                        del self._pending[key]
                for diagnostic_key, count in diagnostics_batch.items():
                    self._pending_diagnostics[diagnostic_key] -= count
                    if self._pending_diagnostics[diagnostic_key] <= 0:
                        del self._pending_diagnostics[diagnostic_key]
                for focus_key, count in focus_batch.items():
                    self._pending_focus_fetch[focus_key] -= count
                    if self._pending_focus_fetch[focus_key] <= 0:
                        del self._pending_focus_fetch[focus_key]
        except Exception as exc:
            LOGGER.warning("Telemetry flush failed (%s)", type(exc).__name__)
        finally:
            self._flush_lock.release()

    def start(self) -> None:
        def loop() -> None:
            while not self._stop.wait(FLUSH_SECONDS):
                self.flush()

        threading.Thread(target=loop, name="paper-telemetry", daemon=True).start()
        atexit.register(self.flush)


_recorder: HourlyTelemetry | None = None


def configure_paper_telemetry(state_path: Path) -> None:
    """Enable measurements only for a paper-only dashboard process."""
    global _recorder
    if _recorder is None:
        try:
            recorder = HourlyTelemetry(state_path)
            recorder.start()
            _recorder = recorder
        except Exception as exc:
            LOGGER.warning("Telemetry startup failed (%s)", type(exc).__name__)


def _safe_add(metric: str, outcome: str, count: int = 1) -> None:
    try:
        if _recorder is not None:
            _recorder.add(metric, outcome, count)
    except Exception as exc:
        LOGGER.warning("Telemetry counter failed (%s)", type(exc).__name__)


def _safe_add_diagnostic(category: str, label: str, outcome: str, count: int = 1) -> None:
    try:
        if _recorder is not None:
            _recorder.add_diagnostic(category, label, outcome, count)
    except Exception as exc:
        LOGGER.warning("Diagnostic counter failed (%s)", type(exc).__name__)


def record_focus_fetch(transactions_5m: int | None, outcome: str) -> None:
    """Record only a fixed activity bucket and outcome, never a mint or pool."""
    try:
        if _recorder is None:
            return
        activity = (
            "unknown"
            if type(transactions_5m) is not int or transactions_5m < 0
            else "zero_5m"
            if transactions_5m == 0
            else "one_to_seven_5m"
            if transactions_5m < 8
            else "eight_plus_5m"
        )
        _recorder.add_focus_fetch(activity, outcome)
    except Exception as exc:
        LOGGER.warning("Focus fetch telemetry failed (%s)", type(exc).__name__)


def _rpc_method(request: httpx.Request) -> str:
    # Read only the fixed JSON-RPC envelope prefix; never retain params or URL.
    try:
        match = _RPC_METHOD_PREFIX.match(request.content[:160])
    except httpx.RequestNotRead:
        return "other"
    if match is None:
        return "other"
    method = match.group(1).decode("ascii")
    return method if method in RPC_METHODS else "other"


def _outcome(status_code: int) -> str:
    if status_code == 429:
        return "http_429"
    if status_code >= 500:
        return "http_5xx"
    if 200 <= status_code < 300:
        return "ok"
    return "http_4xx"


class _MeasuredClient(httpx.Client):
    def __init__(self, endpoint: str, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._telemetry_endpoint = endpoint

    def send(self, request: httpx.Request, **kwargs: Any) -> httpx.Response:
        endpoint = self._telemetry_endpoint
        rpc_method = _rpc_method(request) if endpoint == "rpc" else None
        if endpoint == "jupiter_quote" and not (
            request.method == "GET"
            and request.url.path in ("/swap/v2/build", "/swap/v2/order", "/swap/v1/quote")
        ):
            endpoint = ""
        try:
            response = super().send(request, **kwargs)
        except httpx.TimeoutException:
            if endpoint:
                _safe_add(endpoint, "timeout")
                if rpc_method is not None:
                    _safe_add_diagnostic("rpc_method", rpc_method, "timeout")
            raise
        except httpx.RequestError:
            if endpoint:
                _safe_add(endpoint, "transport_error")
                if rpc_method is not None:
                    _safe_add_diagnostic("rpc_method", rpc_method, "transport_error")
            raise
        if endpoint:
            outcome = _outcome(response.status_code)
            _safe_add(endpoint, outcome)
            if rpc_method is not None:
                _safe_add_diagnostic("rpc_method", rpc_method, outcome)
        return response


def http_client(endpoint: str, **kwargs: Any) -> httpx.Client:
    """Preserve the ordinary client unless paper telemetry is configured."""
    if endpoint not in ENDPOINTS:
        raise ValueError("Unknown telemetry endpoint")
    if _recorder is None:
        return httpx.Client(**kwargs)
    return _MeasuredClient(endpoint, **kwargs)


def record_market_cycle(
    ready: object, succeeded: object, cache_hits: object, interval: int
) -> None:
    try:
        if _recorder is None:
            return
        if (
            type(ready) is not int
            or type(succeeded) is not int
            or type(cache_hits) is not int
            or min(ready, succeeded, cache_hits) < 0
        ):
            return
        if cache_hits > succeeded:
            return
        _recorder.market_cycle(ready, succeeded - cache_hits, interval)
    except Exception as exc:
        LOGGER.warning("Market telemetry failed (%s)", type(exc).__name__)


def record_focus_schedule(selected: object, scheduled: object) -> None:
    """Count selected and scheduled focus slots without storing mint addresses."""
    if type(selected) is not int or type(scheduled) is not int:
        return
    if not 0 <= scheduled <= selected <= 2:
        return
    _safe_add_diagnostic("focus_schedule", "cycles", "count")
    _safe_add_diagnostic("focus_schedule", "selected", "count", selected)
    _safe_add_diagnostic("focus_schedule", "scheduled", "count", scheduled)


_UNUSABLE_CANDLE_STATUSES = frozenset(
    (
        "stale",
        "gap",
        "revised_values",
        "mixed_provenance",
        "conflicting_prices",
        "unverified_values",
    )
)


def record_candidate_evaluations(
    evidence_rows: Sequence[Mapping[str, object]],
    *,
    focus_state_loader: Callable[[], Mapping[str, object] | None] | None = None,
) -> None:
    """Count every market-eligible evaluation; unverified candles never count as fresh."""
    try:
        if _recorder is None:
            return
        try:
            focus = focus_state_loader() if focus_state_loader is not None else None
        except Exception as exc:
            LOGGER.warning("Focus telemetry lookup failed (%s)", type(exc).__name__)
            focus = None
        selected_at = focus.get("selected_at") if isinstance(focus, Mapping) else None
        raw_mints = focus.get("mints") if isinstance(focus, Mapping) else None
        focused_mints = (
            frozenset(mint for mint in raw_mints if isinstance(mint, str))
            if type(selected_at) is int
            and 0 <= int(time.time()) - selected_at < 3_600
            and isinstance(raw_mints, list)
            else frozenset()
        )
        for evidence in evidence_rows:
            inputs = evidence.get("market_gate_inputs")
            mint = inputs.get("mint") if isinstance(inputs, dict) else None
            focused = isinstance(mint, str) and mint in focused_mints
            if focused:
                _safe_add_diagnostic(
                    "focus_market",
                    "eligible"
                    if evidence.get("market_gate_verdict") == "eligible"
                    else "ineligible",
                    "count",
                )
            if evidence.get("market_gate_verdict") != "eligible":
                continue
            raw_signals = evidence.get("candle_signals")
            if focused:
                for frame in ("1m", "5m"):
                    signal = raw_signals.get(frame) if isinstance(raw_signals, dict) else None
                    status = signal.get("status") if isinstance(signal, dict) else None
                    outcome = (
                        status
                        if isinstance(status, str) and status in FOCUS_STATUSES
                        else "invalid"
                        if status is not None
                        else "missing"
                    )
                    _safe_add_diagnostic("focus_candle", frame, outcome)
            if not isinstance(raw_signals, dict):
                _safe_add("candles", "missing")
                continue
            statuses = {
                frame: signal.get("status") if isinstance(signal, dict) else None
                for frame, signal in raw_signals.items()
                if isinstance(frame, str)
            }
            if any(statuses.get(frame) in (None, "collecting") for frame in ("1m", "5m")):
                _safe_add("candles", "missing")
            elif any(statuses.get(frame) != "ready" for frame in ("1m", "5m")) or any(
                statuses.get(frame) in _UNUSABLE_CANDLE_STATUSES for frame in ("15m", "1h")
            ):
                _safe_add("candles", "stale")
            else:
                _safe_add("candles", "fresh")
    except Exception as exc:
        LOGGER.warning("Candle telemetry failed (%s)", type(exc).__name__)


def record_mark_quote_attempt() -> None:
    _safe_add("mark_quotes", "attempted")


def record_mark_quote_success() -> None:
    _safe_add("mark_quotes", "succeeded")


def record_exit_marks(positions: Sequence[Mapping[str, object]]) -> None:
    """Count positions with a live mark accepted by the exit signal gate."""
    try:
        for position in positions:
            if position.get("status") != "open":
                continue
            _safe_add("exit_marks", "fresh" if position.get("quote_state") == "live" else "missed")
    except Exception as exc:
        LOGGER.warning("Exit-mark telemetry failed (%s)", type(exc).__name__)


def telemetry_report(state_path: Path, hours: int, *, now: int | None = None) -> dict[str, object]:
    """Read only complete UTC hours; never create a database or print raw records."""
    if type(hours) is not int or not 1 <= hours <= 48:
        raise ValueError("Telemetry report hours must be between 1 and 48")
    end = _hour(int(time.time()) if now is None else now)
    start = end - hours * 3_600
    counts: Counter[tuple[str, str]] = Counter()
    diagnostics: Counter[tuple[str, str, str]] = Counter()
    focus_fetch: Counter[tuple[str, str]] = Counter()
    diagnostic_hours: set[int] = set()
    diagnostics_available = False
    focus_fetch_available = False
    legacy_ignored: Counter[str] = Counter()
    hourly_counts: dict[int, Counter[tuple[str, str]]] = {}
    hours_seen: set[int] = set()
    try:
        with closing(
            sqlite3.connect(f"{state_path.resolve().as_uri()}?mode=ro", uri=True)
        ) as connection:
            rows = connection.execute(
                "SELECT hour_start, metric, outcome, count FROM telemetry_hourly "
                "WHERE hour_start >= ? AND hour_start < ?",
                (start, end),
            ).fetchall()
            try:
                diagnostic_rows = connection.execute(
                    "SELECT hour_start, category, label, outcome, count "
                    "FROM telemetry_diagnostics_hourly "
                    "WHERE hour_start >= ? AND hour_start < ?",
                    (start, end),
                ).fetchall()
                diagnostics_available = True
            except sqlite3.OperationalError:
                diagnostic_rows = []  # Existing journals predate this additive table.
            try:
                focus_rows = connection.execute(
                    "SELECT activity, outcome, count FROM telemetry_focus_fetch_hourly "
                    "WHERE hour_start >= ? AND hour_start < ?",
                    (start, end),
                ).fetchall()
                focus_fetch_available = True
            except sqlite3.OperationalError:
                focus_rows = []  # Existing journals predate this additive table.
    except (OSError, sqlite3.Error):
        rows = []
        diagnostic_rows = []
        focus_rows = []
    for activity, outcome, count in focus_rows:
        if (
            activity in FOCUS_FETCH_ACTIVITY
            and outcome in FOCUS_FETCH_OUTCOMES
            and type(count) is int
            and count >= 0
        ):
            focus_fetch[(activity, outcome)] += count
    for hour, category, label, outcome, count in diagnostic_rows:
        if (
            label in DIAGNOSTIC_LABELS.get(category, ())
            and outcome in DIAGNOSTIC_OUTCOMES
            and type(count) is int
            and count >= 0
        ):
            diagnostics[(category, label, outcome)] += count
            diagnostic_hours.add(hour)
    for hour, metric, outcome, count in rows:
        if metric in ENDPOINTS and outcome == "other_error" and type(count) is int and count >= 0:
            legacy_ignored[metric] += count
            continue
        if metric in LABELS and outcome in LABELS[metric] and type(count) is int and count >= 0:
            counts[(metric, outcome)] += count
            hourly_counts.setdefault(hour, Counter())[(metric, outcome)] += count
            if metric == "capacity" and outcome == "cycles" and count:
                hours_seen.add(hour)
    endpoint_results: dict[str, dict[str, object]] = {}
    gates: dict[str, str] = {}
    for endpoint in ENDPOINTS:
        outcomes = {outcome: counts[(endpoint, outcome)] for outcome in HTTP_OUTCOMES}
        attempts = sum(outcomes.values())
        failures = sum(outcomes[outcome] for outcome in GATED_FAILURES)
        failure_rate = round(failures * 100 / attempts, 3) if attempts else None
        endpoint_results[endpoint] = {
            **outcomes,
            "attempts": attempts,
            "failures": failures,
            "failure_rate_pct": failure_rate,
            "http_4xx_rate_pct": round(outcomes["http_4xx"] * 100 / attempts, 3)
            if attempts
            else None,
            "legacy_other_error_ignored": legacy_ignored[endpoint],
        }
        gates[f"{endpoint}_errors"] = (
            "undefined"
            if not attempts or legacy_ignored[endpoint]
            else "pass"
            if failures * 20 < attempts
            else "fail"
        )
    candle_counts = {outcome: counts[("candles", outcome)] for outcome in LABELS["candles"]}
    candle_total = sum(candle_counts.values())
    mark_counts = {outcome: counts[("exit_marks", outcome)] for outcome in LABELS["exit_marks"]}
    mark_total = sum(mark_counts.values())
    quote_attempts = counts[("mark_quotes", "attempted")]
    quote_successes = counts[("mark_quotes", "succeeded")]
    needed_mint_seconds = counts[("capacity", "ready_mint_seconds")]
    achieved = counts[("capacity", "successful_calls")]
    observed_seconds = counts[("capacity", "observed_cycle_seconds")]
    window_seconds = hours * 3_600
    complete = (
        hours >= 24 and len(hours_seen) == hours and observed_seconds >= 0.95 * window_seconds
    )
    gates["candidate_candles"] = (
        "pass"
        if candle_total and candle_counts["fresh"] * 20 >= 19 * candle_total
        else "fail"
        if candle_total
        else "undefined"
    )
    gates["exit_marks"] = (
        "pass"
        if mark_total and mark_counts["fresh"] * 20 >= 19 * mark_total
        else "fail"
        if mark_total
        else "undefined"
    )
    gates["mark_quote_success"] = (
        "pass"
        if quote_attempts and quote_successes * 20 >= 19 * quote_attempts
        else "fail"
        if quote_attempts
        else "undefined"
    )
    gates["ohlcv_capacity"] = (
        "pass"
        if needed_mint_seconds and achieved * TARGET_REFRESH_SECONDS >= needed_mint_seconds
        else "fail"
        if needed_mint_seconds
        else "undefined"
    )
    overall = (
        "insufficient_window"
        if not complete
        else "fail"
        if "fail" in gates.values()
        else "undefined"
        if "undefined" in gates.values()
        else "pass"
    )
    return {
        "window_start_utc": _stamp(start),
        "window_end_utc": _stamp(end),
        "hours": hours,
        "complete_hours_with_collector_cycles": len(hours_seen),
        "collector_observed_seconds": observed_seconds,
        "window_complete": complete,
        "endpoints": endpoint_results,
        "diagnostics": {
            "available": diagnostics_available,
            "observed_hours": len(diagnostic_hours),
            "focus_fetch_available": focus_fetch_available,
            "focus_fetch": {
                activity: {
                    outcome: focus_fetch[(activity, outcome)] for outcome in FOCUS_FETCH_OUTCOMES
                }
                for activity in FOCUS_FETCH_ACTIVITY
            },
            "rpc_methods": {
                method: {
                    **{
                        outcome: diagnostics[("rpc_method", method, outcome)]
                        for outcome in HTTP_OUTCOMES
                    },
                    "attempts": sum(
                        diagnostics[("rpc_method", method, outcome)] for outcome in HTTP_OUTCOMES
                    ),
                }
                for method in sorted(RPC_METHODS)
            },
            "focused_evaluations": {
                label: diagnostics[("focus_market", label, "count")]
                for label in ("eligible", "ineligible")
            },
            "focused_candles": {
                frame: {
                    outcome: diagnostics[("focus_candle", frame, outcome)]
                    for outcome in sorted(FOCUS_STATUSES)
                }
                for frame in ("1m", "5m")
            },
            "focus_schedule": {
                label: diagnostics[("focus_schedule", label, "count")]
                for label in ("cycles", "selected", "scheduled")
            },
        },
        "candidate_candles": {
            **candle_counts,
            "evaluations": candle_total,
            "skipped_missing_or_stale": candle_counts["missing"] + candle_counts["stale"],
            "fresh_pct": round(candle_counts["fresh"] * 100 / candle_total, 3)
            if candle_total
            else None,
        },
        "exit_marks": {
            **mark_counts,
            "observations": mark_total,
            "fresh_pct": round(mark_counts["fresh"] * 100 / mark_total, 3) if mark_total else None,
        },
        "mark_quotes": {
            "attempted": quote_attempts,
            "succeeded": quote_successes,
            "success_pct": round(quote_successes * 100 / quote_attempts, 3)
            if quote_attempts
            else None,
        },
        "ohlcv_capacity": {
            "candidate_mint_seconds": needed_mint_seconds,
            "needed_calls": round(needed_mint_seconds / TARGET_REFRESH_SECONDS, 3),
            "successful_network_calls": achieved,
            "needed_calls_per_minute": round(
                needed_mint_seconds / TARGET_REFRESH_SECONDS / (hours * 60), 3
            ),
            "successful_calls_per_minute": round(achieved / (hours * 60), 3),
            "collector_cycles": counts[("capacity", "cycles")],
            "average_needed_calls_per_cycle": round(
                needed_mint_seconds / TARGET_REFRESH_SECONDS / counts[("capacity", "cycles")], 3
            )
            if counts[("capacity", "cycles")]
            else None,
            "average_successful_calls_per_cycle": round(
                achieved / counts[("capacity", "cycles")], 3
            )
            if counts[("capacity", "cycles")]
            else None,
        },
        "hourly": [
            {
                "hour_start_utc": _stamp(hour),
                "collector_cycles": values[("capacity", "cycles")],
                "needed_ohlcv_calls": round(
                    values[("capacity", "ready_mint_seconds")] / TARGET_REFRESH_SECONDS, 3
                ),
                "successful_ohlcv_network_calls": values[("capacity", "successful_calls")],
            }
            for hour, values in sorted(hourly_counts.items())
        ],
        "gates": gates,
        "overall": overall,
        "notes": [
            "Only the last fully completed UTC hours are included.",
            "A candidate lacking audit, market, or candle evidence counts as missing, never fresh.",
            "HTTP ok means a 2xx response, not proof that returned market data was valid.",
            "Endpoint failure gates count 429, 5xx, timeout, and transport errors only; "
            "other HTTP statuses are reported as http_4xx and are ungated.",
            "High http_4xx may reflect thin markets or no routes, not a feed outage.",
            "Legacy other_error rows are excluded from attempts and failures; affected "
            "endpoint gates remain undefined because those outcomes cannot be reclassified.",
            "OHLCV demand uses the existing 120-second market-ready mint minimum; "
            "research and higher-timeframe calls add demand. Passing is necessary, not sufficient; "
            "failing means measured capacity is below even this minimum.",
            "No open-position marks or endpoint requests makes that gate undefined, not passed.",
            "Diagnostic counters start at deployment, are aggregate only, and do not alter gates.",
            "Focus fetch activity is a contemporaneous DEX 5m transaction count; it cannot "
            "prove that a trade occurred in each missing 1m slot. Recent/late compares the "
            "latest returned 1m bar with the existing 180-second entry freshness boundary.",
        ],
    }
