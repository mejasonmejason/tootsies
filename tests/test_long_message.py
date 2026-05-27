"""Tests for utils.long_message: truncation logic."""

from __future__ import annotations

from utils.long_message import DISCORD_MAX, truncate


class TestTruncate:
    def test_short_text_unchanged(self) -> None:
        assert truncate("hello") == "hello"

    def test_exact_limit_unchanged(self) -> None:
        text = "a" * DISCORD_MAX
        assert truncate(text) == text

    def test_cuts_at_last_newline(self) -> None:
        text = "line one\nline two\nline three"
        result = truncate(text, limit=20)
        assert result == "line one\nline two"

    def test_cuts_at_last_space_when_no_newline(self) -> None:
        text = "word " * 50
        result = truncate(text, limit=30)
        assert len(result) <= 30
        assert not result.endswith(" ")

    def test_hard_cut_when_no_break(self) -> None:
        text = "a" * 100
        result = truncate(text, limit=50)
        assert result == "a" * 50
