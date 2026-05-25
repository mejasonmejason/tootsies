"""Tests for utils.url_guardrail, the hallucination + dedup catch."""

from __future__ import annotations

from utils.url_guardrail import (
    enforce_allowlist,
    enforce_source_links,
    extract_urls,
    normalize,
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
