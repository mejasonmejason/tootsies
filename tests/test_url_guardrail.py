"""Tests for utils.url_guardrail, the hallucination + dedup catch."""

from __future__ import annotations

from dataclasses import dataclass

from utils.url_guardrail import (
    enforce_allowlist,
    enforce_source_links,
    ensure_market_citation,
    extract_urls,
    normalize,
)


@dataclass
class _Snap:
    """Minimal snapshot stub for ensure_market_citation tests."""
    title: str
    url: str

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


# ---- ensure_market_citation -------------------------------------------------


def test_market_citation_appends_when_response_has_no_url():
    """Mechanical safety net: append a market URL when none is present."""
    snaps = [_Snap("Lakers @ Spurs", "https://polymarket.com/event/lakers-spurs")]
    out = ensure_market_citation("Lakers tonight, take the points.", snaps)
    assert "https://polymarket.com/event/lakers-spurs" in out


def test_market_citation_noop_when_response_already_has_market_url():
    """If the response already cites one of the snapshot URLs, no-op."""
    snaps = [_Snap("Lakers", "https://polymarket.com/event/lakers")]
    text = "take em. https://polymarket.com/event/lakers"
    assert ensure_market_citation(text, snaps) == text


def test_market_citation_respects_recently_seen_urls():
    """Don't repost a URL the user just saw, even via the mechanical append."""
    snaps = [_Snap("Lakers", "https://polymarket.com/event/lakers")]
    out = ensure_market_citation(
        "Lakers tonight, take em.",
        snaps,
        recently_seen_urls=["https://polymarket.com/event/lakers"],
    )
    # URL was in recently_seen, no append.
    assert "polymarket.com" not in out


def test_market_citation_picks_unseen_snapshot_when_one_seen():
    """If snapshot A was seen but snapshot B wasn't, append B."""
    snaps = [
        _Snap("Lakers", "https://polymarket.com/event/lakers"),
        _Snap("Warriors", "https://polymarket.com/event/warriors"),
    ]
    out = ensure_market_citation(
        "Warriors tonight",
        snaps,
        recently_seen_urls=["https://polymarket.com/event/lakers"],
    )
    assert "https://polymarket.com/event/warriors" in out
    assert "polymarket.com/event/lakers" not in out


def test_market_citation_noop_when_no_snapshots():
    assert ensure_market_citation("take it", None) == "take it"
    assert ensure_market_citation("take it", []) == "take it"


def test_market_citation_title_match_picks_relevant_snapshot():
    """If response mentions a snapshot's title, that snapshot's URL is used."""
    snaps = [
        _Snap("Lakers", "https://polymarket.com/event/lakers"),
        _Snap("Warriors", "https://polymarket.com/event/warriors"),
    ]
    out = ensure_market_citation("warriors covering tonight", snaps)
    assert "https://polymarket.com/event/warriors" in out
    # Lakers URL not appended even though it's the first snapshot.
    assert "polymarket.com/event/lakers" not in out
