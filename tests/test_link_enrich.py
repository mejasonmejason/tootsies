"""Tests for utils.link_enrich, URL detection + per-platform fetchers + cache + format."""

from __future__ import annotations

import json
from collections import OrderedDict
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from utils.link_enrich import (
    EnrichedLink,
    _async_lru_cache,
    _enrich_bluesky,
    _enrich_cached,
    _enrich_reddit,
    _enrich_tiktok,
    _enrich_twitter,
    _enrich_youtube,
    _humanize_count,
    detect_platform,
    enrich,
    enrich_batch,
    format_enriched_for_prompt,
)

FIXTURES = Path(__file__).parent / "fixtures"


def _load_fixture(name: str) -> Any:
    return json.loads((FIXTURES / name).read_text())


@pytest.fixture(autouse=True)
def _reset_cache() -> Any:
    """Clear the shared LRU cache between tests so call counts are predictable."""
    cache = getattr(_enrich_cached, "_cache", None)
    if isinstance(cache, OrderedDict):
        cache.clear()
    yield
    if isinstance(cache, OrderedDict):
        cache.clear()


# ---- URL detection --------------------------------------------------------------


def test_detect_platform_twitter_variants() -> None:
    """Detects canonical x.com + twitter.com + fxtwitter/vxtwitter fixers."""
    assert detect_platform("https://x.com/foo/status/123") == "twitter"
    assert detect_platform("https://twitter.com/foo/status/123") == "twitter"
    assert detect_platform("https://fxtwitter.com/foo/status/123") == "twitter"
    assert detect_platform("https://vxtwitter.com/foo/status/123") == "twitter"
    assert detect_platform("https://nitter.net/foo/status/123") == "twitter"


def test_detect_platform_twitter_requires_status_id() -> None:
    """Profile URLs without /status/<id> aren't enrichable via fxtwitter."""
    assert detect_platform("https://x.com/champagnepapi") is None


def test_detect_platform_tiktok_variants() -> None:
    assert detect_platform("https://tiktok.com/@user/video/123") == "tiktok"
    assert detect_platform("https://vm.tiktok.com/abc") == "tiktok"
    assert detect_platform("https://tnktok.com/t/ZP8pgmfD6/") == "tiktok"
    assert detect_platform("https://vxtiktok.com/@user/video/123") == "tiktok"


def test_detect_platform_youtube_variants() -> None:
    assert detect_platform("https://youtube.com/watch?v=abc") == "youtube"
    assert detect_platform("https://www.youtube.com/watch?v=abc") == "youtube"
    assert detect_platform("https://youtu.be/abc") == "youtube"
    assert detect_platform("https://www.youtube.com/shorts/abc") == "youtube"


def test_detect_platform_reddit_variants() -> None:
    assert detect_platform("https://reddit.com/r/foo/comments/abc/title/") == "reddit"
    assert detect_platform("https://old.reddit.com/r/foo/comments/abc/") == "reddit"
    assert detect_platform("https://redd.it/abc") == "reddit"


def test_detect_platform_bluesky_requires_post_path() -> None:
    assert detect_platform(
        "https://bsky.app/profile/handle.bsky.social/post/abc"
    ) == "bluesky"
    # Profile only -> not enrichable (no post id)
    assert detect_platform("https://bsky.app/profile/handle.bsky.social") is None


def test_detect_platform_unknown_returns_none() -> None:
    assert detect_platform("https://some-random-blog.com/post") is None
    assert detect_platform("https://github.com/foo/bar") is None
    assert detect_platform("https://instagram.com/p/abc") is None  # Instagram not supported


def test_detect_platform_rejects_non_http() -> None:
    assert detect_platform("") is None
    assert detect_platform("not a url") is None
    assert detect_platform("ftp://x.com/status/1") is None


# ---- EnrichedLink dataclass -----------------------------------------------------


def test_enriched_link_defaults() -> None:
    link = EnrichedLink(platform="X/Twitter", url="https://x.com/x/status/1")
    assert link.author == ""
    assert link.text == ""
    assert link.title == ""
    assert link.media_urls == []
    assert link.engagement == {}


def test_enriched_link_with_all_fields() -> None:
    link = EnrichedLink(
        platform="TikTok",
        url="https://tiktok.com/@a/video/1",
        author="@a",
        text="caption",
        title="",
        media_urls=["https://cdn/c.jpg"],
        engagement={"plays": 1000},
    )
    assert link.platform == "TikTok"
    assert link.engagement["plays"] == 1000


# ---- per-platform enrichers (mocked) --------------------------------------------


@pytest.mark.asyncio
async def test_enrich_twitter_parses_fxtwitter_response() -> None:
    sample = _load_fixture("fxtwitter_sample.json")
    with patch("utils.link_enrich._fetch_json", AsyncMock(return_value=sample)):
        link = await _enrich_twitter("https://x.com/champagnepapi/status/12345")
    assert link is not None
    assert link.platform == "X/Twitter"
    assert link.author == "@champagnepapi"
    assert "iceman tracklist" in link.text
    assert link.engagement["likes"] == 127000
    assert link.engagement["retweets"] == 18000
    assert len(link.media_urls) == 1


@pytest.mark.asyncio
async def test_enrich_twitter_returns_none_on_bad_code() -> None:
    with patch(
        "utils.link_enrich._fetch_json",
        AsyncMock(return_value={"code": 404, "message": "tweet not found"}),
    ):
        link = await _enrich_twitter("https://x.com/foo/status/999")
    assert link is None


@pytest.mark.asyncio
async def test_enrich_twitter_returns_none_when_fetch_fails() -> None:
    with patch("utils.link_enrich._fetch_json", AsyncMock(return_value=None)):
        link = await _enrich_twitter("https://x.com/foo/status/999")
    assert link is None


@pytest.mark.asyncio
async def test_enrich_tiktok_parses_tikwm_response() -> None:
    sample = _load_fixture("tikwm_sample.json")
    with patch("utils.link_enrich._fetch_json", AsyncMock(return_value=sample)):
        link = await _enrich_tiktok("https://tiktok.com/@cdmurals/video/7384729384729384")
    assert link is not None
    assert link.platform == "TikTok"
    assert link.author == "@cdmurals"
    assert "kendrick" in link.text.lower()
    assert link.engagement["plays"] == 2100000
    assert link.engagement["likes"] == 487000


@pytest.mark.asyncio
async def test_enrich_tiktok_returns_none_on_bad_code() -> None:
    with patch(
        "utils.link_enrich._fetch_json",
        AsyncMock(return_value={"code": -1, "msg": "fail"}),
    ):
        link = await _enrich_tiktok("https://tiktok.com/@a/video/1")
    assert link is None


@pytest.mark.asyncio
async def test_enrich_youtube_parses_oembed() -> None:
    sample = _load_fixture("youtube_oembed_sample.json")
    with patch("utils.link_enrich._fetch_json", AsyncMock(return_value=sample)):
        link = await _enrich_youtube("https://youtube.com/watch?v=abc123")
    assert link is not None
    assert link.platform == "YouTube"
    assert link.author == "Kendrick Lamar"
    assert "Not Like Us" in link.title
    assert link.media_urls == ["https://i.ytimg.com/vi/abc123/hqdefault.jpg"]
    # oEmbed has no engagement counters; engagement dict stays empty.
    assert link.engagement == {}


@pytest.mark.asyncio
async def test_enrich_reddit_parses_post_and_top_comments() -> None:
    sample = _load_fixture("reddit_sample.json")
    with patch("utils.link_enrich._fetch_json", AsyncMock(return_value=sample)):
        link = await _enrich_reddit(
            "https://reddit.com/r/dccu/comments/abc/penguin_reveal/"
        )
    assert link is not None
    assert link.platform == "Reddit"
    assert link.author == "u/gothamhead"
    assert "Penguin reveal" in link.title
    assert "runtime dragged" in link.text
    # Should include top live comments but skip [deleted] ones.
    assert "the runtime was the real issue" in link.text
    assert "best villain in years" in link.text
    assert "[deleted]" not in link.text
    assert link.engagement["score"] == 14820
    assert link.engagement["comments"] == 3471


@pytest.mark.asyncio
async def test_enrich_reddit_handles_missing_comments_listing() -> None:
    """Some reddit responses come back as just the post listing, no comments."""
    minimal = [
        {
            "data": {
                "children": [{
                    "data": {
                        "title": "just a title",
                        "selftext": "",
                        "author": "x",
                        "score": 1,
                        "num_comments": 0,
                    },
                }],
            },
        },
    ]
    with patch("utils.link_enrich._fetch_json", AsyncMock(return_value=minimal)):
        link = await _enrich_reddit("https://reddit.com/r/foo/comments/abc/x/")
    assert link is not None
    assert link.title == "just a title"


@pytest.mark.asyncio
async def test_enrich_reddit_returns_none_on_malformed_response() -> None:
    with patch("utils.link_enrich._fetch_json", AsyncMock(return_value={"weird": 1})):
        link = await _enrich_reddit("https://reddit.com/r/foo/comments/abc/")
    assert link is None


@pytest.mark.asyncio
async def test_enrich_bluesky_parses_thread_response() -> None:
    sample = _load_fixture("bluesky_sample.json")
    with patch("utils.link_enrich._fetch_json", AsyncMock(return_value=sample)):
        link = await _enrich_bluesky(
            "https://bsky.app/profile/danpriceseattle.bsky.social/post/xyz"
        )
    assert link is not None
    assert link.platform == "Bluesky"
    assert link.author == "@danpriceseattle.bsky.social"
    assert "payroll tax" in link.text
    assert link.engagement["likes"] == 8421
    assert link.engagement["reposts"] == 1840


@pytest.mark.asyncio
async def test_enrich_bluesky_returns_none_on_bad_url() -> None:
    """No post path -> no enrichment, even if URL hits the host check elsewhere."""
    with patch("utils.link_enrich._fetch_json", AsyncMock(return_value={})):
        link = await _enrich_bluesky("https://bsky.app/profile/x")
    assert link is None


# ---- enrich() public entry + event emission -------------------------------------


@pytest.mark.asyncio
async def test_enrich_returns_none_for_unknown_platform() -> None:
    """Non-enrichable URLs short-circuit without emitting events."""
    link = await enrich("https://random-blog.com/post/123")
    assert link is None


@pytest.mark.asyncio
async def test_enrich_emits_event_with_platform_and_url_host(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Successful enrichment emits a link_enrich event with summary stats only."""
    import logging
    caplog.set_level(logging.INFO, logger="tootsies.events")
    sample = _load_fixture("fxtwitter_sample.json")
    with patch("utils.link_enrich._fetch_json", AsyncMock(return_value=sample)):
        await enrich("https://x.com/foo/status/1")
    events = [r.getMessage() for r in caplog.records if r.name == "tootsies.events"]
    matches = [m for m in events if "link_enrich" in m]
    assert matches, "expected a link_enrich event"
    # Verify the event carries platform + url_host + ok, but NOT full URL/query.
    msg = matches[0]
    assert '"platform":"twitter"' in msg
    assert '"url_host":"x.com"' in msg
    assert '"ok":true' in msg
    # Sanity: no path or query string leaked into the event.
    assert "/foo/status/1" not in msg


@pytest.mark.asyncio
async def test_enrich_failopen_on_exception_returns_none(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """An unexpected exception in the underlying fetcher should NOT bubble up."""
    import logging
    caplog.set_level(logging.INFO, logger="tootsies.events")
    # Patch the cached helper to raise mid-flight. The defensive try/except in
    # enrich() catches it and emits a recoverable error event.
    with patch(
        "utils.link_enrich._enrich_cached",
        AsyncMock(side_effect=RuntimeError("boom")),
    ):
        link = await enrich("https://x.com/foo/status/1")
    assert link is None
    events = [r.getMessage() for r in caplog.records if r.name == "tootsies.events"]
    # Both the error event AND the link_enrich event should fire.
    assert any('"event":"error"' in m and "link_enrich" in m for m in events)


# ---- cache behavior --------------------------------------------------------------


@pytest.mark.asyncio
async def test_cache_hits_avoid_repeat_fetch() -> None:
    """Same URL twice = exactly one underlying _fetch_json call."""
    sample = _load_fixture("fxtwitter_sample.json")
    mock_fetch = AsyncMock(return_value=sample)
    with patch("utils.link_enrich._fetch_json", mock_fetch):
        url = "https://x.com/cache-test/status/42"
        link1 = await enrich(url)
        link2 = await enrich(url)
    assert link1 is not None
    assert link2 is not None
    # The fxtwitter endpoint should have been hit exactly ONCE.
    assert mock_fetch.call_count == 1


@pytest.mark.asyncio
async def test_async_lru_cache_evicts_oldest_past_maxsize() -> None:
    """Tiny cache (maxsize=2) drops the LRU entry when a third arrives."""
    calls: list[str] = []

    @_async_lru_cache(maxsize=2)
    async def fake(url: str) -> str:
        calls.append(url)
        return f"R:{url}"

    assert await fake("a") == "R:a"
    assert await fake("b") == "R:b"
    assert await fake("a") == "R:a"  # cache hit (no new call)
    assert await fake("c") == "R:c"  # evicts "b" since "a" was just touched
    assert await fake("b") == "R:b"  # miss, re-fetch
    # a, b, a-hit, c, b-refetch -> 4 underlying calls (a, b, c, b)
    assert calls == ["a", "b", "c", "b"]


# ---- batch helper ----------------------------------------------------------------


@pytest.mark.asyncio
async def test_enrich_batch_runs_in_parallel_and_returns_per_url() -> None:
    """Batch returns a dict keyed by URL, includes all known platforms."""
    twitter_sample = _load_fixture("fxtwitter_sample.json")
    youtube_sample = _load_fixture("youtube_oembed_sample.json")

    async def fake_fetch(url: str) -> Any:
        if "fxtwitter" in url:
            return twitter_sample
        if "youtube" in url or "oembed" in url:
            return youtube_sample
        return None

    with patch("utils.link_enrich._fetch_json", AsyncMock(side_effect=fake_fetch)):
        out = await enrich_batch([
            "https://x.com/a/status/1",
            "https://youtube.com/watch?v=abc",
            "https://random-site.com/post",
        ])
    assert set(out.keys()) == {
        "https://x.com/a/status/1",
        "https://youtube.com/watch?v=abc",
        "https://random-site.com/post",
    }
    assert out["https://x.com/a/status/1"] is not None
    assert out["https://youtube.com/watch?v=abc"] is not None
    assert out["https://random-site.com/post"] is None


@pytest.mark.asyncio
async def test_enrich_batch_returns_partial_when_some_fail() -> None:
    """If one URL's fetcher returns None, the others still get their enrichment."""
    twitter_sample = _load_fixture("fxtwitter_sample.json")

    async def fake_fetch(url: str) -> Any:
        # Twitter works, TikTok endpoint returns nothing useful.
        if "fxtwitter" in url:
            return twitter_sample
        return None

    with patch("utils.link_enrich._fetch_json", AsyncMock(side_effect=fake_fetch)):
        out = await enrich_batch([
            "https://x.com/a/status/1",
            "https://tiktok.com/@a/video/1",
        ])
    assert out["https://x.com/a/status/1"] is not None
    assert out["https://tiktok.com/@a/video/1"] is None


@pytest.mark.asyncio
async def test_enrich_batch_handles_empty_list() -> None:
    assert await enrich_batch([]) == {}


@pytest.mark.asyncio
async def test_enrich_batch_dedupes_repeated_urls() -> None:
    """Same URL passed twice in a batch only fires one fetch."""
    sample = _load_fixture("fxtwitter_sample.json")
    mock_fetch = AsyncMock(return_value=sample)
    with patch("utils.link_enrich._fetch_json", mock_fetch):
        url = "https://x.com/dedup/status/1"
        out = await enrich_batch([url, url, url])
    assert len(out) == 1
    assert mock_fetch.call_count == 1


# ---- format_enriched_for_prompt --------------------------------------------------


def test_format_enriched_for_prompt_empty_returns_empty_string() -> None:
    """Empty list yields empty string so callers can concat unconditionally."""
    assert format_enriched_for_prompt([]) == ""


def test_format_enriched_for_prompt_renders_header_and_entry() -> None:
    """Output should announce itself and include enough for Claude to use it."""
    link = EnrichedLink(
        platform="X/Twitter",
        url="https://x.com/foo/status/1",
        author="@foo",
        text="hot take here",
        engagement={"likes": 1500, "retweets": 200},
    )
    rendered = format_enriched_for_prompt([link])
    assert "ENRICHED LINKS" in rendered
    # The "don't re-call web_search" instruction should be in the header.
    assert "web_search" in rendered
    assert "[X/Twitter]" in rendered
    assert "@foo" in rendered
    assert "hot take here" in rendered
    assert "1.5K likes" in rendered
    assert "https://x.com/foo/status/1" in rendered


def test_format_enriched_for_prompt_multiple_links() -> None:
    """Multi-link block has one bullet per link."""
    links = [
        EnrichedLink(
            platform="TikTok", url="https://tiktok.com/@a/video/1",
            author="@a", text="caption", engagement={"plays": 2100000},
        ),
        EnrichedLink(
            platform="YouTube", url="https://youtube.com/watch?v=x",
            author="kendrick", title="Not Like Us",
        ),
    ]
    rendered = format_enriched_for_prompt(links)
    assert "[TikTok]" in rendered
    assert "[YouTube]" in rendered
    assert "2.1M plays" in rendered
    assert "Not Like Us" in rendered


def test_humanize_count_thresholds() -> None:
    """K above 1k, M above 1m, bare integer below 1k."""
    assert _humanize_count(42) == "42"
    assert _humanize_count(999) == "999"
    assert _humanize_count(1500) == "1.5K"
    assert _humanize_count(2_100_000) == "2.1M"
