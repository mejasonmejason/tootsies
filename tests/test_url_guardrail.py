"""Tests for utils.url_guardrail, the hallucination + dedup catch."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from utils.url_guardrail import (
    enforce_allowlist,
    enforce_source_links,
    ensure_protocol,
    extract_urls,
    normalize,
    prefix_bare_urls,
    verify_live_links,
)

# ---- normalize ----------------------------------------------------------------


def test_normalize_lowercases_host_only():
    assert normalize("HTTPS://Example.COM/Foo") == "https://example.com/Foo"


def test_normalize_strips_tracking_params():
    n = normalize("https://example.com/x?utm_source=a&id=42&utm_medium=b")
    assert n == "https://example.com/x?id=42"


def test_normalize_strips_all_tracking_params_to_bare_url():
    n = normalize("https://example.com/x?utm_source=a&fbclid=zzz&gclid=qqq")
    assert n == "https://example.com/x"


def test_normalize_strips_trailing_slash():
    assert normalize("https://example.com/foo/") == "https://example.com/foo"


def test_normalize_strips_trailing_punct():
    assert normalize("https://example.com/foo.") == "https://example.com/foo"
    assert normalize("https://example.com/foo,") == "https://example.com/foo"


def test_normalize_known_tracking_si_param():
    # YouTube-style `si` share param
    n = normalize("https://youtu.be/abc?si=xyz")
    assert n == "https://youtu.be/abc"


# ---- extract_urls -------------------------------------------------------------


def test_extract_urls_basic():
    text = "see https://example.com/a and https://b.io/x"
    assert extract_urls(text) == ["https://example.com/a", "https://b.io/x"]


def test_extract_urls_strips_trailing_punct():
    text = "wild. https://example.com/a."
    assert extract_urls(text) == ["https://example.com/a"]


def test_extract_urls_stops_at_brackets():
    # Conservative regex must stop at ( ) [ ] { } so wrapped URLs come out clean.
    text = "see (https://example.com/a) and <https://b.io/x>"
    assert extract_urls(text) == ["https://example.com/a", "https://b.io/x"]


def test_extract_urls_none():
    assert extract_urls("no urls here") == []


# ---- enforce_allowlist --------------------------------------------------------


def test_enforce_passes_url_in_allowlist():
    text = "fire take. https://example.com/a"
    cleaned, rejected, deduped = enforce_allowlist(text, ["https://example.com/a"])
    assert cleaned == "fire take. https://example.com/a"
    assert rejected == []
    assert deduped == []


def test_enforce_strips_url_not_in_allowlist():
    text = "fire take.\nhttps://hallucinated.example/x"
    cleaned, rejected, deduped = enforce_allowlist(text, ["https://real.example/a"])
    assert "hallucinated" not in cleaned
    assert cleaned == "fire take."
    assert rejected == ["https://hallucinated.example/x"]
    assert deduped == []


def test_enforce_keeps_real_strips_fake_in_mixed():
    text = "see https://real.example/a and https://fake.example/b end."
    cleaned, rejected, deduped = enforce_allowlist(text, ["https://real.example/a"])
    assert "https://real.example/a" in cleaned
    assert "fake.example" not in cleaned
    assert rejected == ["https://fake.example/b"]
    assert deduped == []


def test_enforce_normalization_match():
    text = "take. https://example.com/foo"
    cleaned, rejected, _ = enforce_allowlist(text, ["HTTPS://EXAMPLE.COM/foo/"])
    assert rejected == []
    assert "https://example.com/foo" in cleaned


def test_enforce_normalization_strips_utm_for_match():
    text = "take. https://example.com/foo?utm_source=zzz"
    cleaned, rejected, _ = enforce_allowlist(text, ["https://example.com/foo"])
    assert rejected == []
    assert "https://example.com/foo" in cleaned


def test_enforce_empty_allowlist_strips_all():
    text = "take. https://example.com/a"
    cleaned, rejected, _ = enforce_allowlist(text, [])
    assert cleaned == "take."
    assert rejected == ["https://example.com/a"]


def test_enforce_no_urls_passthrough():
    text = "just a take, no link"
    cleaned, rejected, deduped = enforce_allowlist(text, ["https://example.com/a"])
    assert cleaned == "just a take, no link"
    assert rejected == []
    assert deduped == []


def test_enforce_preserves_trailing_punct_in_text():
    text = "wild. https://example.com/foo."
    cleaned, rejected, _ = enforce_allowlist(text, [])
    assert "https://example.com" not in cleaned
    assert cleaned.endswith(".")
    assert rejected == ["https://example.com/foo"]


def test_enforce_collapses_blank_lines_from_strip():
    text = "take here.\n\nhttps://fake.example/a\n\nmore text"
    cleaned, _, _ = enforce_allowlist(text, [])
    assert "fake.example" not in cleaned
    assert "\n\n\n" not in cleaned


def test_enforce_url_only_message_becomes_empty():
    text = "https://fake.example/x"
    cleaned, rejected, _ = enforce_allowlist(text, [])
    assert cleaned == ""
    assert rejected == ["https://fake.example/x"]


# ---- enforce_allowlist with recently_seen (dedup) -----------------------------


def test_dedup_strips_url_already_in_chat():
    """URL is in allowlist BUT already visible recently → strip, mark deduped."""
    text = "yeah that's the one.\nhttps://example.com/foo"
    cleaned, rejected, deduped = enforce_allowlist(
        text,
        allowlist=["https://example.com/foo"],
        recently_seen=["https://example.com/foo"],
    )
    assert "https://example.com/foo" not in cleaned
    assert cleaned == "yeah that's the one."
    assert rejected == []
    assert deduped == ["https://example.com/foo"]


def test_dedup_distinguishes_rejected_from_deduped():
    """Hallucinated URL → rejected. Allowlisted-but-seen → deduped."""
    text = "see https://fake.example/x and https://real.example/y"
    cleaned, rejected, deduped = enforce_allowlist(
        text,
        allowlist=["https://real.example/y"],
        recently_seen=["https://real.example/y"],
    )
    assert "fake.example" not in cleaned
    assert "real.example" not in cleaned
    assert rejected == ["https://fake.example/x"]
    assert deduped == ["https://real.example/y"]


def test_dedup_normalizes_for_match():
    """A URL with utm tracking still matches a recently-seen bare URL."""
    text = "take. https://example.com/foo?utm_source=z"
    cleaned, _, deduped = enforce_allowlist(
        text,
        allowlist=["https://example.com/foo"],
        recently_seen=["https://example.com/foo"],
    )
    assert "example.com" not in cleaned
    assert deduped == ["https://example.com/foo?utm_source=z"]


def test_dedup_none_means_no_dedup():
    """If recently_seen is None, deduped is always empty."""
    text = "take. https://example.com/foo"
    cleaned, _, deduped = enforce_allowlist(
        text, allowlist=["https://example.com/foo"], recently_seen=None,
    )
    assert "https://example.com/foo" in cleaned
    assert deduped == []


# ---- enforce_source_links (high-level helper) ---------------------------------


def test_source_links_combines_feed_perplexity_websearch():
    text = (
        "three takes.\n"
        "https://feed.example/a\n"
        "https://pplx.example/b\n"
        "https://search.example/c"
    )
    pplx_context = "SOURCES:\n  [1] https://pplx.example/b"
    cleaned, rejected, _ = enforce_source_links(
        text,
        feed_urls=["https://feed.example/a"],
        perplexity_context=pplx_context,
        web_search_urls=["https://search.example/c"],
    )
    assert rejected == []
    for u in ("feed.example", "pplx.example", "search.example"):
        assert u in cleaned


def test_source_links_strips_hallucinated_url():
    text = "take.\nhttps://hallucinated.example/x"
    cleaned, rejected, _ = enforce_source_links(
        text, feed_urls=["https://real.example/a"],
    )
    assert "hallucinated" not in cleaned
    assert rejected == ["https://hallucinated.example/x"]


def test_source_links_dedups_recently_seen():
    """recently_seen URLs are added to allowlist but still stripped."""
    text = "yeah that's the one.\nhttps://example.com/foo"
    cleaned, rejected, deduped = enforce_source_links(
        text,
        feed_urls=[],
        recently_seen_urls=["https://example.com/foo"],
    )
    assert "example.com" not in cleaned
    # recently_seen is in allowlist, so this isn't a rejection.
    assert rejected == []
    assert deduped == ["https://example.com/foo"]


def test_source_links_recently_seen_does_not_block_distinct_url():
    """Other URLs in the allowlist are unaffected by dedup."""
    text = "compare.\nhttps://other.example/y"
    cleaned, _, deduped = enforce_source_links(
        text,
        feed_urls=["https://other.example/y"],
        recently_seen_urls=["https://example.com/foo"],
    )
    assert "https://other.example/y" in cleaned
    assert deduped == []


def test_source_links_no_sources_strips_all():
    text = "wild take.\nhttps://example.com/foo"
    cleaned, rejected, _ = enforce_source_links(text)
    assert "example.com" not in cleaned
    assert rejected == ["https://example.com/foo"]


def test_source_links_market_urls_in_allowlist():
    """Market URLs from MarketSnapshot.url are real and must pass the guardrail."""
    text = (
        "polymarket has it at 38%. https://polymarket.com/event/drake-july"
    )
    cleaned, rejected, _ = enforce_source_links(
        text,
        market_urls=["https://polymarket.com/event/drake-july"],
    )
    assert "polymarket.com/event/drake-july" in cleaned
    assert rejected == []


def test_source_links_market_urls_combined_with_other_sources():
    """Market URLs add to the allowlist alongside feed/pplx/web_search/recently_seen."""
    text = (
        "stack of takes.\n"
        "https://feed.example/a\n"
        "https://polymarket.com/event/x\n"
        "https://kalshi.com/markets/PRES2028\n"
        "https://hallucinated.example/x"
    )
    cleaned, rejected, _ = enforce_source_links(
        text,
        feed_urls=["https://feed.example/a"],
        market_urls=[
            "https://polymarket.com/event/x",
            "https://kalshi.com/markets/PRES2028",
        ],
    )
    assert "feed.example" in cleaned
    assert "polymarket.com/event/x" in cleaned
    assert "kalshi.com/markets/PRES2028" in cleaned
    assert "hallucinated" not in cleaned
    assert rejected == ["https://hallucinated.example/x"]


def test_source_links_market_urls_none_or_empty_no_change():
    """Passing None/empty for market_urls behaves like the old call shape."""
    text = "take.\nhttps://hallucinated.example/x"
    cleaned_none, rejected_none, _ = enforce_source_links(
        text, market_urls=None,
    )
    cleaned_empty, rejected_empty, _ = enforce_source_links(
        text, market_urls=[],
    )
    assert cleaned_none == cleaned_empty
    assert rejected_none == rejected_empty == ["https://hallucinated.example/x"]


# ---- prefix_bare_urls ---------------------------------------------------------


def test_prefix_bare_urls_adds_https():
    text = "check this www.scrippsnews.com/entertainment/article"
    assert prefix_bare_urls(text) == (
        "check this https://www.scrippsnews.com/entertainment/article"
    )


def test_prefix_bare_urls_ignores_already_prefixed():
    text = "check https://www.example.com/foo"
    assert prefix_bare_urls(text) == text


def test_prefix_bare_urls_multiple():
    text = "www.a.com/x and www.b.com/y"
    result = prefix_bare_urls(text)
    assert "https://www.a.com/x" in result
    assert "https://www.b.com/y" in result


def test_prefix_bare_urls_start_of_text():
    text = "www.example.com/path"
    assert prefix_bare_urls(text) == "https://www.example.com/path"


# ---- enforce_source_links with bare URLs --------------------------------------


def test_source_links_bare_www_url_gets_prefixed_and_allowed():
    """A bare www URL in the output is prefixed with https:// and matched
    against the allowlist, so it passes through clickable."""
    text = "court tossed it. www.scrippsnews.com/article"
    pplx_context = "SOURCES:\n  [1] https://www.scrippsnews.com/article"
    cleaned, rejected, _ = enforce_source_links(
        text, perplexity_context=pplx_context,
    )
    assert "https://www.scrippsnews.com/article" in cleaned
    assert rejected == []


def test_source_links_bare_www_in_perplexity_context():
    """If Perplexity context itself has bare www URLs, they still get
    extracted and added to the allowlist."""
    text = "take. https://www.example.com/story"
    pplx_context = "SOURCES:\n  [1] www.example.com/story"
    cleaned, rejected, _ = enforce_source_links(
        text, perplexity_context=pplx_context,
    )
    assert "https://www.example.com/story" in cleaned
    assert rejected == []


# ---- ensure_protocol ----------------------------------------------------------


def test_ensure_protocol_bare_www():
    assert ensure_protocol("www.example.com/foo") == "https://www.example.com/foo"


def test_ensure_protocol_bare_no_www():
    assert ensure_protocol("scrippsnews.com/article") == "https://scrippsnews.com/article"


def test_ensure_protocol_already_https():
    assert ensure_protocol("https://example.com/foo") == "https://example.com/foo"


def test_ensure_protocol_already_http():
    assert ensure_protocol("http://example.com/foo") == "http://example.com/foo"


# ---- verify_live_links --------------------------------------------------------


@pytest.mark.asyncio
async def test_verify_live_links_no_twitter_urls_skips_network():
    """Non-Twitter URLs short-circuit without calling fxtwitter."""
    text = "fire take. https://pitchfork.com/news/some-article"
    with patch(
        "utils.link_enrich.verify_twitter_alive",
        AsyncMock(side_effect=AssertionError("should not be called")),
    ):
        cleaned, dead = await verify_live_links(text)
    assert cleaned == text
    assert dead == []


@pytest.mark.asyncio
async def test_verify_live_links_all_alive_leaves_text_unchanged():
    text = "the post is fire. https://x.com/foo/status/123"
    with patch("utils.link_enrich.verify_twitter_alive", AsyncMock(return_value=True)):
        cleaned, dead = await verify_live_links(text)
    assert cleaned == text
    assert dead == []


@pytest.mark.asyncio
async def test_verify_live_links_strips_confirmed_dead_url():
    """Confirmed 404 URL is stripped; surrounding prose stays intact."""
    text = "the post is fire.\nhttps://fxtwitter.com/DiscussingFilm/status/999"
    with patch("utils.link_enrich.verify_twitter_alive", AsyncMock(return_value=False)):
        cleaned, dead = await verify_live_links(text)
    assert "fxtwitter.com" not in cleaned
    assert cleaned == "the post is fire."
    assert dead == ["https://fxtwitter.com/DiscussingFilm/status/999"]


@pytest.mark.asyncio
async def test_verify_live_links_strips_only_dead_in_mixed_set():
    text = (
        "two takes.\nhttps://x.com/a/status/111\nhttps://x.com/b/status/222"
    )

    async def fake(url: str) -> bool:
        return "222" not in url  # 222 is dead, 111 is alive

    with patch("utils.link_enrich.verify_twitter_alive", AsyncMock(side_effect=fake)):
        cleaned, dead = await verify_live_links(text)
    assert "111" in cleaned
    assert "222" not in cleaned
    assert dead == ["https://x.com/b/status/222"]


@pytest.mark.asyncio
async def test_verify_live_links_exception_fails_open():
    """An exception from the verifier must not strip the URL."""
    text = "take. https://x.com/foo/status/123"
    with patch(
        "utils.link_enrich.verify_twitter_alive",
        AsyncMock(side_effect=RuntimeError("boom")),
    ):
        cleaned, dead = await verify_live_links(text)
    assert "status/123" in cleaned
    assert dead == []


@pytest.mark.asyncio
async def test_verify_live_links_empty_text():
    cleaned, dead = await verify_live_links("")
    assert cleaned == ""
    assert dead == []
