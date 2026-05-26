"""Tests for utils.markets, the SGO + Polymarket + Kalshi enricher layer."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from utils.markets import (
    KalshiClient,
    MarketsManager,
    MarketSnapshot,
    PolymarketClient,
    PriceHistorySummary,
    SportsGameOddsClient,
    classify_intent,
    detect_league,
    format_markets_for_prompt,
    format_price_history_for_prompt,
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


# ---- classify_intent -------------------------------------------------------


def test_classify_intent_sports_keywords():
    for q in (
        "make me a parlay for tonight",
        "what's the spread on the lakers game",
        "best NBA picks for tonight",
        "any player props you like for warriors",
        "draftkings has the line at -3",
    ):
        assert classify_intent(q) == "sports", q


def test_classify_intent_prediction_market_keywords():
    for q in (
        "what does polymarket say about the election",
        "odds of trump winning 2028",
        "kalshi has it at 40%",
        "will drake drop an album by july",
        "will the fed cut rates before december",
    ):
        assert classify_intent(q) == "prediction_market", q


def test_classify_intent_no_match():
    for q in (
        "is drake done",
        "what's the vibe in here today",
        "best taco spot in oakland",
        "",
    ):
        assert classify_intent(q) is None, q


# ---- detect_league ---------------------------------------------------------


def test_detect_league_explicit_names():
    assert detect_league("nba game tonight") == "NBA"
    assert detect_league("NFL spread") == "NFL"
    assert detect_league("the MLB playoffs") == "MLB"
    assert detect_league("nhl over under") == "NHL"
    assert detect_league("college football slate") == "CFB"


def test_detect_league_sport_names():
    assert detect_league("any good basketball games") == "NBA"
    assert detect_league("football tonight") == "NFL"


def test_detect_league_no_match():
    assert detect_league("what's a good parlay") is None
    assert detect_league("") is None


# ---- MarketsManager -------------------------------------------------------


def test_manager_init_with_sgo_key():
    m = MarketsManager(sgo_api_key="test-key")
    assert m.sgo.enabled is True
    assert m.polymarket is not None
    assert m.kalshi is not None


def test_manager_init_without_sgo_key():
    m = MarketsManager(sgo_api_key=None)
    assert m.sgo.enabled is False
    # Polymarket + Kalshi still work because they need no auth.
    assert m.polymarket is not None
    assert m.kalshi is not None


async def test_manager_returns_none_for_unrelated_query():
    m = MarketsManager(sgo_api_key="test")
    result = await m.get_context("is drake done")
    assert result is None


async def test_manager_sports_query_calls_sgo():
    m = MarketsManager(sgo_api_key="test")
    expected = [
        MarketSnapshot(source="sgo", title="A @ B", url="u", odds={"spread": -3.0}),
    ]
    m.sgo.get_event_odds = AsyncMock(return_value=expected)  # type: ignore[method-assign]
    result = await m.get_context("any good NBA parlays tonight")
    assert result == expected
    m.sgo.get_event_odds.assert_awaited_once()


async def test_manager_sports_query_skips_when_no_sgo_key():
    m = MarketsManager(sgo_api_key=None)
    result = await m.get_context("nba parlay tonight")
    assert result is None


async def test_manager_pm_query_calls_polymarket():
    m = MarketsManager(sgo_api_key=None)
    expected = [
        MarketSnapshot(source="polymarket", title="Drake", url="u", probability=0.3),
    ]
    m.polymarket.search_markets = AsyncMock(return_value=expected)  # type: ignore[method-assign]
    m.kalshi.get_open_markets = AsyncMock(return_value=[])  # type: ignore[method-assign]
    result = await m.get_context("will drake drop an album by july")
    assert result == expected
    m.polymarket.search_markets.assert_awaited_once()


async def test_manager_pm_query_falls_back_to_kalshi():
    m = MarketsManager(sgo_api_key=None)
    kalshi_snaps = [
        MarketSnapshot(source="kalshi", title="K1", url="u1", probability=0.5),
        MarketSnapshot(source="kalshi", title="K2", url="u2", probability=0.6),
        MarketSnapshot(source="kalshi", title="K3", url="u3", probability=0.7),
        MarketSnapshot(source="kalshi", title="K4", url="u4", probability=0.8),
    ]
    m.polymarket.search_markets = AsyncMock(return_value=None)  # type: ignore[method-assign]
    m.kalshi.get_open_markets = AsyncMock(return_value=kalshi_snaps)  # type: ignore[method-assign]
    result = await m.get_context("polymarket chances of the election")
    assert result is not None
    # Kalshi fallback returns top 3 only.
    assert len(result) == 3
    assert all(s.source == "kalshi" for s in result)


async def test_manager_fails_open_on_exception():
    m = MarketsManager(sgo_api_key=None)
    m.polymarket.search_markets = AsyncMock(side_effect=RuntimeError("boom"))  # type: ignore[method-assign]
    result = await m.get_context("will drake drop by july")
    assert result is None


# ---- MarketsManager with Haiku classifier injected -----------------------


async def test_manager_uses_haiku_classifier_when_injected():
    classifier = AsyncMock(return_value={
        "intent": "sports",
        "league": "NBA",
        "search_terms": "OKC Spurs game 5",
    })
    m = MarketsManager(sgo_api_key="test", intent_classifier=classifier)
    expected = [MarketSnapshot(source="sgo", title="OKC @ SAS", url="u", odds={})]
    m.sgo.get_event_odds = AsyncMock(return_value=expected)  # type: ignore[method-assign]
    # Note: query has no regex-matchable keyword, but Haiku classified it as NBA.
    result = await m.get_context("any reads on tonight")
    assert result == expected
    classifier.assert_awaited_once()
    m.sgo.get_event_odds.assert_awaited_once_with("NBA", purpose="ask")


async def test_manager_classifier_extracted_league_routes_to_league():
    classifier = AsyncMock(return_value={
        "intent": "sports",
        "league": "NFL",
        "search_terms": "chiefs ravens",
    })
    m = MarketsManager(sgo_api_key="test", intent_classifier=classifier)
    m.sgo.get_event_odds = AsyncMock(return_value=[])  # type: ignore[method-assign]
    await m.get_context("chiefs vs ravens take")
    m.sgo.get_event_odds.assert_awaited_once_with("NFL", purpose="ask")


async def test_manager_classifier_uses_search_terms_for_pm():
    classifier = AsyncMock(return_value={
        "intent": "prediction_market",
        "search_terms": "drake album july",
    })
    m = MarketsManager(sgo_api_key=None, intent_classifier=classifier)
    m.polymarket.search_markets = AsyncMock(return_value=None)  # type: ignore[method-assign]
    m.kalshi.get_open_markets = AsyncMock(return_value=[])  # type: ignore[method-assign]
    await m.get_context("anything about drake")
    m.polymarket.search_markets.assert_awaited_once_with("drake album july", limit=5)


async def test_manager_classifier_failure_falls_back_to_regex():
    """If the Haiku classifier errors, the manager should still try regex."""
    classifier = AsyncMock(side_effect=RuntimeError("haiku down"))
    m = MarketsManager(sgo_api_key="test", intent_classifier=classifier)
    expected = [MarketSnapshot(source="sgo", title="A", url="u", odds={})]
    m.sgo.get_event_odds = AsyncMock(return_value=expected)  # type: ignore[method-assign]
    # The query DOES contain regex-matchable keywords, so fallback should fire.
    result = await m.get_context("any good NBA parlays tonight")
    assert result == expected
    m.sgo.get_event_odds.assert_awaited_once()


async def test_manager_classifier_returns_none_no_fetch():
    classifier = AsyncMock(return_value=None)
    m = MarketsManager(sgo_api_key="test", intent_classifier=classifier)
    m.sgo.get_event_odds = AsyncMock()  # type: ignore[method-assign]
    m.polymarket.search_markets = AsyncMock()  # type: ignore[method-assign]
    result = await m.get_context("totally unrelated question")
    assert result is None
    m.sgo.get_event_odds.assert_not_awaited()
    m.polymarket.search_markets.assert_not_awaited()


# ---- SGO player props -----------------------------------------------------


async def test_sgo_player_props_disabled_when_no_key():
    client = SportsGameOddsClient(api_key=None)
    assert await client.get_player_props("NBA") is None


async def test_sgo_player_props_parses_props():
    client = SportsGameOddsClient(api_key="test")
    payload = {
        "data": [
            {
                "eventID": "evt_1",
                "playerProps": {
                    "lebron_pts": {
                        "player": "LeBron James",
                        "marketType": "points",
                        "line": 24.5,
                        "over": -110,
                        "under": -110,
                    },
                    "luka_ast": {
                        "player": "Luka Doncic",
                        "marketType": "assists",
                        "line": 7.5,
                        "over": -120,
                        "under": 100,
                    },
                },
            },
        ],
    }
    with patch(
        "utils.markets._get_session",
        AsyncMock(return_value=_mock_session(_mock_resp(200, payload))),
    ):
        result = await client.get_player_props("NBA")
    assert result is not None
    assert len(result) == 2
    lebron = next(s for s in result if "LeBron" in s.title)
    assert lebron.odds["line"] == 24.5
    assert lebron.odds["over"] == -110
    assert "evt_1" in lebron.url


async def test_sgo_player_props_skips_malformed():
    client = SportsGameOddsClient(api_key="test")
    payload = {
        "data": [
            {
                "eventID": "evt_1",
                "playerProps": {
                    "valid": {
                        "player": "Steph", "marketType": "threes", "line": 4.5,
                    },
                    "no_player": {"marketType": "points", "line": 20},
                    "no_line": {"player": "Joker", "marketType": "rebounds"},
                    "not_a_dict": "garbage",
                },
            },
        ],
    }
    with patch(
        "utils.markets._get_session",
        AsyncMock(return_value=_mock_session(_mock_resp(200, payload))),
    ):
        result = await client.get_player_props("NBA")
    assert result is not None
    assert len(result) == 1
    assert "Steph" in result[0].title


# ---- Polymarket multi-outcome events --------------------------------------


async def test_polymarket_multi_outcome_event_populates_outcomes():
    client = PolymarketClient()
    payload = [
        {
            "title": "2028 Presidential Election",
            "slug": "pres-2028",
            "markets": [
                {
                    "id": "m1",
                    "groupItemTitle": "Trump",
                    "outcomePrices": ["0.42", "0.58"],
                },
                {
                    "id": "m2",
                    "groupItemTitle": "Vance",
                    "outcomePrices": ["0.31", "0.69"],
                },
                {
                    "id": "m3",
                    "groupItemTitle": "Harris",
                    "outcomePrices": ["0.18", "0.82"],
                },
            ],
        },
    ]
    with patch(
        "utils.markets._get_session",
        AsyncMock(return_value=_mock_session(_mock_resp(200, payload))),
    ):
        result = await client.get_trending_events()
    assert result is not None
    assert len(result) == 1
    snap = result[0]
    assert snap.outcomes == {"Trump": 0.42, "Vance": 0.31, "Harris": 0.18}
    # Leader's probability surfaced as the snapshot's `probability`.
    assert snap.probability == pytest.approx(0.42)
    assert snap.meta["market_ids"] == ["m1", "m2", "m3"]


async def test_polymarket_binary_event_leaves_outcomes_empty():
    """One market, no label = binary. outcomes must stay empty."""
    client = PolymarketClient()
    payload = [
        {
            "title": "Will Drake drop an album by July?",
            "slug": "drake",
            "markets": [{"outcomePrices": ["0.38", "0.62"]}],
        },
    ]
    with patch(
        "utils.markets._get_session",
        AsyncMock(return_value=_mock_session(_mock_resp(200, payload))),
    ):
        result = await client.get_trending_events()
    assert result is not None
    snap = result[0]
    assert snap.outcomes == {}
    assert snap.probability == pytest.approx(0.38)


# ---- Polymarket competitive sort ------------------------------------------


async def test_polymarket_competitive_events_hits_correct_endpoint():
    client = PolymarketClient()
    payload = [
        {
            "title": "Close race",
            "slug": "close",
            "markets": [{"outcomePrices": ["0.49", "0.51"]}],
        },
    ]
    mock_sess = _mock_session(_mock_resp(200, payload))
    with patch(
        "utils.markets._get_session",
        AsyncMock(return_value=mock_sess),
    ):
        result = await client.get_competitive_events(limit=5)
    assert result is not None
    assert len(result) == 1
    # Verify the call used the competitive sort param.
    call_args = mock_sess.get.call_args
    assert call_args.kwargs["params"]["order"] == "competitive"


# ---- Polymarket price history ---------------------------------------------


async def test_polymarket_price_history_summarizes_series():
    client = PolymarketClient()
    payload = {
        "history": [
            {"t": 1000, "p": 0.22},
            {"t": 2000, "p": 0.28},
            {"t": 3000, "p": 0.32},
            {"t": 4000, "p": 0.38},
        ],
    }
    with patch(
        "utils.markets._get_session",
        AsyncMock(return_value=_mock_session(_mock_resp(200, payload))),
    ):
        result = await client.get_price_history("market-abc", hours=24)
    assert result is not None
    assert isinstance(result, PriceHistorySummary)
    assert result.open_price == pytest.approx(0.22)
    assert result.current_price == pytest.approx(0.38)
    assert result.change == pytest.approx(0.16)
    assert result.change_pct == pytest.approx(0.16 / 0.22)
    assert result.history_points == 4


async def test_polymarket_price_history_returns_none_for_too_few_points():
    client = PolymarketClient()
    payload = {"history": [{"t": 1, "p": 0.5}]}
    with patch(
        "utils.markets._get_session",
        AsyncMock(return_value=_mock_session(_mock_resp(200, payload))),
    ):
        result = await client.get_price_history("m")
    assert result is None


async def test_polymarket_price_history_returns_none_when_open_is_zero():
    client = PolymarketClient()
    payload = {"history": [{"t": 1, "p": 0}, {"t": 2, "p": 0.1}]}
    with patch(
        "utils.markets._get_session",
        AsyncMock(return_value=_mock_session(_mock_resp(200, payload))),
    ):
        result = await client.get_price_history("m")
    assert result is None


# ---- format helpers for new types -----------------------------------------


def test_format_markets_renders_multi_outcome():
    snap = MarketSnapshot(
        source="polymarket",
        title="2028 Election",
        url="https://polymarket.com/event/pres-2028",
        probability=0.42,
        outcomes={"Trump": 0.42, "Vance": 0.31, "Harris": 0.18},
        meta={"volume": 1_000_000},
    )
    out = format_markets_for_prompt([snap])
    assert "outcomes:" in out
    assert "Trump 42%" in out
    assert "Vance 31%" in out
    assert "Harris 18%" in out


def test_format_price_history_summary():
    summary = PriceHistorySummary(
        market_id="abc",
        current_price=0.38,
        open_price=0.22,
        change=0.16,
        change_pct=0.727,
        history_points=24,
    )
    out = format_price_history_for_prompt(summary)
    assert "opened 22%" in out
    assert "now 38%" in out
    assert "up 16pts" in out
    assert "24 data points" in out


def test_format_price_history_flat():
    summary = PriceHistorySummary(
        market_id="abc",
        current_price=0.5,
        open_price=0.5,
        change=0.0,
        change_pct=0.0,
        history_points=5,
    )
    out = format_price_history_for_prompt(summary)
    assert "flat" in out
