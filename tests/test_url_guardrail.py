"""Tests for utils.url_guardrail, the discourse hallucination catch."""

from __future__ import annotations

from utils.url_guardrail import enforce_allowlist, extract_urls, normalize

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


def test_extract_urls_none():
    assert extract_urls("no urls here") == []


# ---- enforce_allowlist --------------------------------------------------------


def test_enforce_passes_url_in_allowlist():
    text = "fire take. https://example.com/a"
    cleaned, rejected = enforce_allowlist(text, ["https://example.com/a"])
    assert cleaned == "fire take. https://example.com/a"
    assert rejected == []


def test_enforce_strips_url_not_in_allowlist():
    text = "fire take.\nhttps://hallucinated.example/x"
    cleaned, rejected = enforce_allowlist(text, ["https://real.example/a"])
    assert "hallucinated" not in cleaned
    assert cleaned == "fire take."
    assert rejected == ["https://hallucinated.example/x"]


def test_enforce_keeps_real_strips_fake_in_mixed():
    text = "see https://real.example/a and https://fake.example/b end."
    cleaned, rejected = enforce_allowlist(text, ["https://real.example/a"])
    assert "https://real.example/a" in cleaned
    assert "fake.example" not in cleaned
    assert rejected == ["https://fake.example/b"]


def test_enforce_normalization_match():
    # Allowlist has trailing slash + uppercase; output URL is clean.
    text = "take. https://example.com/foo"
    cleaned, rejected = enforce_allowlist(text, ["HTTPS://EXAMPLE.COM/foo/"])
    assert rejected == []
    assert "https://example.com/foo" in cleaned


def test_enforce_normalization_strips_utm_for_match():
    text = "take. https://example.com/foo?utm_source=zzz"
    cleaned, rejected = enforce_allowlist(text, ["https://example.com/foo"])
    assert rejected == []
    assert "https://example.com/foo" in cleaned


def test_enforce_empty_allowlist_strips_all():
    text = "take. https://example.com/a"
    cleaned, rejected = enforce_allowlist(text, [])
    assert cleaned == "take."
    assert rejected == ["https://example.com/a"]


def test_enforce_no_urls_passthrough():
    text = "just a take, no link"
    cleaned, rejected = enforce_allowlist(text, ["https://example.com/a"])
    assert cleaned == "just a take, no link"
    assert rejected == []


def test_enforce_preserves_trailing_punct_in_text():
    # URL has a trailing period in the text. The period should stay in text.
    text = "wild. https://example.com/foo."
    cleaned, rejected = enforce_allowlist(text, [])
    assert "https://example.com" not in cleaned
    assert cleaned.endswith(".")
    assert rejected == ["https://example.com/foo"]


def test_enforce_collapses_blank_lines_from_strip():
    text = "take here.\n\nhttps://fake.example/a\n\nmore text"
    cleaned, rejected = enforce_allowlist(text, [])
    assert "fake.example" not in cleaned
    # No triple-blank runs.
    assert "\n\n\n" not in cleaned


def test_enforce_url_only_message_becomes_empty():
    text = "https://fake.example/x"
    cleaned, rejected = enforce_allowlist(text, [])
    assert cleaned == ""
    assert rejected == ["https://fake.example/x"]
