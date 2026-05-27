"""Tests for utils.perplexity, the Perplexity Sonar search client."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from utils.perplexity import (
    PerplexityClient,
    build_chart_fact_query,
    build_search_query,
    format_perplexity_for_prompt,
    is_chart_fact_question,
)

# ---- is_chart_fact_question ---------------------------------------------------


def test_chart_fact_most_hot_100_number_ones():
    assert is_chart_fact_question("who's the male solo artist with the most number 1 hot 100?")


def test_chart_fact_how_many_number_ones():
    assert is_chart_fact_question("how many number ones does drake have")


def test_chart_fact_how_many_hash_ones():
    assert is_chart_fact_question("how many #1s does drake have on the billboard")


def test_chart_fact_hot_100_record():
    assert is_chart_fact_question("who holds the hot 100 #1 record")


def test_chart_fact_all_time_number_one():
    assert is_chart_fact_question("drake all time hot 100 #1s count")


def test_not_chart_fact_generic_opinion():
    assert not is_chart_fact_question("is drake done")


def test_not_chart_fact_pure_chart_no_stat():
    assert not is_chart_fact_question("drake billboard chart")


def test_not_chart_fact_unrelated():
    assert not is_chart_fact_question("who's bigger travis or kendrick")


# ---- build_chart_fact_query ---------------------------------------------------


def test_chart_fact_query_includes_question():
    q = build_chart_fact_query("how many #1s does drake have")
    assert "how many #1s does drake have" in q


def test_chart_fact_query_mentions_billboard():
    q = build_chart_fact_query("who has the most hot 100 number ones")
    assert "Billboard" in q


def test_chart_fact_query_asks_for_current_data():
    q = build_chart_fact_query("drake hot 100 record")
    assert "current" in q.lower() or "up-to-date" in q.lower()


# ---- build_search_query -------------------------------------------------------


def test_build_query_ask():
    q = build_search_query("is drake done", surface="ask")
    assert "drake" in q.lower()


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
