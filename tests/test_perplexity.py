"""Tests for utils.perplexity, the Perplexity Sonar search client."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from utils.perplexity import (
    PerplexityClient,
    build_search_query,
    format_perplexity_for_prompt,
    is_hedged,
)

# ---- build_search_query -------------------------------------------------------


def test_build_query_ask():
    q = build_search_query("is drake done", surface="ask")
    assert "drake" in q.lower()


def test_build_query_ask_leads_with_fact_verification():
    """Ask queries must instruct Perplexity to verify facts before discourse,
    so verifiable counts/records ("how many #1s") come back grounded in
    authoritative sources rather than ambient social chatter."""
    q = build_search_query("how many number 1 hot 100s does drake have", surface="ask")
    fact_terms = ("verifiable", "authoritative", "wikipedia", "billboard")
    assert any(t in q.lower() for t in fact_terms), (
        f"ask query should signal fact verification, got: {q}"
    )


def test_build_query_discourse_with_category():
    q = build_search_query("", surface="discourse", category="nba")
    assert "nba" in q.lower()


def test_build_query_discourse_each_category():
    for cat in ("nba", "sports", "hiphop", "pop", "cinema"):
        q = build_search_query("", surface="discourse", category=cat)
        assert cat in q.lower() or len(q) > 50


def test_build_query_discourse_channel_name_passed_as_context():
    q = build_search_query("", surface="discourse", channel_name="nba-talk")
    assert "nba-talk" in q.lower()


def test_build_query_discourse_no_context_gets_trending():
    q = build_search_query("", surface="discourse")
    assert "trending" in q.lower()


def test_build_query_discourse_channel_topic_preferred_over_name():
    q = build_search_query(
        "", surface="discourse",
        channel_name="screening-room", channel_topic="movies, tv, film talk",
    )
    assert "movies, tv, film talk" in q.lower()
    assert "screening-room" not in q.lower()


def test_build_query_discourse_explicit_category_beats_channel_topic():
    q = build_search_query(
        "", surface="discourse",
        category="nba", channel_topic="movies, tv, film talk",
    )
    assert "film talk" not in q.lower()


def test_build_query_recap():
    q = build_search_query("lebron trade rumors", surface="recap")
    assert "lebron" in q.lower()


def test_build_query_chimein():
    q = build_search_query("kendrick dropped", surface="chimein")
    assert "kendrick" in q.lower()


def test_build_query_unknown_surface():
    q = build_search_query("something", surface="new_thing")
    assert "trending" in q.lower()


# ---- format_perplexity_for_prompt ----------------------------------------------


def test_format_prompt_includes_header():
    result = format_perplexity_for_prompt("drake is beefing with kendrick")
    assert "REAL-TIME SEARCH CONTEXT" in result
    assert "drake is beefing with kendrick" in result
    assert "Perplexity" in result


def test_format_prompt_flags_block_as_fact_authoritative():
    """The block must read to Claude as ground-truth-for-this-question, not
    just 'vibes'. Otherwise the model falls back to stale training memory on
    specific counts/records that the verified context contradicts."""
    result = format_perplexity_for_prompt("drake has 14 number ones on the hot 100")
    lower = result.lower()
    # Some phrasing that nudges the model to prefer the verified value over
    # what training data remembers.
    fact_signals = ("ground truth", "verbatim", "wins", "stale", "override")
    assert any(s in lower for s in fact_signals), (
        f"format header should signal fact-authority over training memory, got: {result}"
    )


# ---- is_hedged -----------------------------------------------------------------


def test_is_hedged_detects_punt():
    assert is_hedged("I can't verify live trends right now.")
    assert is_hedged("Results are mostly youtube mixes and playlist pages.")


def test_is_hedged_passes_real_answer():
    assert not is_hedged("Chxrry22 dropped a new single this week and it's charting.")


def test_is_hedged_ignores_sources_block():
    """A marker word inside a citation URL must not trip the flag."""
    text = (
        "Drake has 13 number ones.\n\nSOURCES:\n"
        "  [1] https://example.com/cannot-verify-this-slug"
    )
    assert not is_hedged(text)


# ---- PerplexityClient ----------------------------------------------------------


@pytest.fixture
def client():
    return PerplexityClient("test-api-key")


async def test_search_success(client: PerplexityClient):
    mock_resp = AsyncMock()
    mock_resp.status = 200
    mock_resp.json = AsyncMock(return_value={
        "choices": [{"message": {"content": "Drake is trending"}}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 20},
    })
    mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
    mock_resp.__aexit__ = AsyncMock(return_value=False)

    mock_session = AsyncMock()
    mock_session.post = MagicMock(return_value=mock_resp)
    mock_session.closed = False

    client._session = mock_session
    result = await client.search("what's trending", purpose="ask")
    assert result == "Drake is trending"


def _ok_session(content: str = "trending") -> MagicMock:
    """A mock session whose .post returns a 200 with the given content."""
    mock_resp = AsyncMock()
    mock_resp.status = 200
    mock_resp.json = AsyncMock(return_value={
        "choices": [{"message": {"content": content}}],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1},
    })
    mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
    mock_resp.__aexit__ = AsyncMock(return_value=False)
    mock_session = MagicMock()
    mock_session.post = MagicMock(return_value=mock_resp)
    mock_session.closed = False
    return mock_session


def _posted_payload(mock_session: MagicMock) -> dict:
    return mock_session.post.call_args.kwargs["json"]


async def test_search_sets_medium_context_by_default(client: PerplexityClient):
    """Every surface gets search_context_size=medium so Sonar does deeper
    retrieval than its shallow 'low' default (the root cause of evergreen
    filler / 'can't verify' hedging)."""
    client._session = _ok_session()
    await client.search("q", purpose="ask")
    payload = _posted_payload(client._session)
    assert payload["web_search_options"]["search_context_size"] == "medium"


async def test_search_ask_has_no_recency_filter(client: PerplexityClient):
    """ask leads with evergreen fact verification (record counts, chart totals)
    that live on authoritative pages, so it must NOT clamp to a recency window."""
    client._session = _ok_session()
    await client.search("how many #1s does drake have", purpose="ask")
    assert "search_recency_filter" not in _posted_payload(client._session)


async def test_search_discourse_uses_week_recency(client: PerplexityClient):
    client._session = _ok_session()
    await client.search("q", purpose="discourse")
    assert _posted_payload(client._session)["search_recency_filter"] == "week"


async def test_search_music_uses_month_recency(client: PerplexityClient):
    """Music curation has a longer half-life than sports/pop discourse, so it
    gets a wider month window (still excludes evergreen filler) rather than the
    7-day novelty bias of week."""
    client._session = _ok_session()
    await client.search("q", purpose="music")
    assert _posted_payload(client._session)["search_recency_filter"] == "month"


async def test_search_recap_and_chimein_use_day_recency(client: PerplexityClient):
    for purpose in ("recap", "chimein"):
        client._session = _ok_session()
        await client.search("q", purpose=purpose)
        assert _posted_payload(client._session)["search_recency_filter"] == "day"


async def test_search_unknown_purpose_gets_default_config(client: PerplexityClient):
    """Unknown purposes fall back to medium context, no recency window."""
    client._session = _ok_session()
    await client.search("q", purpose="brand_new_surface")
    payload = _posted_payload(client._session)
    assert payload["web_search_options"]["search_context_size"] == "medium"
    assert "search_recency_filter" not in payload


async def test_search_explicit_overrides_win(client: PerplexityClient):
    """The eval harness can override context size and disable recency."""
    client._session = _ok_session()
    await client.search(
        "q", purpose="music", search_context_size="high", recency=None,
    )
    payload = _posted_payload(client._session)
    assert payload["web_search_options"]["search_context_size"] == "high"
    assert "search_recency_filter" not in payload


async def test_search_explicit_recency_override(client: PerplexityClient):
    client._session = _ok_session()
    await client.search("q", purpose="ask", recency="hour")
    assert _posted_payload(client._session)["search_recency_filter"] == "hour"


async def test_search_http_error(client: PerplexityClient):
    mock_resp = AsyncMock()
    mock_resp.status = 500
    mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
    mock_resp.__aexit__ = AsyncMock(return_value=False)

    mock_session = AsyncMock()
    mock_session.post = MagicMock(return_value=mock_resp)
    mock_session.closed = False

    client._session = mock_session
    result = await client.search("test", purpose="ask")
    assert result is None


async def test_search_empty_choices(client: PerplexityClient):
    mock_resp = AsyncMock()
    mock_resp.status = 200
    mock_resp.json = AsyncMock(return_value={
        "choices": [],
        "usage": {},
    })
    mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
    mock_resp.__aexit__ = AsyncMock(return_value=False)

    mock_session = AsyncMock()
    mock_session.post = MagicMock(return_value=mock_resp)
    mock_session.closed = False

    client._session = mock_session
    result = await client.search("test", purpose="ask")
    assert result is None


async def test_search_network_error(client: PerplexityClient):
    import aiohttp

    mock_session = AsyncMock()
    mock_session.post = MagicMock(side_effect=aiohttp.ClientError("timeout"))
    mock_session.closed = False

    client._session = mock_session
    result = await client.search("test", purpose="ask")
    assert result is None


async def test_close_session(client: PerplexityClient):
    mock_session = AsyncMock()
    mock_session.closed = False
    client._session = mock_session
    await client.close()
    mock_session.close.assert_awaited_once()
    assert client._session is None


async def test_close_no_session(client: PerplexityClient):
    await client.close()  # should not raise


async def test_search_normalizes_bare_citation_urls(client: PerplexityClient):
    """Citation URLs without a protocol get https:// prepended."""
    mock_resp = AsyncMock()
    mock_resp.status = 200
    mock_resp.json = AsyncMock(return_value={
        "choices": [{"message": {"content": "Court tossed it"}}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5},
        "citations": ["www.scrippsnews.com/entertainment/article"],
    })
    mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
    mock_resp.__aexit__ = AsyncMock(return_value=False)

    mock_session = AsyncMock()
    mock_session.post = MagicMock(return_value=mock_resp)
    mock_session.closed = False

    client._session = mock_session
    result = await client.search("drake lawsuit", purpose="ask")
    assert result is not None
    assert "https://www.scrippsnews.com/entertainment/article" in result
