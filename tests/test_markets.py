"""Tests for utils.markets, the SGO + Polymarket + Kalshi enricher layer."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from utils.markets import (
    KalshiClient,
    MarketSnapshot,
    PolymarketClient,
    SportsGameOddsClient,
    format_markets_for_prompt,
)

# ---- helpers ---------------------------------------------------------------


def _mock_resp(status: int = 200, payload: Any = None) -> AsyncMock:
    """Build a fake aiohttp response object that supports `async with`."""
    resp = AsyncMock()
    resp.status = status
    resp.json = AsyncMock(return_value=payload)
    resp.__aenter__ = AsyncMock(return_value=resp)
    resp.__aexit__ = AsyncMock(return_value=False)
    return resp


def _mock_session(resp: AsyncMock) -> MagicMock:
    sess = MagicMock()
    sess.get = MagicMock(return_value=resp)
    return sess


# ---- SportsGameOddsClient --------------------------------------------------


def test_sgo_disabled_when_no_key():
    client = SportsGameOddsClient(api_key=None)
    assert client.enabled is False


def test_sgo_enabled_when_key():
    client = SportsGameOddsClient(api_key="test")
    assert client.enabled is True


async def test_sgo_returns_none_when_disabled():
    client = SportsGameOddsClient(api_key=None)
    result = await client.get_event_odds("NBA")
    assert result is None


async def test_sgo_parses_event():
    client = SportsGameOddsClient(api_key="test")
    payload = {
        "data": [
            {
                "eventID": "evt_1",
                "teams": {"home": {"name": "Lakers"}, "away": {"name": "Warriors"}},
                "odds": {
                    "spread": -3.5,
                    "moneyline_home": -150,
                    "moneyline_away": 130,
                    "total": 225.5,
                },
            },
        ],
    }
    with patch(
        "utils.markets._get_session",
        AsyncMock(return_value=_mock_session(_mock_resp(200, payload))),
    ):
        result = await client.get_event_odds("NBA")
    assert result is not None
    assert len(result) == 1
    snap = result[0]
    assert snap.source == "sgo"
    assert "Warriors" in snap.title and "Lakers" in snap.title
    assert snap.odds["spread"] == -3.5
    assert snap.odds["total"] == 225.5
    assert "evt_1" in snap.url


async def test_sgo_http_error_returns_none():
    client = SportsGameOddsClient(api_key="test")
    with patch(
        "utils.markets._get_session",
        AsyncMock(return_value=_mock_session(_mock_resp(500))),
    ):
        result = await client.get_event_odds("MLB")
    assert result is None


async def test_sgo_empty_data_returns_empty_list():
    client = SportsGameOddsClient(api_key="test")
    with patch(
        "utils.markets._get_session",
        AsyncMock(return_value=_mock_session(_mock_resp(200, {"data": []}))),
    ):
        result = await client.get_event_odds("NHL")
    assert result == []


async def test_sgo_unparseable_data_returns_none():
    client = SportsGameOddsClient(api_key="test")
    with patch(
        "utils.markets._get_session",
        AsyncMock(return_value=_mock_session(_mock_resp(200, "not a dict"))),
    ):
        result = await client.get_event_odds("CBB")
    assert result is None


# ---- PolymarketClient ------------------------------------------------------


async def test_polymarket_trending_parses_events():
    client = PolymarketClient()
    payload = [
        {
            "title": "Will Drake drop an album before July?",
            "slug": "drake-album-july",
            "volume": 125000,
            "liquidity": 8000,
            "endDate": "2026-07-01T00:00:00Z",
            "markets": [
                {"outcomePrices": ["0.38", "0.62"], "bestBid": 0.37, "bestAsk": 0.39},
            ],
        },
        {
            "title": "Will Lakers make the playoffs?",
            "slug": "lakers-playoffs",
            "markets": [{"outcomePrices": "[\"0.71\", \"0.29\"]"}],
        },
    ]
    with patch(
        "utils.markets._get_session",
        AsyncMock(return_value=_mock_session(_mock_resp(200, payload))),
    ):
        result = await client.get_trending_events(limit=5)
    assert result is not None
    assert len(result) == 2
    assert result[0].source == "polymarket"
    assert result[0].probability == pytest.approx(0.38)
    assert "drake-album-july" in result[0].url
    # String-encoded outcomePrices should still parse.
    assert result[1].probability == pytest.approx(0.71)


async def test_polymarket_search_parses_results():
    client = PolymarketClient()
    payload = {
        "events": [
            {
                "title": "Trump indictment by year end",
                "slug": "trump-indictment",
                "markets": [{"outcomePrices": ["0.15", "0.85"]}],
            },
        ],
    }
    with patch(
        "utils.markets._get_session",
        AsyncMock(return_value=_mock_session(_mock_resp(200, payload))),
    ):
        result = await client.search_markets("trump")
    assert result is not None
    assert len(result) == 1
    assert result[0].probability == pytest.approx(0.15)


async def test_polymarket_http_error_returns_none():
    client = PolymarketClient()
    with patch(
        "utils.markets._get_session",
        AsyncMock(return_value=_mock_session(_mock_resp(503))),
    ):
        result = await client.get_trending_events()
    assert result is None


async def test_polymarket_skips_event_without_title():
    client = PolymarketClient()
    payload = [
        {"slug": "no-title-event", "markets": [{"outcomePrices": ["0.5", "0.5"]}]},
        {"title": "Real event", "slug": "real", "markets": []},
    ]
    with patch(
        "utils.markets._get_session",
        AsyncMock(return_value=_mock_session(_mock_resp(200, payload))),
    ):
        result = await client.get_trending_events()
    assert result is not None
    assert len(result) == 1
    assert result[0].title == "Real event"


# ---- KalshiClient ----------------------------------------------------------


async def test_kalshi_parses_markets():
    client = KalshiClient()
    payload = {
        "markets": [
            {
                "ticker": "PRES2028-DJT",
                "title": "Trump wins 2028 election",
                "yes_bid_dollars": 0.30,
                "yes_ask_dollars": 0.34,
                "volume_fp": 50000,
                "liquidity_dollars": 12000,
                "expiration_time": "2028-11-04T00:00:00Z",
            },
        ],
    }
    with patch(
        "utils.markets._get_session",
        AsyncMock(return_value=_mock_session(_mock_resp(200, payload))),
    ):
        result = await client.get_open_markets()
    assert result is not None
    assert len(result) == 1
    snap = result[0]
    assert snap.source == "kalshi"
    # Midpoint of bid/ask
    assert snap.probability == pytest.approx(0.32)
    assert "PRES2028-DJT" in snap.url
    assert snap.meta["ticker"] == "PRES2028-DJT"


async def test_kalshi_handles_only_bid():
    client = KalshiClient()
    payload = {
        "markets": [
            {
                "ticker": "T",
                "title": "Bid-only market",
                "yes_bid_dollars": 0.42,
            },
        ],
    }
    with patch(
        "utils.markets._get_session",
        AsyncMock(return_value=_mock_session(_mock_resp(200, payload))),
    ):
        result = await client.get_open_markets()
    assert result is not None
    assert result[0].probability == pytest.approx(0.42)


async def test_kalshi_http_error_returns_none():
    client = KalshiClient()
    with patch(
        "utils.markets._get_session",
        AsyncMock(return_value=_mock_session(_mock_resp(500))),
    ):
        result = await client.get_open_markets()
    assert result is None


# ---- format_markets_for_prompt ---------------------------------------------


def test_format_empty():
    assert format_markets_for_prompt([]) == ""


def test_format_sgo_snapshot():
    snap = MarketSnapshot(
        source="sgo",
        title="Warriors @ Lakers",
        url="https://sportsgameodds.com/event/evt_1",
        odds={"spread": -3.5, "total": 225.5},
    )
    out = format_markets_for_prompt([snap])
    assert "MARKET CONTEXT" in out
    assert "[SGO]" in out
    assert "spread=-3.5" in out
    assert "evt_1" in out


def test_format_polymarket_snapshot():
    snap = MarketSnapshot(
        source="polymarket",
        title="Will Drake drop in July?",
        url="https://polymarket.com/event/drake",
        probability=0.38,
        meta={"volume": 125000.0},
    )
    out = format_markets_for_prompt([snap])
    assert "[Polymarket]" in out
    assert "yes=38%" in out
    assert "vol $125,000" in out


def test_format_kalshi_snapshot():
    snap = MarketSnapshot(
        source="kalshi",
        title="Trump 2028",
        url="https://kalshi.com/markets/PRES2028",
        probability=0.32,
    )
    out = format_markets_for_prompt([snap])
    assert "[Kalshi]" in out
    assert "yes=32%" in out


def test_format_missing_probability_shows_question_mark():
    snap = MarketSnapshot(
        source="polymarket",
        title="Mystery market",
        url="https://polymarket.com/event/mystery",
        probability=None,
    )
    out = format_markets_for_prompt([snap])
    assert "yes=?" in out
