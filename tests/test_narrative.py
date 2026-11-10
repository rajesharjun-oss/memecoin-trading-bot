from __future__ import annotations

import sqlite3
import time
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
from solders.pubkey import Pubkey

from memecoin_bot.journal import TradeJournal
from memecoin_bot.narrative import (
    BlueskySearchClient,
    GdeltNewsClient,
    GoogleNewsRssClient,
    NarrativeError,
    NarrativeItem,
    build_narrative_signals,
    deduplicate_items,
    normalize_bluesky_posts,
    normalize_gdelt_articles,
    refresh_narrative_intelligence,
)


def test_gdelt_client_uses_fixed_bounded_article_query() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/v2/doc/doc"
        assert request.url.params["mode"] == "artlist"
        assert request.url.params["timespan"] == "24h"
        assert request.url.params["maxrecords"] == "5"
        return httpx.Response(200, json={"articles": [{"title": "Solana meme coin"}]})

    client = GdeltNewsClient()
    client._client.close()
    client._client = httpx.Client(
        base_url=GdeltNewsClient.BASE_URL,
        transport=httpx.MockTransport(handler),
    )
    with client:
        assert client.recent(limit=5) == [{"title": "Solana meme coin"}]


def test_provider_timeout_is_wrapped_as_degraded_source_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("provider timed out", request=request)

    client = GdeltNewsClient()
    client._client.close()
    client._client = httpx.Client(
        base_url=GdeltNewsClient.BASE_URL,
        transport=httpx.MockTransport(handler),
    )
    with client, pytest.raises(NarrativeError, match="timed out"):
        client.recent(limit=5)


def test_provider_http_status_is_reported_without_response_body() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(429, text="sensitive upstream diagnostic")

    client = GdeltNewsClient()
    client._client.close()
    client._client = httpx.Client(
        base_url=GdeltNewsClient.BASE_URL,
        transport=httpx.MockTransport(handler),
    )
    with client, pytest.raises(NarrativeError, match=r"GDELT returned HTTP 429") as raised:
        client.recent(limit=5)

    assert "sensitive upstream diagnostic" not in str(raised.value)


def test_google_news_rss_is_parsed_with_bounded_safe_xml() -> None:
    xml = b"""<?xml version="1.0" encoding="UTF-8"?>
    <rss version="2.0"><channel><item>
      <title>Solana memecoin activity</title>
      <link>https://news.google.com/rss/articles/example</link>
      <pubDate>Sun, 02 Nov 2026 17:55:00 GMT</pubDate>
      <source url="https://www.coindesk.com">CoinDesk</source>
    </item></channel></rss>"""

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/rss/search"
        assert request.url.params["ceid"] == "US:en"
        return httpx.Response(200, content=xml, headers={"Content-Type": "application/xml"})

    client = GoogleNewsRssClient()
    client._client.close()
    client._client = httpx.Client(
        base_url=GoogleNewsRssClient.BASE_URL,
        transport=httpx.MockTransport(handler),
    )
    with client:
        articles = client.recent(limit=5)

    assert articles == [
        {
            "title": "Solana memecoin activity",
            "url": "https://news.google.com/rss/articles/example",
            "seendate": "Sun, 02 Nov 2026 17:55:00 GMT",
            "source_name": "CoinDesk",
            "source_url": "https://www.coindesk.com",
        }
    ]


def test_google_news_rejects_xml_entities() -> None:
    xml = (
        b'<?xml version="1.0"?><!DOCTYPE x [<!ENTITY boom "unsafe">]>'
        b"<rss><channel><item><title>&boom;</title></item></channel></rss>"
    )

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=xml)

    client = GoogleNewsRssClient()
    client._client.close()
    client._client = httpx.Client(
        base_url=GoogleNewsRssClient.BASE_URL,
        transport=httpx.MockTransport(handler),
    )
    with client, pytest.raises(NarrativeError, match="unsafe or malformed XML"):
        client.recent(limit=5)


def test_bluesky_client_deduplicates_posts_across_fixed_queries() -> None:
    assert BlueskySearchClient.BASE_URL == "https://api.bsky.app"
    post = {"uri": "at://did:plc:test/app.bsky.feed.post/abc", "record": {"text": "hi"}}

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/xrpc/app.bsky.feed.searchPosts"
        assert request.url.params["sort"] == "latest"
        assert request.url.params["q"] in BlueskySearchClient.QUERIES
        return httpx.Response(200, json={"posts": [post]})

    client = BlueskySearchClient()
    client._client.close()
    client._client = httpx.Client(
        base_url=BlueskySearchClient.BASE_URL,
        transport=httpx.MockTransport(handler),
    )
    with client:
        assert client.recent(limit=10) == [post]


def test_narrative_signal_distinguishes_contract_evidence_and_source_diversity() -> None:
    mint = str(Pubkey.new_unique())
    now = datetime(2026, 11, 2, 18, 0, tzinfo=UTC)
    candidates: list[dict[str, object]] = [{"mint": mint, "symbol": "RADAR", "name": "Radar Coin"}]
    news = normalize_gdelt_articles(
        [
            {
                "title": f"Radar Coin activity grows on Solana {mint}",
                "url": "https://coindesk.com/markets/radar",
                "domain": "coindesk.com",
                "seendate": "20261102T175000Z",
            }
        ],
        candidates,
    )
    social = normalize_bluesky_posts(
        [
            {
                "uri": "at://did:plc:author/app.bsky.feed.post/abc123",
                "author": {"handle": "analyst.example"},
                "record": {
                    "text": f"Watching $RADAR contract {mint}",
                    "createdAt": "2026-11-02T17:55:00Z",
                },
                "likeCount": 80,
                "repostCount": 20,
                "replyCount": 5,
                "quoteCount": 2,
            }
        ],
        candidates,
    )

    signal = build_narrative_signals([*news, *social], candidates, now=now)[0]

    assert signal.mint == mint
    assert signal.latest_mention_at == "2026-11-02T17:55:00+00:00"
    assert signal.confidence == "high"
    assert signal.unique_sources == 2
    assert signal.news_count == 1
    assert signal.social_count == 1
    assert signal.explicit_contract_mentions == 2
    assert signal.score >= 65
    assert "social_only_evidence" not in signal.risk_flags


def test_social_promotion_is_flagged_and_cannot_create_high_confidence() -> None:
    mint = str(Pubkey.new_unique())
    candidates: list[dict[str, object]] = [{"mint": mint, "symbol": "RADAR", "name": "Radar Coin"}]
    items = normalize_bluesky_posts(
        [
            {
                "uri": "at://did:plc:promoter/app.bsky.feed.post/promo1",
                "author": {"handle": "promoter.example"},
                "record": {
                    "text": "$RADAR GEM ALERT guaranteed 100x, buy now!",
                    "createdAt": "2026-11-02T17:58:00Z",
                },
                "likeCount": 5_000,
            }
        ],
        candidates,
    )
    signal = build_narrative_signals(
        items,
        candidates,
        now=datetime(2026, 11, 2, 18, 0, tzinfo=UTC),
    )[0]

    assert signal.confidence == "low"
    assert "social_only_evidence" in signal.risk_flags
    assert "promotion_dominant" in signal.risk_flags
    assert "weak_symbol_or_name_linkage" in signal.risk_flags
    assert signal.state != "surging"


def test_ticker_only_mentions_remain_low_confidence_even_across_publishers() -> None:
    mint = str(Pubkey.new_unique())
    now = datetime(2026, 11, 2, 18, 0, tzinfo=UTC)
    candidates: list[dict[str, object]] = [{"mint": mint, "symbol": "RADAR", "name": "Radar Coin"}]
    items = normalize_gdelt_articles(
        [
            {
                "title": "Radar Coin activity grows",
                "url": f"https://news{index}.example/article",
                "domain": f"news{index}.example",
                "seendate": "20261102T175000Z",
            }
            for index in range(3)
        ],
        candidates,
    )
    signal = build_narrative_signals(items, candidates, now=now)[0]

    assert signal.unique_sources == 3
    assert signal.explicit_contract_mentions == 0
    assert signal.confidence == "low"
    assert signal.score <= 39
    assert signal.state == "quiet"
    assert "weak_symbol_or_name_linkage" in signal.risk_flags


def test_invalid_future_and_stale_publication_times_cannot_create_signal() -> None:
    mint = str(Pubkey.new_unique())
    now = datetime(2026, 11, 2, 18, 0, tzinfo=UTC)
    candidates: list[dict[str, object]] = [{"mint": mint, "symbol": "RADAR"}]
    assert (
        normalize_gdelt_articles(
            [{"title": f"{mint} launch", "url": "https://news.example/invalid"}], candidates
        )
        == []
    )
    current = normalize_gdelt_articles(
        [
            {
                "title": f"{mint} launch",
                "url": "https://news.example/current",
                "seendate": "20261102T175000Z",
            }
        ],
        candidates,
    )[0]
    invalid = replace(current, item_id="invalid", published_at="not-a-date")
    future = replace(current, item_id="future", published_at="2026-11-03T18:00:00+00:00")
    stale = replace(current, item_id="stale", published_at="2026-10-31T18:00:00+00:00")

    assert build_narrative_signals([invalid, future, stale], candidates, now=now) == []
    assert (
        build_narrative_signals([invalid, future, stale, current], candidates, now=now)[
            0
        ].mention_count
        == 1
    )


def test_exact_duplicate_content_is_removed() -> None:
    published = (datetime.now(UTC) - timedelta(minutes=2)).isoformat()
    first = NarrativeItem(
        item_id="a" * 64,
        published_at=published,
        source="bluesky",
        source_kind="social",
        source_name="one.example",
        source_url=None,
        title="Same call",
        excerpt="Same call https://example.com/one",
        engagement=10,
        evidence_tier="unverified_social",
        stance="neutral",
        tags=("callout",),
        risk_flags=(),
        matched_mints=(),
        explicit_mints=(),
    )
    second = NarrativeItem(
        **{
            **first.as_dict(),
            "item_id": "b" * 64,
            "source_name": "two.example",
            "source_url": "https://example.com/two",
            "tags": first.tags,
            "risk_flags": first.risk_flags,
            "matched_mints": first.matched_mints,
            "explicit_mints": first.explicit_mints,
        }
    )

    assert deduplicate_items([first, second]) == [first]


def test_journal_persists_narrative_evidence_and_signal(tmp_path: Path) -> None:
    mint = str(Pubkey.new_unique())
    item = NarrativeItem(
        item_id="c" * 64,
        published_at="2026-11-02T17:55:00+00:00",
        source="gdelt",
        source_kind="news",
        source_name="coindesk.com",
        source_url="https://coindesk.com/example",
        title="Example",
        excerpt="Example",
        engagement=0,
        evidence_tier="established_news",
        stance="neutral",
        tags=("launch",),
        risk_flags=(),
        matched_mints=(mint,),
        explicit_mints=(mint,),
    )
    signal = build_narrative_signals(
        [item],
        [{"mint": mint}],
        now=datetime(2026, 11, 2, 18, 0, tzinfo=UTC),
    )[0]
    with TradeJournal(tmp_path / "state.sqlite3") as journal:
        assert journal.upsert_narrative_items([item.as_dict()]) == 1
        assert journal.replace_narrative_signals([signal.as_dict()]) == 1
        stored_item = journal.narrative_items()[0]
        stored_signal = journal.narrative_signals()[0]

    assert stored_item["matched_mints"] == [mint]
    assert stored_item["tags"] == ["launch"]
    assert stored_signal["mint"] == mint
    assert stored_signal["latest_mention_at"] == item.published_at
    assert stored_signal["explicit_contract_mentions"] == 1


def test_refresh_links_stored_exact_callout_to_known_older_candidate(tmp_path: Path) -> None:
    known_mint = str(Pubkey.new_unique())
    unknown_mint = str(Pubkey.new_unique())
    published_at = (datetime.now(UTC) - timedelta(minutes=10)).isoformat()
    item = NarrativeItem(
        item_id="e" * 64,
        published_at=published_at,
        source="bluesky",
        source_kind="social",
        source_name="caller.example",
        source_url=None,
        title="Exact contract callout",
        excerpt=f"{known_mint} {unknown_mint}",
        engagement=1,
        evidence_tier="unverified_social",
        stance="neutral",
        tags=(),
        risk_flags=(),
        matched_mints=(),
        explicit_mints=(known_mint, unknown_mint),
    )
    with TradeJournal(tmp_path / "older-candidate.sqlite3") as journal:
        journal.upsert_discovery_candidate(
            mint=known_mint,
            source_program="test",
            audit_status="safe",
            audit_summary=None,
            top10_concentration_bps=1_000,
            decimals=6,
            market=None,
        )
        for _ in range(100):
            journal.upsert_discovery_candidate(
                mint=str(Pubkey.new_unique()),
                source_program="test",
                audit_status="blocked",
                audit_summary=None,
                top10_concentration_bps=None,
                decimals=6,
                market=None,
            )
        assert known_mint not in {str(row["mint"]) for row in journal.discovery_candidates()}
        journal.upsert_narrative_items([item.as_dict()])
        observed_before = journal._connection.execute(
            "SELECT observed_at FROM narrative_items WHERE item_id=?", (item.item_id,)
        ).fetchone()[0]
        result = refresh_narrative_intelligence(
            journal, gdelt_enabled=False, google_news_enabled=False, bluesky_enabled=False
        )
        observed_after = journal._connection.execute(
            "SELECT observed_at FROM narrative_items WHERE item_id=?", (item.item_id,)
        ).fetchone()[0]
        stored = journal.narrative_items()[0]
        signal = journal.narrative_signals()[0]

    assert result["exact_contract_items_relinked"] == 1
    assert result["older_known_candidates_linked"] == 1
    assert result["signals"] == 1
    assert stored["matched_mints"] == [known_mint]
    assert observed_after == observed_before
    assert signal["mint"] == known_mint
    assert signal["latest_mention_at"] == published_at
    assert signal["confidence"] == "medium"
    assert "social_only_evidence" in signal["risk_flags"]


def test_existing_narrative_signal_table_adds_mention_time_safely(tmp_path: Path) -> None:
    path = tmp_path / "legacy.sqlite3"
    with sqlite3.connect(path) as connection:
        connection.execute(
            """CREATE TABLE narrative_signals (
                mint TEXT PRIMARY KEY, updated_at TEXT NOT NULL,
                score INTEGER NOT NULL, state TEXT NOT NULL, confidence TEXT NOT NULL,
                mention_count INTEGER NOT NULL, unique_sources INTEGER NOT NULL,
                news_count INTEGER NOT NULL, social_count INTEGER NOT NULL,
                engagement INTEGER NOT NULL, explicit_contract_mentions INTEGER NOT NULL,
                tags_json TEXT NOT NULL, reasons_json TEXT NOT NULL,
                risk_flags_json TEXT NOT NULL
            )"""
        )
    mint = str(Pubkey.new_unique())
    with TradeJournal(path) as journal:
        journal.replace_narrative_signals(
            [{"mint": mint, "latest_mention_at": "2026-11-02T17:55:00+00:00"}]
        )
        assert journal.narrative_signals()[0]["latest_mention_at"] == ("2026-11-02T17:55:00+00:00")


def test_refresh_removes_expired_signals_even_when_all_providers_are_disabled(
    tmp_path: Path,
) -> None:
    mint = str(Pubkey.new_unique())
    with TradeJournal(tmp_path / "state.sqlite3") as journal:
        journal.replace_narrative_signals(
            [
                {
                    "mint": mint,
                    "score": 90,
                    "state": "surging",
                    "confidence": "high",
                    "mention_count": 2,
                    "unique_sources": 2,
                    "news_count": 2,
                    "social_count": 0,
                    "engagement": 0,
                    "explicit_contract_mentions": 2,
                    "tags": [],
                    "reasons": [],
                    "risk_flags": [],
                }
            ]
        )
        result = refresh_narrative_intelligence(
            journal, gdelt_enabled=False, google_news_enabled=False, bluesky_enabled=False
        )
        assert result["signals"] == 0
        assert journal.narrative_signals() == []


def test_narrative_refresh_history_preserves_prior_signal_and_explicit_absence(
    tmp_path: Path,
) -> None:
    mint = str(Pubkey.new_unique())
    with TradeJournal(tmp_path / "narrative-history.sqlite3") as journal:
        assert journal.narrative_signal_before(mint, decision_at_us=time.time_ns() // 1_000) is None
        journal.replace_narrative_signals(
            [
                {
                    "mint": mint,
                    "confidence": "high",
                    "latest_mention_at": "2026-11-03T12:00:00+00:00",
                    "explicit_contract_mentions": 2,
                    "unique_sources": 2,
                    "risk_flags": ["security_claim_present"],
                }
            ]
        )
        first = journal._connection.execute(
            "SELECT captured_at_us FROM narrative_signal_observations ORDER BY refresh_id LIMIT 1"
        ).fetchone()[0]
        earlier = journal.narrative_signal_before(mint, decision_at_us=first)
        assert earlier is not None
        assert earlier["signal"]["confidence"] == "high"
        assert earlier["signal"]["risk_flags"] == ["security_claim_present"]

        time.sleep(0.002)
        journal.replace_narrative_signals([])
        second = journal._connection.execute(
            "SELECT captured_at_us FROM narrative_signal_observations "
            "ORDER BY refresh_id DESC LIMIT 1"
        ).fetchone()[0]
        assert second > first
        assert journal.narrative_signal_before(mint, decision_at_us=first) == earlier
        cleared = journal.narrative_signal_before(mint, decision_at_us=second)
        assert cleared is not None
        assert cleared["signal"] is None
        assert journal.narrative_signals() == []


def test_failed_narrative_refresh_rolls_back_current_and_historical_signals(
    tmp_path: Path,
) -> None:
    mint = str(Pubkey.new_unique())
    with TradeJournal(tmp_path / "narrative-atomic.sqlite3") as journal:
        journal.replace_narrative_signals([{"mint": mint, "confidence": "medium"}])
        with pytest.raises(TypeError):
            journal.replace_narrative_signals(
                [{"mint": mint, "confidence": "high", "risk_flags": {"not-json"}}]
            )
        assert journal.narrative_signals()[0]["confidence"] == "medium"
        count = journal._connection.execute(
            "SELECT COUNT(*) FROM narrative_signal_observations"
        ).fetchone()[0]
        assert count == 1
