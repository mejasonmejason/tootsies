"""Tests for utils.dedup, the discourse / chimein repeat-post gate."""

from __future__ import annotations

from utils.dedup import is_duplicate_of_recent


def test_empty_line_not_duplicate() -> None:
    assert is_duplicate_of_recent("", ["some prior topic"]) is False


def test_no_recent_topics() -> None:
    assert is_duplicate_of_recent("a fresh take", []) is False


def test_similar_text_is_duplicate() -> None:
    prior = "lakers beat the nuggets in game 7, series over"
    assert is_duplicate_of_recent(
        "lakers beat the nuggets in game 7, the series is over", [prior]
    )


def test_distinct_text_not_duplicate() -> None:
    prior = "lakers beat the nuggets in game 7"
    assert is_duplicate_of_recent("kendrick dropped a surprise album tonight", [prior]) is False


def test_same_url_different_wording_is_duplicate() -> None:
    """The screening-room/main-stage case: two takes, one link, different words."""
    prior = (
        "drake debuted #1 and #2 on the hot 100 the same week. last time someone "
        "went 1-2 on debut?\nhttps://fxtwitter.com/DrakeDirect/status/2060342033940705422"
    )
    new = (
        "drake didn't just go 1-2 on the hot 100. he went 1, 2, 3, and 4. iceman "
        "season is not a metaphor.\nhttps://fxtwitter.com/DrakeDirect/status/2060342033940705422"
    )
    assert is_duplicate_of_recent(new, [prior])


def test_url_host_alias_folds_to_same_post() -> None:
    """fxtwitter and twitter point at the same tweet, so they dedup."""
    prior = "old take\nhttps://twitter.com/DrakeDirect/status/2060342033940705422"
    new = "totally different words\nhttps://fxtwitter.com/DrakeDirect/status/2060342033940705422"
    assert is_duplicate_of_recent(new, [prior])


def test_url_x_and_twitter_and_fixers_all_fold_together() -> None:
    """twitter.com is retired -> everything Twitter/X folds to one canonical host."""
    prior = "take\nhttps://x.com/DrakeDirect/status/2060342033940705422"
    for host in ("twitter.com", "fxtwitter.com", "vxtwitter.com", "fixupx.com"):
        new = (
            "a totally unrelated sentence with different wording\n"
            f"https://{host}/DrakeDirect/status/2060342033940705422"
        )
        assert is_duplicate_of_recent(new, [prior]), host


def test_url_query_and_trailing_punct_ignored() -> None:
    prior = "take one\nhttps://news.example/article"
    new = "take two, unrelated phrasing\n(https://news.example/article?utm=x)."
    assert is_duplicate_of_recent(new, [prior])


def test_different_urls_not_duplicate() -> None:
    prior = "take one\nhttps://news.example/article-a"
    new = "take one\nhttps://news.example/article-b"
    # Same wording would still trip the text gate; use distinct text here so
    # only the URL signal is under test.
    new = "completely unrelated sentence about something else\nhttps://news.example/article-b"
    assert is_duplicate_of_recent(new, [prior]) is False
