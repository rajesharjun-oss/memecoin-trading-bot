from __future__ import annotations

import argparse
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from memecoin_bot.cli import _cmd_research_backfill
from memecoin_bot.journal import TradeJournal
from memecoin_bot.ohlcv import GeckoTerminalOhlcvClient, PoolCandle
from memecoin_bot.research_history import backfill_research_candles

MINT = "DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263"
POOL = "HJa61kEfSAwRt3DW9p15mLh6U5MaQgZGbE51ngg7bYtT"
START = 1_700_000_040


class NeverFetchSource:
    def candles(self, *args: object, **kwargs: object) -> list[PoolCandle]:
        raise AssertionError("Invalid backfill must not contact the provider")


def test_backfill_paginates_real_pool_bars_without_retroactive_entry_provenance(
    tmp_path: Path,
) -> None:
    requests: list[int] = []

    def respond(request: httpx.Request) -> httpx.Response:
        cursor = int(request.url.params["before_timestamp"])
        requests.append(cursor)
        rows = [
            [START + step * 60, "1", "1.2", "0.9", "1.1", "100"]
            for step in reversed(range(6))
            if START + step * 60 < cursor
        ][: int(request.url.params["limit"])]
        return httpx.Response(
            200,
            json={
                "data": {"attributes": {"ohlcv_list": rows}},
                "meta": {"base": {"address": MINT}, "quote": {"address": "SOL"}},
            },
        )

    with (
        TradeJournal(tmp_path / "research.sqlite3") as journal,
        httpx.Client(
            base_url="https://api.geckoterminal.com", transport=httpx.MockTransport(respond)
        ) as http,
        GeckoTerminalOhlcvClient(client=http) as source,
    ):
        result = backfill_research_candles(
            journal,
            source,
            mint=MINT,
            pool=POOL,
            start=START,
            end=START + 6 * 60,
            limit=3,
            max_requests=3,
        )
        assert result.requests == 2
        assert result.new_candles == 6
        assert requests == [START + 6 * 60, START + 3 * 60]
        stored = journal.pool_candles(MINT, POOL, "1m")
        assert [row["opened_at"] for row in stored] == [START + step * 60 for step in range(6)]
        assert all(int(str(row["first_fetched_at"])) > START + 6 * 60 for row in stored)
        assert all(int(str(row["first_observed_values_at"])) > START + 6 * 60 for row in stored)
        repeat = backfill_research_candles(
            journal,
            source,
            mint=MINT,
            pool=POOL,
            start=START,
            end=START + 6 * 60,
            limit=3,
        )
        assert repeat.new_candles == 0


def test_backfill_rejects_unbounded_ranges_before_fetch(tmp_path: Path) -> None:
    with (
        TradeJournal(tmp_path / "research.sqlite3") as journal,
        pytest.raises(ValueError, match="Research backfill range"),
    ):
        backfill_research_candles(
            journal,
            NeverFetchSource(),
            mint=MINT,
            pool=POOL,
            start=START,
            end=START + 86_401,
        )


def test_cli_backfill_rejects_unsaved_research_pair_before_network(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "memecoin_bot.cli._settings",
        lambda _: SimpleNamespace(
            state_path=tmp_path / "research.sqlite3", paper_max_hold_seconds=86_400
        ),
    )
    args = argparse.Namespace(
        env_file=None,
        mint=MINT,
        pool=POOL,
        start=START,
        end=START + 60,
        limit=3,
        max_requests=1,
    )
    with pytest.raises(ValueError, match="saved research candidate"):
        _cmd_research_backfill(args)


def test_cli_demo_backfill_requires_key_before_network(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "memecoin_bot.cli._settings",
        lambda _: SimpleNamespace(state_path=tmp_path / "research.sqlite3"),
    )
    args = argparse.Namespace(
        env_file=None,
        provider="coingecko-demo",
        mint=MINT,
        pool=POOL,
        start=START,
        end=START + 60,
        limit=3,
        max_requests=1,
    )
    with pytest.raises(ValueError, match="COINGECKO_DEMO_API_KEY"):
        _cmd_research_backfill(args)
    assert not (tmp_path / "research.sqlite3").exists()
