"""Bounded, secret-free paper data-feed measurements.

Only fixed metric and outcome labels reach SQLite. Network hooks add counters in
memory; a daemon flushes them every five minutes without delaying paper exits.
"""

from __future__ import annotations

import atexit
import logging
import sqlite3
import threading
import time
from collections import Counter
from collections.abc import Mapping, Sequence
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


def _hour(epoch_seconds: int) -> int:
    return epoch_seconds // 3_600 * 3_600


def _stamp(epoch_seconds: int) -> str:
    return datetime.fromtimestamp(epoch_seconds, UTC).isoformat()


class HourlyTelemetry:
    """One process-local, bounded accumulator for the paper dashboard."""

    def __init__(self, state_path: Path) -> None:
        self.state_path = state_path
        self._pending: Counter[tuple[int, str, str]] = Counter()
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
            connection = sqlite3.connect(self.state_path, timeout=0.2)
            try:
                with connection:
                    connection.execute(_CREATE_TABLE)
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
            finally:
                connection.close()
            with self._lock:
                for key, count in batch.items():
                    self._pending[key] -= count
                    if self._pending[key] <= 0:
                        del self._pending[key]
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
            raise
        except httpx.RequestError:
            if endpoint:
                _safe_add(endpoint, "transport_error")
            raise
        if endpoint:
            _safe_add(endpoint, _outcome(response.status_code))
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


def record_candidate_evaluations(evidence_rows: Sequence[Mapping[str, object]]) -> None:
    """Count every market-eligible evaluation; unverified candles never count as fresh."""
    try:
        for evidence in evidence_rows:
            if evidence.get("market_gate_verdict") != "eligible":
                continue
            raw_signals = evidence.get("candle_signals")
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
    except (OSError, sqlite3.Error):
        rows = []
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
        ],
    }
