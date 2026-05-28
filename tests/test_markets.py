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
    """Verified against real SGO API response shape (May 2026)."""
    client = SportsGameOddsClient(api_key="test")
    payload = {
        "data": [
            {
                "eventID": "evt_1",
                "teams": {
                    "home": {"names": {"long": "Los Angeles Lakers", "medium": "Lakers"}},
                    "away": {"names": {"long": "Golden State Warriors", "medium": "Warriors"}},
                },
                "odds": {
                    "points-home-game-ml-home": {"bookOdds": "-150"},
                    "points-away-game-ml-away": {"bookOdds": "+130"},
                    "points-home-game-sp-home": {"bookOdds": "-110"},
                    "points-away-game-sp-away": {"bookOdds": "-110"},
                    "points-all-game-ou-over": {"bookOdds": "-108"},
                    "points-all-game-ou-under": {"bookOdds": "-112"},
                },
                "links": {
                    "bookmakers": {
                        "draftkings": "https://sportsbook.draftkings.com/event/12345",
                        "fanduel": "https://sportsbook.fanduel.com/basketball/-/12345",
                    },
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
    # Title uses team `names.medium`, formatted as "{away} @ {home}".
    assert snap.title == "Warriors @ Lakers"
    # Moneyline extracted from the nested structure.
    assert snap.odds["moneyline_home"] == -150
    assert snap.odds["moneyline_away"] == 130
    assert snap.odds["total_over"] == -108
    # URL prefers direct bookmaker deeplink over the SGO event page.
    assert snap.url == "https://sportsbook.draftkings.com/event/12345"


async def test_sgo_falls_back_to_event_url_when_no_bookmaker_link():
    client = SportsGameOddsClient(api_key="test")
    payload = {
        "data": [
            {
                "eventID": "evt_2",
                "teams": {
                    "home": {"names": {"medium": "Spurs"}},
                    "away": {"names": {"medium": "Thunder"}},
                },
                "odds": {},
            },
        ],
    }
    with patch(
        "utils.markets._get_session",
        AsyncMock(return_value=_mock_session(_mock_resp(200, payload))),
    ):
        result = await client.get_event_odds("NBA")
    assert result is not None
    assert result[0].title == "Thunder @ Spurs"
    assert result[0].url == "https://sportsgameodds.com/event/evt_2"


async def test_sgo_team_name_fallback_chain():
    """When .medium missing, falls back to long, short, location."""
    client = SportsGameOddsClient(api_key="test")
    payload = {
        "data": [
            {
                "eventID": "evt_3",
                "teams": {
                    "home": {"names": {"long": "Boston Celtics"}},  # only long
                    "away": {"names": {"short": "MIA"}},  # only short
                },
                "odds": {},
            },
        ],
    }
    with patch(
        "utils.markets._get_session",
        AsyncMock(return_value=_mock_session(_mock_resp(200, payload))),
    ):
        result = await client.get_event_odds("NBA")
    assert result is not None
    assert result[0].title == "MIA @ Boston Celtics"


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


async def test_kalshi_refresh_sorts_by_volume_and_keeps_top_n():
    """Series index is sorted by volume_fp desc, capped at _CACHE_TOP_N."""
    client = KalshiClient()
    client._CACHE_TOP_N = 3  # type: ignore[misc]  # shrink for test
    payload = {
        "series": [
            {"ticker": "KXLOW", "title": "Low vol", "volume_fp": "100"},
            {"ticker": "KXHIGH", "title": "High vol", "volume_fp": "999999"},
            {"ticker": "KXMID", "title": "Mid vol", "volume_fp": "5000"},
            {"ticker": "KXTINY", "title": "Tiny vol", "volume_fp": "1"},
        ],
    }
    with patch(
        "utils.markets._get_session",
        AsyncMock(return_value=_mock_session(_mock_resp(200, payload))),
    ), patch.object(
        client, "_fetch_open_event_series_tickers",
        AsyncMock(return_value=None),  # skip filter for sort/cap isolation
    ):
        ok = await client.refresh_series_index()
    assert ok is True
    assert [s["ticker"] for s in client.series_index] == ["KXHIGH", "KXMID", "KXLOW"]


async def test_kalshi_refresh_filters_out_series_without_open_events():
    """When the open-events set is known, series outside it get pruned even
    if they have higher volume_fp than series inside it."""
    client = KalshiClient()
    payload = {
        "series": [
            {"ticker": "KXDEAD", "title": "High vol but no events", "volume_fp": "999999"},
            {"ticker": "KXALIVE", "title": "Low vol, has events", "volume_fp": "10"},
            {"ticker": "KXALSODEAD", "title": "Also no events", "volume_fp": "500000"},
        ],
    }
    with patch(
        "utils.markets._get_session",
        AsyncMock(return_value=_mock_session(_mock_resp(200, payload))),
    ), patch.object(
        client, "_fetch_open_event_series_tickers",
        AsyncMock(return_value={"KXALIVE"}),
    ):
        await client.refresh_series_index()
    assert [s["ticker"] for s in client.series_index] == ["KXALIVE"]


async def test_kalshi_refresh_keeps_unfiltered_index_when_events_fetch_fails():
    """If the open-events helper returns None (fetch error), the index stays
    unfiltered rather than blanking. Better some shelves than no shelves."""
    client = KalshiClient()
    payload = {
        "series": [
            {"ticker": "KXA", "title": "a", "volume_fp": "100"},
            {"ticker": "KXB", "title": "b", "volume_fp": "50"},
        ],
    }
    with patch(
        "utils.markets._get_session",
        AsyncMock(return_value=_mock_session(_mock_resp(200, payload))),
    ), patch.object(
        client, "_fetch_open_event_series_tickers",
        AsyncMock(return_value=None),
    ):
        await client.refresh_series_index()
    assert [s["ticker"] for s in client.series_index] == ["KXA", "KXB"]


async def test_kalshi_refresh_keeps_stale_cache_on_failure():
    """Network/parse errors preserve the previous cache rather than blank it."""
    client = KalshiClient()
    # Seed a stale cache to defend.
    client._series_index = [{"ticker": "KXOLD", "title": "old"}]
    with patch(
        "utils.markets._get_session",
        AsyncMock(return_value=_mock_session(_mock_resp(500))),
    ):
        ok = await client.refresh_series_index()
    assert ok is False
    assert client.series_index == [{"ticker": "KXOLD", "title": "old"}]


async def test_kalshi_refresh_skips_malformed_series_entries():
    """Non-dict entries / missing ticker / missing title get filtered out."""
    client = KalshiClient()
    payload = {
        "series": [
            {"ticker": "KXOK", "title": "ok", "volume_fp": "10"},
            {"ticker": None, "title": "no ticker", "volume_fp": "5"},
            "not a dict",
            {"ticker": "KXNOTITLE", "title": None, "volume_fp": "5"},
        ],
    }
    with patch(
        "utils.markets._get_session",
        AsyncMock(return_value=_mock_session(_mock_resp(200, payload))),
    ), patch.object(
        client, "_fetch_open_event_series_tickers",
        AsyncMock(return_value=None),
    ):
        await client.refresh_series_index()
    assert client.series_index == [{"ticker": "KXOK", "title": "ok"}]


async def test_kalshi_fetch_open_event_series_tickers_paginates_and_dedupes():
    """Cursors across multiple pages; same series can appear in multiple events."""
    client = KalshiClient()
    page1 = {
        "events": [
            {"series_ticker": "KXA", "event_ticker": "KXA-1"},
            {"series_ticker": "KXB", "event_ticker": "KXB-1"},
            {"series_ticker": "KXA", "event_ticker": "KXA-2"},  # dup series
        ],
        "cursor": "p2",
    }
    page2 = {
        "events": [
            {"series_ticker": "KXC", "event_ticker": "KXC-1"},
        ],
        "cursor": "",  # last page
    }
    sess = MagicMock()
    sess.get = MagicMock(
        side_effect=[_mock_resp(200, page1), _mock_resp(200, page2)],
    )
    with patch("utils.markets._get_session", AsyncMock(return_value=sess)):
        result = await client._fetch_open_event_series_tickers()
    assert result == {"KXA", "KXB", "KXC"}


async def test_kalshi_fetch_open_event_series_tickers_http_error_returns_none():
    client = KalshiClient()
    with (
        patch(
            "utils.markets._get_session",
            AsyncMock(return_value=_mock_session(_mock_resp(500))),
        ),
        patch("utils.markets.emit") as emit_mock,
    ):
        result = await client._fetch_open_event_series_tickers()
    assert result is None
    # The hourly degradation must surface as a structured market_fetch event
    # (issue #91): it used to leave only a WARNING, invisible to dashboards.
    fetches = [
        c for c in emit_mock.call_args_list
        if c.args and c.args[0] == "market_fetch"
    ]
    assert len(fetches) == 1, f"expected one market_fetch emit, got {emit_mock.call_args_list}"
    assert fetches[0].kwargs["ok"] is False
    assert fetches[0].kwargs["source"] == "kalshi"
    assert fetches[0].kwargs["query"] == "open_events"
    assert fetches[0].kwargs["error"] == "pagination_failed"


async def test_kalshi_get_events_for_series_flattens_nested_markets():
    """/events?with_nested_markets=true returns events with .markets inside;
    we flatten across events into a single snapshot list."""
    client = KalshiClient()
    payload = {
        "events": [
            {
                "event_ticker": "KXBILLBOARD-26-DEC",
                "markets": [
                    {
                        "ticker": "KXBILLBOARD-26-DEC-DRAKE",
                        "event_ticker": "KXBILLBOARD-26-DEC",
                        "title": "Drake #1 on Dec 31",
                        "yes_bid_dollars": 0.30,
                        "yes_ask_dollars": 0.34,
                    },
                ],
            },
            {
                "event_ticker": "KXBILLBOARD-27-JAN",
                "markets": [
                    {
                        "ticker": "KXBILLBOARD-27-JAN-DRAKE",
                        "event_ticker": "KXBILLBOARD-27-JAN",
                        "title": "Drake #1 on Jan 7",
                        "yes_bid_dollars": 0.20,
                    },
                ],
            },
        ],
    }
    with patch(
        "utils.markets._get_session",
        AsyncMock(return_value=_mock_session(_mock_resp(200, payload))),
    ):
        snaps = await client.get_events_for_series("KXBILLBOARD")
    assert snaps is not None
    assert len(snaps) == 2
    assert snaps[0].title == "Drake #1 on Dec 31"
    assert snaps[0].probability == pytest.approx(0.32)
    assert snaps[0].url == "https://kalshi.com/markets/kxbillboard"


async def test_kalshi_get_events_filters_mve_combo_markets():
    """MVE Exotics (combo/parlay aggregations) get filtered by the snapshot
    converter, same as before the refactor."""
    client = KalshiClient()
    payload = {
        "events": [
            {
                "event_ticker": "KXMVENBASINGLEGAME",
                "markets": [
                    {
                        "ticker": "KXMVE-T1",
                        "event_ticker": "KXMVENBASINGLEGAME",
                        "title": "MVE NBA",
                        "yes_bid_dollars": 0.5,
                    },
                ],
            },
            {
                "event_ticker": "KXNBAGAME-LAL-BOS",
                "markets": [
                    {
                        "ticker": "KXNBA-T1",
                        "event_ticker": "KXNBAGAME-LAL-BOS",
                        "title": "Lakers beat Celtics",
                        "yes_bid_dollars": 0.5,
                    },
                ],
            },
        ],
    }
    with patch(
        "utils.markets._get_session",
        AsyncMock(return_value=_mock_session(_mock_resp(200, payload))),
    ):
        snaps = await client.get_events_for_series("KXNBAGAME")
    assert snaps is not None
    titles = [s.title for s in snaps]
    assert "Lakers beat Celtics" in titles
    assert "MVE NBA" not in titles


async def test_kalshi_get_events_empty_ticker_returns_none():
    client = KalshiClient()
    assert await client.get_events_for_series("") is None


async def test_kalshi_get_events_http_error_returns_none():
    client = KalshiClient()
    with patch(
        "utils.markets._get_session",
        AsyncMock(return_value=_mock_session(_mock_resp(500))),
    ):
        result = await client.get_events_for_series("KXFOO")
    assert result is None


async def test_kalshi_market_snapshot_parses_midpoint_and_url():
    """Direct sanity check on the snapshot converter (used by get_events_for_series)."""
    from utils.markets import _kalshi_market_to_snapshot
    snap = _kalshi_market_to_snapshot({
        "ticker": "KXPRES2028-DJT-T1",
        "event_ticker": "KXPRES2028-DJT",
        "title": "Trump wins 2028 election",
        "yes_bid_dollars": 0.30,
        "yes_ask_dollars": 0.34,
        "volume_fp": 50000,
    })
    assert snap is not None
    assert snap.probability == pytest.approx(0.32)
    assert snap.url == "https://kalshi.com/markets/kxpres2028"
    assert snap.meta["ticker"] == "KXPRES2028-DJT-T1"


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


def _kalshi_mocks(
    manager: MarketsManager,
    *,
    series_index: list[dict[str, str]] | None = None,
    picker_ticker: str | None = "KXFOO",
    events: list[MarketSnapshot] | None = None,
) -> tuple[AsyncMock, AsyncMock]:
    """Wire a MarketsManager with a hot Kalshi cache + a picker + an events fetch.

    Mirrors the production wiring: bot.setup_hook calls start_series_refresh_loop
    which populates series_index, then per-query MarketsManager._kalshi_snapshots
    hands the cache to the picker and fetches events for the chosen ticker.
    """
    manager.kalshi._series_index = series_index or [
        {"ticker": "KXFOO", "title": "Foo series"},
        {"ticker": "KXBAR", "title": "Bar series"},
    ]
    picker = AsyncMock(return_value=picker_ticker)
    manager._kalshi_picker = picker
    fetch = AsyncMock(return_value=events or [])
    manager.kalshi.get_events_for_series = fetch  # type: ignore[method-assign]
    return picker, fetch


async def test_manager_pm_query_hits_both_sources_in_parallel():
    """Polymarket and Kalshi are peer sources, not fallback. Both fire."""
    m = MarketsManager(sgo_api_key=None)
    poly = [MarketSnapshot(source="polymarket", title="P", url="u", probability=0.3)]
    kalshi = [MarketSnapshot(source="kalshi", title="K", url="u2", probability=0.4)]
    m.polymarket.search_markets = AsyncMock(return_value=poly)  # type: ignore[method-assign]
    picker, fetch = _kalshi_mocks(m, events=kalshi)
    result = await m.get_context("will drake drop an album by july")
    assert result is not None
    # Default order: Polymarket first, Kalshi after.
    assert [s.source for s in result] == ["polymarket", "kalshi"]
    m.polymarket.search_markets.assert_awaited_once()
    picker.assert_awaited_once()
    fetch.assert_awaited_once_with("KXFOO", limit=4)


async def test_manager_pm_kalshi_first_when_user_mentions_kalshi():
    """When the query says 'kalshi', Kalshi results lead the list."""
    m = MarketsManager(sgo_api_key=None)
    poly = [MarketSnapshot(source="polymarket", title="P", url="u", probability=0.3)]
    kalshi = [MarketSnapshot(source="kalshi", title="K", url="u2", probability=0.4)]
    m.polymarket.search_markets = AsyncMock(return_value=poly)  # type: ignore[method-assign]
    _kalshi_mocks(m, events=kalshi)
    result = await m.get_context("any spicy kalshi markets right now")
    assert result is not None
    assert [s.source for s in result] == ["kalshi", "polymarket"]


async def test_manager_pm_one_source_outage_keeps_other():
    """If Polymarket errors, Kalshi results still come through."""
    m = MarketsManager(sgo_api_key=None)
    kalshi_snaps = [MarketSnapshot(source="kalshi", title="K", url="u", probability=0.5)]
    m.polymarket.search_markets = AsyncMock(side_effect=RuntimeError("polymarket down"))  # type: ignore[method-assign]
    _kalshi_mocks(m, events=kalshi_snaps)
    # Prompt mentions polymarket/kalshi so regex classifier routes to PM intent.
    result = await m.get_context("polymarket trump 2028 election")
    assert result == kalshi_snaps


async def test_manager_fails_open_on_exception():
    """Both PM sources fail (poly errors, kalshi picker errors) -> manager returns None."""
    m = MarketsManager(sgo_api_key=None)
    m.polymarket.search_markets = AsyncMock(side_effect=RuntimeError("boom"))  # type: ignore[method-assign]
    # Picker errors -> _kalshi_snapshots returns [] (swallowed via emit_error).
    _kalshi_mocks(m, picker_ticker=None)
    m._kalshi_picker = AsyncMock(side_effect=RuntimeError("haiku down"))
    result = await m.get_context("will drake drop by july")
    assert result is None


async def test_manager_kalshi_no_picker_skips_kalshi():
    """No picker wired (e.g. classifier never injected) -> Kalshi skipped silently."""
    m = MarketsManager(sgo_api_key=None)
    poly = [MarketSnapshot(source="polymarket", title="P", url="u", probability=0.3)]
    m.polymarket.search_markets = AsyncMock(return_value=poly)  # type: ignore[method-assign]
    # Don't wire picker; ensure get_events_for_series is never even reached.
    m.kalshi.get_events_for_series = AsyncMock()  # type: ignore[method-assign]
    result = await m.get_context("will drake drop by july")
    assert result == poly
    m.kalshi.get_events_for_series.assert_not_called()


async def test_manager_kalshi_empty_cache_skips_kalshi():
    """Cache still warming up (refresh hasn't fired) -> Kalshi skipped silently."""
    m = MarketsManager(sgo_api_key=None)
    poly = [MarketSnapshot(source="polymarket", title="P", url="u", probability=0.3)]
    m.polymarket.search_markets = AsyncMock(return_value=poly)  # type: ignore[method-assign]
    picker = AsyncMock()
    m._kalshi_picker = picker
    # series_index is empty (default) so picker shouldn't fire.
    m.kalshi.get_events_for_series = AsyncMock()  # type: ignore[method-assign]
    result = await m.get_context("will drake drop by july")
    assert result == poly
    picker.assert_not_called()


async def test_manager_kalshi_picker_says_none_skips_fetch():
    """Haiku NONE -> no series fetch, fall through to Polymarket-only."""
    m = MarketsManager(sgo_api_key=None)
    poly = [MarketSnapshot(source="polymarket", title="P", url="u", probability=0.3)]
    m.polymarket.search_markets = AsyncMock(return_value=poly)  # type: ignore[method-assign]
    picker, fetch = _kalshi_mocks(m, picker_ticker=None)
    result = await m.get_context("will drake drop by july")
    assert result == poly
    picker.assert_awaited_once()
    fetch.assert_not_called()


# ---- MarketsManager two-stage Kalshi (series picker + market picker) ----


async def test_manager_stage2_narrows_to_single_market():
    """When the market_picker returns a specific market_ticker, the
    snapshot list narrows to just that one snapshot."""
    classifier = AsyncMock(return_value={
        "intent": "prediction_market", "search_terms": "drake hot 100",
    })
    m = MarketsManager(sgo_api_key=None, intent_classifier=classifier)
    m.polymarket.search_markets = AsyncMock(return_value=None)  # type: ignore[method-assign]
    m.kalshi._series_index = [
        {"ticker": "KXBILLBOARD", "title": "Billboard Hot 100"},
    ]
    m._kalshi_picker = AsyncMock(return_value="KXBILLBOARD")
    drake = MarketSnapshot(
        source="kalshi", title="Drake #1 on Dec 8",
        url="https://kalshi.com/markets/kxbillboard", probability=0.18,
        meta={"ticker": "KXBILLBOARD-DEC-DRAKE"},
    )
    weeknd = MarketSnapshot(
        source="kalshi", title="Weeknd #1 on Dec 8",
        url="https://kalshi.com/markets/kxbillboard", probability=0.12,
        meta={"ticker": "KXBILLBOARD-DEC-WEEKND"},
    )
    m.kalshi.get_events_for_series = AsyncMock(return_value=[drake, weeknd])  # type: ignore[method-assign]
    market_picker = AsyncMock(return_value="KXBILLBOARD-DEC-DRAKE")
    m._kalshi_market_picker = market_picker
    result = await m.get_context("drake hot 100")
    assert result == [drake]
    # Stage 2 picker saw both markets as candidates.
    market_picker.assert_awaited_once()
    candidates = market_picker.call_args.args[1]
    assert {c["ticker"] for c in candidates} == {
        "KXBILLBOARD-DEC-DRAKE", "KXBILLBOARD-DEC-WEEKND",
    }


async def test_manager_stage2_none_returns_full_series():
    """Stage 2 NONE means 'no specific market matches the query'; we fall
    back to showing the whole series's markets."""
    classifier = AsyncMock(return_value={
        "intent": "prediction_market", "search_terms": "billboard chart",
    })
    m = MarketsManager(sgo_api_key=None, intent_classifier=classifier)
    m.polymarket.search_markets = AsyncMock(return_value=None)  # type: ignore[method-assign]
    m.kalshi._series_index = [
        {"ticker": "KXBILLBOARD", "title": "Billboard Hot 100"},
    ]
    m._kalshi_picker = AsyncMock(return_value="KXBILLBOARD")
    snaps = [
        MarketSnapshot(source="kalshi", title="Drake", url="u", meta={"ticker": "A"}),
        MarketSnapshot(source="kalshi", title="Weeknd", url="u", meta={"ticker": "B"}),
    ]
    m.kalshi.get_events_for_series = AsyncMock(return_value=snaps)  # type: ignore[method-assign]
    m._kalshi_market_picker = AsyncMock(return_value=None)
    result = await m.get_context("billboard chart")
    assert result == snaps


async def test_manager_stage2_skipped_when_single_market():
    """Single-market series short-circuits stage 2, no second Haiku call."""
    classifier = AsyncMock(return_value={
        "intent": "prediction_market", "search_terms": "fed rate cut",
    })
    m = MarketsManager(sgo_api_key=None, intent_classifier=classifier)
    m.polymarket.search_markets = AsyncMock(return_value=None)  # type: ignore[method-assign]
    m.kalshi._series_index = [{"ticker": "KXRATECUT", "title": "Rate cut"}]
    m._kalshi_picker = AsyncMock(return_value="KXRATECUT")
    only_snap = MarketSnapshot(
        source="kalshi", title="Will Fed cut?", url="u",
        meta={"ticker": "KXRATECUT-ONLY"},
    )
    m.kalshi.get_events_for_series = AsyncMock(return_value=[only_snap])  # type: ignore[method-assign]
    market_picker = AsyncMock()
    m._kalshi_market_picker = market_picker
    result = await m.get_context("fed rate cut")
    assert result == [only_snap]
    market_picker.assert_not_called()


async def test_manager_stage2_skipped_when_no_market_picker():
    """When the market_picker isn't wired, stage 2 is skipped and we
    return the full series (Option B graceful degradation)."""
    classifier = AsyncMock(return_value={
        "intent": "prediction_market", "search_terms": "billboard",
    })
    m = MarketsManager(sgo_api_key=None, intent_classifier=classifier)
    m.polymarket.search_markets = AsyncMock(return_value=None)  # type: ignore[method-assign]
    m.kalshi._series_index = [{"ticker": "KXBILLBOARD", "title": "Hot 100"}]
    m._kalshi_picker = AsyncMock(return_value="KXBILLBOARD")
    snaps = [
        MarketSnapshot(source="kalshi", title="A", url="u", meta={"ticker": "A"}),
        MarketSnapshot(source="kalshi", title="B", url="u", meta={"ticker": "B"}),
    ]
    m.kalshi.get_events_for_series = AsyncMock(return_value=snaps)  # type: ignore[method-assign]
    # _kalshi_market_picker stays None (default).
    result = await m.get_context("billboard")
    assert result == snaps


async def test_manager_stage2_failure_falls_back_to_full_series():
    """If stage 2 raises, we keep the series snapshots rather than blanking
    Kalshi context entirely."""
    classifier = AsyncMock(return_value={
        "intent": "prediction_market", "search_terms": "billboard",
    })
    m = MarketsManager(sgo_api_key=None, intent_classifier=classifier)
    m.polymarket.search_markets = AsyncMock(return_value=None)  # type: ignore[method-assign]
    m.kalshi._series_index = [{"ticker": "KXBILLBOARD", "title": "Hot 100"}]
    m._kalshi_picker = AsyncMock(return_value="KXBILLBOARD")
    snaps = [
        MarketSnapshot(source="kalshi", title="A", url="u", meta={"ticker": "A"}),
        MarketSnapshot(source="kalshi", title="B", url="u", meta={"ticker": "B"}),
    ]
    m.kalshi.get_events_for_series = AsyncMock(return_value=snaps)  # type: ignore[method-assign]
    m._kalshi_market_picker = AsyncMock(side_effect=RuntimeError("haiku down"))
    result = await m.get_context("billboard")
    assert result == snaps


async def test_manager_stage2_unknown_ticker_falls_back_to_full_series():
    """Stage 2 returns a ticker that doesn't match any snapshot
    (shouldn't happen but defend against it) -> fall back to all snapshots."""
    classifier = AsyncMock(return_value={
        "intent": "prediction_market", "search_terms": "billboard",
    })
    m = MarketsManager(sgo_api_key=None, intent_classifier=classifier)
    m.polymarket.search_markets = AsyncMock(return_value=None)  # type: ignore[method-assign]
    m.kalshi._series_index = [{"ticker": "KXBILLBOARD", "title": "Hot 100"}]
    m._kalshi_picker = AsyncMock(return_value="KXBILLBOARD")
    snaps = [
        MarketSnapshot(source="kalshi", title="A", url="u", meta={"ticker": "A"}),
        MarketSnapshot(source="kalshi", title="B", url="u", meta={"ticker": "B"}),
    ]
    m.kalshi.get_events_for_series = AsyncMock(return_value=snaps)  # type: ignore[method-assign]
    m._kalshi_market_picker = AsyncMock(return_value="KXSTALE")
    result = await m.get_context("billboard")
    assert result == snaps


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
    """Search terms from the classifier flow through to Polymarket AND the
    Kalshi picker (both peer PM sources)."""
    classifier = AsyncMock(return_value={
        "intent": "prediction_market",
        "search_terms": "drake album july",
    })
    m = MarketsManager(sgo_api_key=None, intent_classifier=classifier)
    m.polymarket.search_markets = AsyncMock(return_value=None)  # type: ignore[method-assign]
    picker, fetch = _kalshi_mocks(m, picker_ticker="KXBILLBOARD")
    await m.get_context("anything about drake")
    m.polymarket.search_markets.assert_awaited_once_with("drake album july", limit=4)
    # Picker sees the classifier's search_terms, not the raw query.
    picker.assert_awaited_once()
    assert picker.call_args.args[0] == "drake album july"
    fetch.assert_awaited_once_with("KXBILLBOARD", limit=4)


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
