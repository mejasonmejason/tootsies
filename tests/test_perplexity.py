"""Tests for utils.perplexity, the Perplexity Sonar search client."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from utils.perplexity import (
    PerplexityClient,
    _infer_category_from_channel,
    build_search_query,
    format_perplexity_for_prompt,
)

# ---- build_search_query -------------------------------------------------------


def test_build_query_ask():
    q = build_search_query("is drake done", surface="ask")
    assert "drake" in q.lower()


def test_build_query_discourse_with_category():
    q = build_search_query("", surface="discourse", category="nba")
    assert "nba" in q.lower()
    assert "twitter" in q.lower() or "x" in q.lower()


def test_build_query_discourse_each_category():
    for cat in ("nba", "sports", "hiphop", "pop", "cinema"):
        q = build_search_query("", surface="discourse", category=cat)
        assert cat in q.lower() or len(q) > 50


def test_build_query_discourse_infers_from_channel():
    q = build_search_query("", surface="discourse", channel_name="nba-talk")
    assert "nba" in q.lower()


def test_build_query_discourse_unknown_channel_gets_trending():
    q = build_search_query("", surface="discourse", channel_name="general")
    assert "trending" in q.lower()


def test_build_query_discourse_no_context_gets_trending():
    q = build_search_query("", surface="discourse")
    assert "trending" in q.lower()


def test_build_query_recap():
    q = build_search_query("lebron trade rumors", surface="recap")
    assert "lebron" in q.lower()


def test_build_query_chimein():
    q = build_search_query("kendrick dropped", surface="chimein")
    assert "kendrick" in q.lower()


def test_build_query_unknown_surface():
    q = build_search_query("something", surface="new_thing")
    assert "trending" in q.lower()


# ---- _infer_category_from_channel ----------------------------------------------


def test_infer_nba():
    assert _infer_category_from_channel("nba-talk") == "nba"
    assert _infer_category_from_channel("basketball-chat") == "nba"


def test_infer_sports():
    assert _infer_category_from_channel("sports-general") == "sports"
    assert _infer_category_from_channel("nfl-picks") == "sports"


def test_infer_hiphop():
    assert _infer_category_from_channel("hip-hop") == "hiphop"
    assert _infer_category_from_channel("rap-drops") == "hiphop"


def test_infer_cinema():
    assert _infer_category_from_channel("movie-night") == "cinema"
    assert _infer_category_from_channel("tv-shows") == "cinema"


def test_infer_pop():
    assert _infer_category_from_channel("pop-culture") == "pop"
    assert _infer_category_from_channel("celeb-gossip") == "pop"


def test_infer_none():
    assert _infer_category_from_channel("general") is None
    assert _infer_category_from_channel("lounge") is None


# ---- format_perplexity_for_prompt ----------------------------------------------


def test_format_prompt_includes_header():
    result = format_perplexity_for_prompt("drake is beefing with kendrick")
    assert "REAL-TIME SEARCH CONTEXT" in result
    assert "drake is beefing with kendrick" in result
    assert "Perplexity" in result


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
