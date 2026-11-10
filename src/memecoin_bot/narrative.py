from __future__ import annotations

import hashlib
import math
import re
from collections import Counter
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit
from xml.etree.ElementTree import ParseError

import httpx
from defusedxml import ElementTree as SafeElementTree
from defusedxml.common import DefusedXmlException
from solders.pubkey import Pubkey

if TYPE_CHECKING:
    from .journal import TradeJournal


class NarrativeError(RuntimeError):
    """Raised when a fixed narrative provider returns malformed or unsafe data."""


_MINT_PATTERN = re.compile(
    r"(?<![1-9A-HJ-NP-Za-km-z])[1-9A-HJ-NP-Za-km-z]{32,44}(?![1-9A-HJ-NP-Za-km-z])"
)
_SPACE_PATTERN = re.compile(r"\s+")
_URL_PATTERN = re.compile(r"https?://\S+", re.IGNORECASE)
_PROMOTION_TERMS = (
    "100x",
    "1000x",
    "ape now",
    "buy now",
    "call alert",
    "contract address",
    "don't miss",
    "dont miss",
    "easy moon",
    "gem alert",
    "guaranteed",
    "next moon",
    "send it",
)
_SECURITY_TERMS = (
    "drained",
    "exploit",
    "freeze authority",
    "hack",
    "honeypot",
    "insider dump",
    "liquidity pulled",
    "rug pull",
    "rugged",
    "scam",
    "stolen",
)
_POSITIVE_TERMS = ("adoption", "breakout", "growth", "listed", "listing", "partnership", "surge")
_NEGATIVE_TERMS = ("crash", "dump", "fraud", "loss", "risk", "selloff", *_SECURITY_TERMS)
_SYMBOL_STOPLIST = {
    "AI",
    "BUY",
    "CA",
    "DOG",
    "ETH",
    "FUN",
    "MEME",
    "MOON",
    "PUMP",
    "SOL",
    "TOKEN",
    "USD",
    "USDC",
}
_ESTABLISHED_NEWS_DOMAINS = {
    "apnews.com",
    "bbc.com",
    "bloomberg.com",
    "coindesk.com",
    "cointelegraph.com",
    "decrypt.co",
    "forbes.com",
    "reuters.com",
    "theblock.co",
}


@dataclass(frozen=True, slots=True)
class NarrativeItem:
    item_id: str
    published_at: str
    source: str
    source_kind: str
    source_name: str
    source_url: str | None
    title: str
    excerpt: str
    engagement: int
    evidence_tier: str
    stance: str
    tags: tuple[str, ...]
    risk_flags: tuple[str, ...]
    matched_mints: tuple[str, ...]
    explicit_mints: tuple[str, ...]

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class NarrativeSignal:
    mint: str
    latest_mention_at: str
    score: int
    state: str
    confidence: str
    mention_count: int
    unique_sources: int
    news_count: int
    social_count: int
    engagement: int
    explicit_contract_mentions: int
    tags: tuple[str, ...]
    reasons: tuple[str, ...]
    risk_flags: tuple[str, ...]

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _short_text(value: object, *, limit: int) -> str:
    return _SPACE_PATTERN.sub(" ", value).strip()[:limit] if isinstance(value, str) else ""


def _safe_https_url(value: object) -> str | None:
    if not isinstance(value, str) or len(value) > 2_048:
        return None
    parsed = urlsplit(value)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        return None
    return value


def _parse_time(value: object) -> datetime:
    if isinstance(value, str):
        normalized = value.strip()
        for pattern in ("%Y%m%dT%H%M%SZ", "%Y%m%d%H%M%S"):
            try:
                return datetime.strptime(normalized, pattern).replace(tzinfo=UTC)
            except ValueError:
                pass
        try:
            parsed = datetime.fromisoformat(normalized.replace("Z", "+00:00"))
            if parsed.tzinfo is not None:
                return parsed.astimezone(UTC)
        except ValueError:
            pass
        try:
            parsed = parsedate_to_datetime(normalized)
            if parsed.tzinfo is not None:
                return parsed.astimezone(UTC)
        except (TypeError, ValueError):
            pass
    raise ValueError("narrative publication time is missing or invalid")


def _published_in_window(value: object, *, now: datetime) -> bool:
    try:
        published = _parse_time(value)
    except ValueError:
        return False
    return now - timedelta(hours=24) <= published <= now + timedelta(minutes=2)


def _nonnegative_int(value: object) -> int:
    return value if isinstance(value, int) and value >= 0 else 0


def _string_tuple(value: object) -> tuple[str, ...]:
    return tuple(item for item in value if isinstance(item, str)) if isinstance(value, list) else ()


def _valid_mints(text: str) -> tuple[str, ...]:
    values: list[str] = []
    for candidate in _MINT_PATTERN.findall(text):
        try:
            Pubkey.from_string(candidate)
        except ValueError:
            continue
        if candidate not in values:
            values.append(candidate)
    return tuple(values)


def _content_tags(text: str) -> tuple[str, ...]:
    lowered = text.lower()
    groups = {
        "ai": (" ai ", "artificial intelligence", "agent"),
        "animal": ("cat", "dog", "frog", "inu"),
        "callout": ("callout", "called", "ape", "gem"),
        "exchange_listing": ("exchange", "listed", "listing", "binance", "coinbase", "kraken"),
        "launch": ("launch", "bonding curve", "pump.fun", "pumpfun"),
        "politics": ("president", "election", "trump", "government"),
        "security": _SECURITY_TERMS,
        "whale_activity": ("whale", "smart money", "insider"),
    }
    return tuple(tag for tag, terms in groups.items() if any(term in lowered for term in terms))


def _stance(text: str) -> str:
    lowered = text.lower()
    positive = sum(term in lowered for term in _POSITIVE_TERMS)
    negative = sum(term in lowered for term in _NEGATIVE_TERMS)
    if positive and negative:
        return "mixed"
    if positive:
        return "positive"
    if negative:
        return "negative"
    return "neutral"


def _risk_flags(text: str, *, social: bool) -> tuple[str, ...]:
    lowered = text.lower()
    flags: list[str] = []
    if any(term in lowered for term in _PROMOTION_TERMS):
        flags.append("promotional_language")
    if any(term in lowered for term in _SECURITY_TERMS):
        flags.append("security_claim")
    if social:
        flags.append("unverified_social_source")
    return tuple(flags)


def _fingerprint(source: str, identity: str) -> str:
    return hashlib.sha256(f"{source}:{identity}".encode()).hexdigest()


def _provider_http_error(provider: str, exc: httpx.HTTPError) -> NarrativeError:
    """Create a useful provider error without exposing upstream response bodies."""
    if isinstance(exc, httpx.HTTPStatusError):
        return NarrativeError(f"{provider} returned HTTP {exc.response.status_code}")
    if isinstance(exc, httpx.TimeoutException):
        return NarrativeError(f"{provider} request timed out")
    return NarrativeError(f"{provider} request failed")


class GdeltNewsClient:
    """Bounded reader for GDELT's fixed-host DOC 2.0 article-list endpoint."""

    BASE_URL = "https://api.gdeltproject.org"
    QUERY = '(memecoin OR "meme coin" OR pumpfun OR "pump.fun") solana'

    def __init__(self, *, timeout_seconds: float = 10.0) -> None:
        self._client = httpx.Client(
            base_url=self.BASE_URL,
            headers={"Accept": "application/json", "User-Agent": "MemeOps/0.1"},
            timeout=httpx.Timeout(timeout_seconds, connect=4.0),
            limits=httpx.Limits(max_connections=2, max_keepalive_connections=1),
            follow_redirects=False,
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> GdeltNewsClient:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def recent(self, *, limit: int = 25) -> list[dict[str, object]]:
        if not 1 <= limit <= 50:
            raise NarrativeError("GDELT article limit must be between 1 and 50")
        try:
            response = self._client.get(
                "/api/v2/doc/doc",
                params={
                    "query": self.QUERY,
                    "mode": "artlist",
                    "maxrecords": str(limit),
                    "timespan": "24h",
                    "sort": "datedesc",
                    "format": "json",
                },
            )
            if len(response.content) > 2_000_000:
                raise NarrativeError("GDELT response exceeded the 2 MB safety limit")
            response.raise_for_status()
            payload = response.json()
        except httpx.HTTPError as exc:
            raise _provider_http_error("GDELT", exc) from exc
        except ValueError as exc:
            raise NarrativeError("GDELT returned invalid JSON") from exc
        articles = payload.get("articles") if isinstance(payload, dict) else None
        if not isinstance(articles, list):
            raise NarrativeError("GDELT returned malformed article data")
        return [article for article in articles if isinstance(article, dict)][:limit]


class GoogleNewsRssClient:
    """Bounded safe-XML reader for a fixed Google News RSS search."""

    BASE_URL = "https://news.google.com"

    def __init__(self, *, timeout_seconds: float = 10.0) -> None:
        self._client = httpx.Client(
            base_url=self.BASE_URL,
            headers={"Accept": "application/rss+xml", "User-Agent": "MemeOps/0.1"},
            timeout=httpx.Timeout(timeout_seconds, connect=4.0),
            limits=httpx.Limits(max_connections=2, max_keepalive_connections=1),
            follow_redirects=False,
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> GoogleNewsRssClient:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def recent(self, *, limit: int = 25) -> list[dict[str, object]]:
        if not 1 <= limit <= 50:
            raise NarrativeError("Google News article limit must be between 1 and 50")
        try:
            response = self._client.get(
                "/rss/search",
                params={
                    "q": 'solana (memecoin OR "meme coin" OR pumpfun) when:1d',
                    "hl": "en-US",
                    "gl": "US",
                    "ceid": "US:en",
                },
            )
            if len(response.content) > 2_000_000:
                raise NarrativeError("Google News response exceeded the 2 MB safety limit")
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise _provider_http_error("Google News", exc) from exc
        try:
            root = SafeElementTree.fromstring(response.content)
        except (ParseError, DefusedXmlException) as exc:
            raise NarrativeError("Google News returned unsafe or malformed XML") from exc
        articles: list[dict[str, object]] = []
        for item in root.findall("./channel/item")[:limit]:
            source_node = item.find("source")
            title = item.findtext("title")
            link = item.findtext("link")
            if not title or not link:
                continue
            articles.append(
                {
                    "title": title,
                    "url": link,
                    "seendate": item.findtext("pubDate"),
                    "source_name": source_node.text if source_node is not None else "Google News",
                    "source_url": source_node.get("url") if source_node is not None else None,
                }
            )
        return articles


class BlueskySearchClient:
    """Bounded unauthenticated reader for Bluesky's public AppView search endpoint."""

    BASE_URL = "https://api.bsky.app"
    QUERIES = ("solana memecoin", "pumpfun token")

    def __init__(self, *, timeout_seconds: float = 8.0) -> None:
        self._client = httpx.Client(
            base_url=self.BASE_URL,
            headers={"Accept": "application/json", "User-Agent": "MemeOps/0.1"},
            timeout=httpx.Timeout(timeout_seconds, connect=4.0),
            limits=httpx.Limits(max_connections=2, max_keepalive_connections=1),
            follow_redirects=False,
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> BlueskySearchClient:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def recent(self, *, limit: int = 25) -> list[dict[str, object]]:
        if not 1 <= limit <= 50:
            raise NarrativeError("Bluesky post limit must be between 1 and 50")
        posts: list[dict[str, object]] = []
        seen: set[str] = set()
        per_query = min(25, max(5, math.ceil(limit / len(self.QUERIES))))
        for query in self.QUERIES:
            try:
                response = self._client.get(
                    "/xrpc/app.bsky.feed.searchPosts",
                    params={"q": query, "limit": str(per_query), "sort": "latest"},
                )
                if len(response.content) > 2_000_000:
                    raise NarrativeError("Bluesky response exceeded the 2 MB safety limit")
                response.raise_for_status()
                payload = response.json()
            except httpx.HTTPError as exc:
                raise _provider_http_error("Bluesky", exc) from exc
            except ValueError as exc:
                raise NarrativeError("Bluesky returned invalid JSON") from exc
            values = payload.get("posts") if isinstance(payload, dict) else None
            if not isinstance(values, list):
                raise NarrativeError("Bluesky returned malformed post data")
            for post in values:
                if not isinstance(post, dict):
                    continue
                uri = post.get("uri")
                if not isinstance(uri, str) or uri in seen:
                    continue
                seen.add(uri)
                posts.append(post)
                if len(posts) >= limit:
                    return posts
        return posts


def _candidate_matches(text: str, candidates: list[dict[str, object]]) -> tuple[str, ...]:
    lowered = text.lower()
    explicit = set(_valid_mints(text))
    matches: list[str] = []
    for candidate in candidates:
        mint = str(candidate.get("mint") or "")
        if not mint:
            continue
        if mint in explicit:
            matches.append(mint)
            continue
        symbol = str(candidate.get("symbol") or "").strip().upper()
        name = str(candidate.get("name") or "").strip()
        symbol_match = (
            3 <= len(symbol) <= 12
            and symbol not in _SYMBOL_STOPLIST
            and re.search(rf"(?<![A-Z0-9])\$?{re.escape(symbol)}(?![A-Z0-9])", text.upper())
        )
        name_match = len(name) >= 5 and name.lower() in lowered
        if symbol_match or name_match:
            matches.append(mint)
    return tuple(dict.fromkeys(matches))


def normalize_gdelt_articles(
    articles: list[dict[str, object]], candidates: list[dict[str, object]]
) -> list[NarrativeItem]:
    items: list[NarrativeItem] = []
    for article in articles:
        title = _short_text(article.get("title"), limit=300)
        url = _safe_https_url(article.get("url"))
        if not title or not url:
            continue
        try:
            published_at = _parse_time(article.get("seendate")).isoformat()
        except ValueError:
            continue
        domain = _short_text(article.get("domain"), limit=120).lower()
        domain = domain.removeprefix("www.") or urlsplit(url).hostname or "unknown"
        text = title
        explicit = _valid_mints(text)
        tier = "established_news" if domain in _ESTABLISHED_NEWS_DOMAINS else "aggregated_news"
        items.append(
            NarrativeItem(
                item_id=_fingerprint("gdelt", url),
                published_at=published_at,
                source="gdelt",
                source_kind="news",
                source_name=domain,
                source_url=url,
                title=title,
                excerpt=title,
                engagement=0,
                evidence_tier=tier,
                stance=_stance(text),
                tags=_content_tags(text),
                risk_flags=_risk_flags(text, social=False),
                matched_mints=_candidate_matches(text, candidates),
                explicit_mints=explicit,
            )
        )
    return items


def normalize_google_news_articles(
    articles: list[dict[str, object]], candidates: list[dict[str, object]]
) -> list[NarrativeItem]:
    items: list[NarrativeItem] = []
    for article in articles:
        title = _short_text(article.get("title"), limit=300)
        url = _safe_https_url(article.get("url"))
        if not title or not url:
            continue
        try:
            published_at = _parse_time(article.get("seendate")).isoformat()
        except ValueError:
            continue
        publisher = _short_text(article.get("source_name"), limit=120) or "Google News"
        publisher_url = _safe_https_url(article.get("source_url"))
        publisher_domain = (
            (urlsplit(publisher_url).hostname or "").removeprefix("www.") if publisher_url else ""
        )
        explicit = _valid_mints(title)
        tier = (
            "established_news"
            if publisher_domain in _ESTABLISHED_NEWS_DOMAINS
            else "aggregated_news"
        )
        items.append(
            NarrativeItem(
                item_id=_fingerprint("google_news", url),
                published_at=published_at,
                source="google_news",
                source_kind="news",
                source_name=publisher,
                source_url=url,
                title=title,
                excerpt=title,
                engagement=0,
                evidence_tier=tier,
                stance=_stance(title),
                tags=_content_tags(title),
                risk_flags=_risk_flags(title, social=False),
                matched_mints=_candidate_matches(title, candidates),
                explicit_mints=explicit,
            )
        )
    return items


def normalize_bluesky_posts(
    posts: list[dict[str, object]], candidates: list[dict[str, object]]
) -> list[NarrativeItem]:
    items: list[NarrativeItem] = []
    for post in posts:
        uri = post.get("uri")
        author_value = post.get("author")
        record_value = post.get("record")
        author = author_value if isinstance(author_value, dict) else {}
        record = record_value if isinstance(record_value, dict) else {}
        text = _short_text(record.get("text"), limit=1_000)
        handle = _short_text(author.get("handle"), limit=120).lower()
        if not isinstance(uri, str) or not uri.startswith("at://") or not text or not handle:
            continue
        try:
            published_at = _parse_time(record.get("createdAt") or post.get("indexedAt")).isoformat()
        except ValueError:
            continue
        rkey = uri.rsplit("/", 1)[-1]
        source_url = (
            f"https://bsky.app/profile/{handle}/post/{rkey}"
            if re.fullmatch(r"[a-z0-9.-]+", handle) and re.fullmatch(r"[a-zA-Z0-9]+", rkey)
            else None
        )
        engagement = sum(
            _nonnegative_int(post.get(key))
            for key in ("likeCount", "repostCount", "replyCount", "quoteCount")
        )
        explicit = _valid_mints(text)
        items.append(
            NarrativeItem(
                item_id=_fingerprint("bluesky", uri),
                published_at=published_at,
                source="bluesky",
                source_kind="social",
                source_name=handle,
                source_url=source_url,
                title=_short_text(text, limit=180),
                excerpt=text,
                engagement=max(0, engagement),
                evidence_tier="unverified_social",
                stance=_stance(text),
                tags=_content_tags(text),
                risk_flags=_risk_flags(text, social=True),
                matched_mints=_candidate_matches(text, candidates),
                explicit_mints=explicit,
            )
        )
    return items


def deduplicate_items(items: list[NarrativeItem]) -> list[NarrativeItem]:
    unique: list[NarrativeItem] = []
    seen_ids: set[str] = set()
    seen_content: set[str] = set()
    for item in sorted(items, key=lambda value: value.published_at, reverse=True):
        normalized = _URL_PATTERN.sub("", item.excerpt.lower())
        content_hash = hashlib.sha256(
            _SPACE_PATTERN.sub(" ", normalized).strip().encode()
        ).hexdigest()
        if item.item_id in seen_ids or content_hash in seen_content:
            continue
        seen_ids.add(item.item_id)
        seen_content.add(content_hash)
        unique.append(item)
    return unique


def build_narrative_signals(
    items: list[NarrativeItem],
    candidates: list[dict[str, object]],
    *,
    now: datetime | None = None,
) -> list[NarrativeSignal]:
    current = now or datetime.now(UTC)
    signals: list[NarrativeSignal] = []
    for candidate in candidates:
        mint = str(candidate.get("mint") or "")
        related = [
            item
            for item in items
            if mint
            and mint in item.matched_mints
            and _published_in_window(item.published_at, now=current)
        ]
        if not related:
            continue
        sources = {f"{item.source}:{item.source_name}" for item in related}
        news_count = sum(item.source_kind == "news" for item in related)
        social_count = sum(item.source_kind == "social" for item in related)
        engagement = sum(item.engagement for item in related)
        explicit_count = sum(mint in item.explicit_mints for item in related)
        newest = max(_parse_time(item.published_at) for item in related)
        age_minutes = max(0, int((current - newest).total_seconds() // 60))
        reasons: list[str] = []
        risk_flags: list[str] = []
        score = min(24, len(sources) * 8)
        reasons.append(f"{len(sources)} independent source(s) (+{min(24, len(sources) * 8)})")
        recency_points = (
            20
            if age_minutes <= 30
            else 14
            if age_minutes <= 120
            else 8
            if age_minutes <= 360
            else 2
        )
        score += recency_points
        reasons.append(f"newest mention is {age_minutes}m old (+{recency_points})")
        news_points = min(15, news_count * 5)
        score += news_points
        if news_points:
            reasons.append(f"{news_count} news mention(s) (+{news_points})")
        engagement_points = min(15, int(math.log10(engagement + 1) * 5))
        score += engagement_points
        if engagement_points:
            reasons.append(f"bounded social engagement (+{engagement_points})")
        linkage_points = 20 if explicit_count else 5
        score += linkage_points
        reasons.append(
            f"{'contract-address' if explicit_count else 'symbol/name'} linkage (+{linkage_points})"
        )
        if len(sources) >= 3:
            score += 10
            reasons.append("three-source corroboration (+10)")
        promotion_count = sum("promotional_language" in item.risk_flags for item in related)
        security_count = sum("security_claim" in item.risk_flags for item in related)
        if social_count and not news_count:
            risk_flags.append("social_only_evidence")
            score -= 10
        if promotion_count * 2 >= len(related):
            risk_flags.append("promotion_dominant")
            score -= 15
        if security_count:
            risk_flags.append("security_claim_present")
        if explicit_count == 0:
            risk_flags.append("weak_symbol_or_name_linkage")
        confidence = (
            "high"
            if explicit_count and len(sources) >= 2
            else "medium"
            if explicit_count
            else "low"
        )
        score = max(0, min(100, score))
        if not explicit_count:
            score = min(score, 39)
            reasons.append("ambiguous ticker/name linkage caps attention below active (max 39)")
        state = "surging" if score >= 65 else "active" if score >= 40 else "quiet"
        tags = tuple(sorted({tag for item in related for tag in item.tags}))
        signals.append(
            NarrativeSignal(
                mint=mint,
                latest_mention_at=newest.isoformat(),
                score=score,
                state=state,
                confidence=confidence,
                mention_count=len(related),
                unique_sources=len(sources),
                news_count=news_count,
                social_count=social_count,
                engagement=engagement,
                explicit_contract_mentions=explicit_count,
                tags=tags,
                reasons=tuple(reasons),
                risk_flags=tuple(risk_flags),
            )
        )
    return sorted(signals, key=lambda signal: signal.score, reverse=True)


def _stored_item(value: dict[str, object]) -> NarrativeItem:
    return NarrativeItem(
        item_id=str(value.get("item_id") or ""),
        published_at=str(value.get("published_at") or ""),
        source=str(value.get("source") or "unknown"),
        source_kind=str(value.get("source_kind") or "social"),
        source_name=str(value.get("source_name") or "unknown"),
        source_url=str(value["source_url"]) if value.get("source_url") else None,
        title=str(value.get("title") or "Untitled"),
        excerpt=str(value.get("excerpt") or ""),
        engagement=_nonnegative_int(value.get("engagement")),
        evidence_tier=str(value.get("evidence_tier") or "unverified"),
        stance=str(value.get("stance") or "neutral"),
        tags=_string_tuple(value.get("tags")),
        risk_flags=_string_tuple(value.get("risk_flags")),
        matched_mints=_string_tuple(value.get("matched_mints")),
        explicit_mints=_string_tuple(value.get("explicit_mints")),
    )


def link_known_contract_mentions(
    journal: TradeJournal,
    items: list[NarrativeItem],
    candidates: list[dict[str, object]],
) -> tuple[list[NarrativeItem], list[dict[str, object]], int, int]:
    """Link exact contract mentions to known older candidates, with bounded lookups."""
    by_mint = {
        str(candidate["mint"]): candidate for candidate in candidates if candidate.get("mint")
    }
    checked: set[str] = set()
    relinked = 0
    initial_candidate_count = len(by_mint)
    linked_items: list[NarrativeItem] = []
    for item in items:
        for mint in item.explicit_mints[:5]:
            if mint in by_mint or mint in checked or len(checked) >= 100:
                continue
            checked.add(mint)
            known = journal.discovery_candidate(mint)
            if known is not None:
                by_mint[mint] = known
        matches = tuple(
            dict.fromkeys(
                [*item.matched_mints, *(mint for mint in item.explicit_mints if mint in by_mint)]
            )
        )[:100]
        if matches != item.matched_mints:
            journal.update_narrative_item_matches(item.item_id, matches)
            item = replace(item, matched_mints=matches)
            relinked += 1
        linked_items.append(item)
    return linked_items, list(by_mint.values()), relinked, len(by_mint) - initial_candidate_count


def refresh_narrative_intelligence(
    journal: TradeJournal,
    *,
    gdelt_enabled: bool = True,
    google_news_enabled: bool = True,
    bluesky_enabled: bool = True,
    item_limit: int = 30,
) -> dict[str, object]:
    """Collect, normalize, persist, and score recent public narrative evidence."""
    candidates = journal.discovery_candidates(limit=100)
    collected: list[NarrativeItem] = []
    providers: dict[str, dict[str, object]] = {}
    if gdelt_enabled:
        try:
            with GdeltNewsClient() as client:
                normalized = normalize_gdelt_articles(client.recent(limit=item_limit), candidates)
            collected.extend(normalized)
            providers["gdelt"] = {"status": "ok", "items": len(normalized)}
        except NarrativeError as exc:
            providers["gdelt"] = {"status": "degraded", "error": str(exc)[:160]}
    else:
        providers["gdelt"] = {"status": "disabled"}
    if google_news_enabled:
        try:
            with GoogleNewsRssClient() as client:
                normalized = normalize_google_news_articles(
                    client.recent(limit=item_limit), candidates
                )
            collected.extend(normalized)
            providers["google_news"] = {"status": "ok", "items": len(normalized)}
        except NarrativeError as exc:
            providers["google_news"] = {"status": "degraded", "error": str(exc)[:160]}
    else:
        providers["google_news"] = {"status": "disabled"}
    if bluesky_enabled:
        try:
            with BlueskySearchClient() as client:
                normalized = normalize_bluesky_posts(client.recent(limit=item_limit), candidates)
            collected.extend(normalized)
            providers["bluesky"] = {"status": "ok", "items": len(normalized)}
        except NarrativeError as exc:
            providers["bluesky"] = {"status": "degraded", "error": str(exc)[:160]}
    else:
        providers["bluesky"] = {"status": "disabled"}

    now = datetime.now(UTC)
    unique = [
        item
        for item in deduplicate_items(collected)
        if _published_in_window(item.published_at, now=now)
    ]
    persisted = journal.upsert_narrative_items([item.as_dict() for item in unique])
    stored = [_stored_item(item) for item in journal.narrative_items(limit=200)]
    recent = [item for item in stored if _published_in_window(item.published_at, now=now)]
    recent, candidates, relinked, extra_candidates = link_known_contract_mentions(
        journal, recent, candidates
    )
    signals = build_narrative_signals(recent, candidates)
    journal.replace_narrative_signals([signal.as_dict() for signal in signals])
    tags = Counter(tag for item in recent for tag in item.tags)
    return {
        "updated_at": datetime.now(UTC).isoformat(),
        "providers": providers,
        "collected": len(unique),
        "persisted": persisted,
        "recent_items": len(recent),
        "exact_contract_items_relinked": relinked,
        "older_known_candidates_linked": extra_candidates,
        "signals": len(signals),
        "top_tags": [{"tag": tag, "mentions": count} for tag, count in tags.most_common(8)],
    }
