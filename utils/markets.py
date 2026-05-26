"""Market data enrichers: sports lines + prediction markets for Toots' takes.

Three sources behind one normalized shape so cogs can pull live market signal
without caring about per-source auth or response wrangling:

- SportsGameOdds (SGO): sports lines (spreads, moneylines, totals, props) across
  9 books on the free Amateur tier, 80+ on the paid Rookie tier ($99/mo). Bearer
  auth via X-Api-Key. Object-based billing (1 row = 1 object, no credit math).
  Free tier covers NFL/NBA/MLB/NHL/CFB/CBB/UCL/MLS.
- Polymarket gamma: prediction markets across politics, culture, sports, crypto.
  No auth. Real-time crowd belief on future events. Endpoints we use:
  /events sorted by 24h volume, /public-search for query lookup.
- Kalshi: CFTC-regulated US prediction markets. Reads are public and unauth'd
  (the docs are explicit). RSA-signing only applies to trade execution, which
  we don't do. If trading lands later, add auth wiring here.

All three are fail-open: any network error, parse error, or missing key returns
None and the caller falls through to vibes-only commentary. We never bubble an
exception into a cog. Pattern matches utils/perplexity.py and utils/link_enrich.py.

Each fetch emits a `market_fetch` event (source, query, ok, duration_ms,
cache_hit, result_count, error) for the dashboard.
"""

from __future__ import annotations

import logging
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Literal

import aiohttp

from utils.async_cache import async_lru_cache
from utils.events import emit, emit_error

log = logging.getLogger(__name__)

_TIMEOUT_SECONDS = 5.0
_CACHE_SIZE = 128

_SGO_API_BASE = "https://api.sportsgameodds.com/v2"
_POLY_API_BASE = "https://gamma-api.polymarket.com"
_KALSHI_API_BASE = "https://api.elections.kalshi.com/trade-api/v2"


# ---- normalized output ------------------------------------------------------


MarketSource = Literal["sgo", "polymarket", "kalshi"]


@dataclass
class MarketSnapshot:
    """One market or one sports line, normalized across sources.

    `source` distinguishes signal type so Toots can weave it into commentary
    (e.g. "polymarket has it at 38%, kalshi at 42%, that's a real split").
    For sports lines (`source="sgo"`), `odds` carries per-market values like
    {"spread": -3.5, "moneyline_home": -150}. For binary prediction markets,
    `probability` is in [0, 1].

    `outcomes` is populated for multi-outcome prediction-market events (a
    presidential election with 8 candidates, an Oscars race with 5 nominees).
    Maps outcome label -> implied probability. Empty for binary markets where
    `probability` is the YES side and the NO side is just (1 - probability).
    """

    source: MarketSource
    title: str
    url: str
    probability: float | None = None
    odds: dict[str, float] = field(default_factory=dict)
    outcomes: dict[str, float] = field(default_factory=dict)
    meta: dict[str, Any] = field(default_factory=dict)


@dataclass
class PriceHistorySummary:
    """Compressed view of a price-history time series.

    The raw timeseries can be hundreds of points; for Toots' commentary the
    load-bearing facts are: where it opened, where it is now, how much it
    moved, how many data points are in the window. That's enough to say "this
    moved 12 points in 6 hours" without spending a thousand tokens on a chart.
    """

    market_id: str
    current_price: float
    open_price: float
    change: float
    change_pct: float
    history_points: int


# ---- shared session ---------------------------------------------------------


_session: aiohttp.ClientSession | None = None


async def _get_session() -> aiohttp.ClientSession:
    global _session
    if _session is None or _session.closed:
        _session = aiohttp.ClientSession(
            headers={"User-Agent": "tootsies-bot (markets)"},
            timeout=aiohttp.ClientTimeout(total=_TIMEOUT_SECONDS),
        )
    return _session


async def close_session() -> None:
    """Close the shared session, called on bot shutdown."""
    global _session
    if _session is not None and not _session.closed:
        await _session.close()
    _session = None


# ---- fetch + emit helper ----------------------------------------------------


async def _fetch_json(
    url: str,
    *,
    params: dict[str, str] | None = None,
    headers: dict[str, str] | None = None,
) -> Any | None:
    """GET JSON with timeout. Returns None on any HTTP/parse error."""
    try:
        sess = await _get_session()
        async with sess.get(url, params=params, headers=headers) as resp:
            if resp.status >= 400:
                return None
            return await resp.json(content_type=None)
    except (aiohttp.ClientError, TimeoutError, ValueError):
        return None


def _emit_fetch(
    *,
    source: MarketSource,
    query: str,
    ok: bool,
    start: float,
    cache_hit: bool = False,
    result_count: int = 0,
    error: str | None = None,
) -> None:
    emit(
        "market_fetch",
        source=source,
        query=query,
        ok=ok,
        duration_ms=int((time.monotonic() - start) * 1000),
        cache_hit=cache_hit,
        result_count=result_count,
        error=error,
    )


# ---- SportsGameOdds ---------------------------------------------------------


class SportsGameOddsClient:
    """Sports lines from SportsGameOdds.

    Auth via `X-Api-Key` header. Set SPORTS_GAME_ODDS_API_KEY in env.
    Free Amateur tier: 9 books, 10-min update, NFL/NBA/MLB/NHL/CFB/CBB/UCL/MLS.
    Paid Rookie ($99/mo): 77 books incl. Pinnacle, 3-min update, 17 leagues.

    Docs: https://sportsgameodds.com/docs/
    """

    def __init__(self, api_key: str | None) -> None:
        self._api_key = api_key

    @property
    def enabled(self) -> bool:
        return bool(self._api_key)

    async def get_event_odds(
        self,
        league: str,
        *,
        purpose: str = "unknown",
        limit: int = 10,
    ) -> list[MarketSnapshot] | None:
        """Fetch active events with odds for a league (e.g. 'NBA', 'NFL').

        Returns None on disabled / failure, empty list if no events available.
        Caches per (league, limit) for 5min via the cached helper below.
        """
        if not self.enabled:
            return None
        start = time.monotonic()
        result = await self._get_event_odds_cached(league, limit)
        cache_hit = bool(getattr(self._get_event_odds_cached, "_last_was_hit", False))
        if result is None:
            _emit_fetch(
                source="sgo", query=league, ok=False, start=start,
                cache_hit=cache_hit, error="fetch_failed",
            )
            return None
        _emit_fetch(
            source="sgo", query=league, ok=True, start=start,
            cache_hit=cache_hit, result_count=len(result),
        )
        return result

    @async_lru_cache(maxsize=_CACHE_SIZE)
    async def _get_event_odds_cached(
        self,
        league: str,
        limit: int,
    ) -> list[MarketSnapshot] | None:
        if not self._api_key:
            return None
        data = await _fetch_json(
            f"{_SGO_API_BASE}/events",
            params={
                "leagueID": league,
                "type": "match",
                "oddsAvailable": "true",
                "limit": str(limit),
            },
            headers={"X-Api-Key": self._api_key},
        )
        if not isinstance(data, dict):
            return None
        events = data.get("data") or data.get("events") or []
        if not isinstance(events, list):
            return None
        snapshots: list[MarketSnapshot] = []
        for event in events[:limit]:
            snap = _sgo_event_to_snapshot(event, league)
            if snap is not None:
                snapshots.append(snap)
        return snapshots

    async def get_player_props(
        self,
        league: str,
        *,
        purpose: str = "unknown",
        limit: int = 10,
    ) -> list[MarketSnapshot] | None:
        """Fetch player props (LeBron over 7.5 dimes, etc.) for a league.

        Props are the personality territory for sports commentary, calling out
        a specific player + line + over/under is way more interesting than the
        team spread. SGO returns props nested inside event objects; we surface
        each prop as its own MarketSnapshot so the format helper can render
        them inline alongside the main lines.

        Returns None on disabled / failure, empty list if no props available
        for the league right now (mid-offseason, etc.).
        """
        if not self.enabled:
            return None
        start = time.monotonic()
        result = await self._get_player_props_cached(league, limit)
        cache_hit = bool(getattr(self._get_player_props_cached, "_last_was_hit", False))
        if result is None:
            _emit_fetch(
                source="sgo", query=f"props:{league}", ok=False, start=start,
                cache_hit=cache_hit, error="fetch_failed",
            )
            return None
        _emit_fetch(
            source="sgo", query=f"props:{league}", ok=True, start=start,
            cache_hit=cache_hit, result_count=len(result),
        )
        return result

    @async_lru_cache(maxsize=_CACHE_SIZE)
    async def _get_player_props_cached(
        self,
        league: str,
        limit: int,
    ) -> list[MarketSnapshot] | None:
        if not self._api_key:
            return None
        data = await _fetch_json(
            f"{_SGO_API_BASE}/events",
            params={
                "leagueID": league,
                "type": "match",
                "oddsAvailable": "true",
                "includeProps": "true",
                "limit": str(limit),
            },
            headers={"X-Api-Key": self._api_key},
        )
        if not isinstance(data, dict):
            return None
        events = data.get("data") or data.get("events") or []
        if not isinstance(events, list):
            return None
        snapshots: list[MarketSnapshot] = []
        for event in events[:limit]:
            snapshots.extend(_sgo_event_to_prop_snapshots(event, league))
        return snapshots


def _sgo_event_to_snapshot(event: dict[str, Any], league: str) -> MarketSnapshot | None:
    """Best-effort flatten of an SGO event into one MarketSnapshot.

    SGO API shape (verified against live response May 2026):
    - `teams.{home,away}.names.{long,medium,short,location}` for team names
    - `odds` is a flat dict keyed by `{statID}-{entity}-{period}-{betType}-{side}`
      (e.g. "points-home-game-ml-home" for the home moneyline), with each
      value carrying `bookOdds` (american odds string like "+160", "-110")
    - `links.bookmakers.{book}` for direct sportsbook deeplinks (preferred
      over the SGO event page since users can actually go bet there)

    Defensive against shape drift: every lookup falls back gracefully.
    """
    if not isinstance(event, dict):
        return None
    event_id = event.get("eventID") or event.get("id") or ""

    # Team names — try medium, long, short, location in that order.
    teams_raw = event.get("teams")
    teams: dict[str, Any] = teams_raw if isinstance(teams_raw, dict) else {}
    home = _sgo_team_name(teams.get("home"))
    away = _sgo_team_name(teams.get("away"))
    title = f"{away} @ {home}" if home and away else "match"

    # Main lines: pick out the canonical moneyline / spread / total entries.
    # Player props live in this same dict but are handled by the props fetcher.
    flat_odds: dict[str, float] = {}
    odds_payload = event.get("odds")
    if isinstance(odds_payload, dict):
        for odd_key, dest_key in (
            ("points-home-game-ml-home", "moneyline_home"),
            ("points-away-game-ml-away", "moneyline_away"),
            ("points-home-game-sp-home", "spread_home"),
            ("points-away-game-sp-away", "spread_away"),
            ("points-all-game-ou-over", "total_over"),
            ("points-all-game-ou-under", "total_under"),
        ):
            entry = odds_payload.get(odd_key)
            if not isinstance(entry, dict):
                continue
            raw = entry.get("bookOdds") or entry.get("fairOdds")
            if not isinstance(raw, str):
                continue
            try:
                flat_odds[dest_key] = float(raw.replace("+", ""))
            except ValueError:
                continue

    # URL: prefer a direct sportsbook link over the SGO event page so users
    # can actually go place the bet. Fall back to SGO if no book link present.
    url = f"https://sportsgameodds.com/event/{event_id}" if event_id else ""
    bookmaker_links = ((event.get("links") or {}).get("bookmakers") or {})
    if isinstance(bookmaker_links, dict):
        for preferred in ("draftkings", "fanduel", "betmgm", "caesars", "pointsbet"):
            link = bookmaker_links.get(preferred)
            if isinstance(link, str) and link.startswith("http"):
                url = link
                break

    return MarketSnapshot(
        source="sgo",
        title=title,
        url=url,
        odds=flat_odds,
        meta={"league": league, "event_id": event_id},
    )


def _sgo_team_name(team: Any) -> str:
    """Extract a display name from an SGO team object."""
    if not isinstance(team, dict):
        return ""
    names = team.get("names")
    if not isinstance(names, dict):
        return ""
    return (
        names.get("medium")
        or names.get("long")
        or names.get("short")
        or names.get("location")
        or ""
    )


def _sgo_event_to_prop_snapshots(event: dict[str, Any], league: str) -> list[MarketSnapshot]:
    """Pull each player prop in the event out as its own MarketSnapshot.

    SGO returns props nested under each event in a `playerProps` dict keyed by
    player + market type (points / assists / rebounds / etc.). Shape varies
    by API version so this is intentionally defensive: anything that doesn't
    look like {player, market_type, line, over_odds, under_odds} is skipped.

    Returns empty list (not None) on miss so the caller can extend() freely.
    """
    if not isinstance(event, dict):
        return []
    event_id = event.get("eventID") or event.get("id") or ""
    props_payload = event.get("playerProps") or event.get("props") or {}
    if not isinstance(props_payload, dict):
        return []
    snapshots: list[MarketSnapshot] = []
    for prop_key, prop_data in props_payload.items():
        if not isinstance(prop_data, dict):
            continue
        player = prop_data.get("player") or prop_data.get("playerName") or ""
        market_type = prop_data.get("marketType") or prop_data.get("type") or ""
        line = prop_data.get("line") or prop_data.get("threshold")
        over_odds = prop_data.get("over") or prop_data.get("overOdds")
        under_odds = prop_data.get("under") or prop_data.get("underOdds")
        if not (player and market_type) or not isinstance(line, int | float):
            continue
        flat_odds: dict[str, float] = {"line": float(line)}
        if isinstance(over_odds, int | float):
            flat_odds["over"] = float(over_odds)
        if isinstance(under_odds, int | float):
            flat_odds["under"] = float(under_odds)
        title = f"{player} {market_type} o/u {line}"
        snapshots.append(MarketSnapshot(
            source="sgo",
            title=title,
            url=f"https://sportsgameodds.com/event/{event_id}" if event_id else "",
            odds=flat_odds,
            meta={"league": league, "event_id": event_id, "prop_key": str(prop_key)},
        ))
    return snapshots


# ---- Polymarket -------------------------------------------------------------


class PolymarketClient:
    """Prediction markets from Polymarket's public Gamma API. No auth.

    Two primary use cases:
    - get_trending_events: sort by 24h volume for "what's the room betting on"
    - search_markets: free-text query for "what's the line on <topic>"
    """

    async def get_trending_events(
        self,
        *,
        limit: int = 5,
        tag: str | None = None,
    ) -> list[MarketSnapshot] | None:
        """Top events by 24h volume. `tag` filters to a category like 'sports'."""
        start = time.monotonic()
        result = await self._get_trending_cached(limit, tag or "")
        cache_hit = bool(getattr(self._get_trending_cached, "_last_was_hit", False))
        if result is None:
            _emit_fetch(
                source="polymarket", query=f"trending:{tag or 'all'}", ok=False,
                start=start, cache_hit=cache_hit, error="fetch_failed",
            )
            return None
        _emit_fetch(
            source="polymarket", query=f"trending:{tag or 'all'}", ok=True,
            start=start, cache_hit=cache_hit, result_count=len(result),
        )
        return result

    @async_lru_cache(maxsize=_CACHE_SIZE)
    async def _get_trending_cached(
        self,
        limit: int,
        tag: str,
    ) -> list[MarketSnapshot] | None:
        params: dict[str, str] = {
            "order": "volume24hr",
            "ascending": "false",
            "limit": str(limit),
            "closed": "false",
        }
        if tag:
            params["tag"] = tag
        data = await _fetch_json(f"{_POLY_API_BASE}/events", params=params)
        if not isinstance(data, list):
            return None
        return [snap for event in data if (snap := _poly_event_to_snapshot(event))]

    async def search_markets(
        self,
        query: str,
        *,
        limit: int = 5,
    ) -> list[MarketSnapshot] | None:
        start = time.monotonic()
        result = await self._search_cached(query, limit)
        cache_hit = bool(getattr(self._search_cached, "_last_was_hit", False))
        if result is None:
            _emit_fetch(
                source="polymarket", query=query, ok=False,
                start=start, cache_hit=cache_hit, error="fetch_failed",
            )
            return None
        _emit_fetch(
            source="polymarket", query=query, ok=True,
            start=start, cache_hit=cache_hit, result_count=len(result),
        )
        return result

    @async_lru_cache(maxsize=_CACHE_SIZE)
    async def _search_cached(self, query: str, limit: int) -> list[MarketSnapshot] | None:
        data = await _fetch_json(
            f"{_POLY_API_BASE}/public-search",
            params={"q": query, "limit_per_type": str(limit)},
        )
        if not isinstance(data, dict):
            return None
        events = data.get("events") or []
        if not isinstance(events, list):
            return None
        return [snap for event in events if (snap := _poly_event_to_snapshot(event))]

    async def get_competitive_events(
        self,
        *,
        limit: int = 5,
        tag: str | None = None,
    ) -> list[MarketSnapshot] | None:
        """Events sorted by `competitive` (markets near 50/50).

        Sure-things at 95% are boring. The interesting commentary is on markets
        that are genuinely uncertain, where the crowd is split or wrong. This
        is the spiciest signal for /discourse beats: "polymarket has it 50/50,
        the news consensus says it's a lock, somebody's wrong."
        """
        start = time.monotonic()
        result = await self._get_competitive_cached(limit, tag or "")
        cache_hit = bool(getattr(self._get_competitive_cached, "_last_was_hit", False))
        if result is None:
            _emit_fetch(
                source="polymarket", query=f"competitive:{tag or 'all'}", ok=False,
                start=start, cache_hit=cache_hit, error="fetch_failed",
            )
            return None
        _emit_fetch(
            source="polymarket", query=f"competitive:{tag or 'all'}", ok=True,
            start=start, cache_hit=cache_hit, result_count=len(result),
        )
        return result

    @async_lru_cache(maxsize=_CACHE_SIZE)
    async def _get_competitive_cached(
        self,
        limit: int,
        tag: str,
    ) -> list[MarketSnapshot] | None:
        params: dict[str, str] = {
            "order": "competitive",
            "ascending": "false",
            "limit": str(limit),
            "closed": "false",
        }
        if tag:
            params["tag"] = tag
        data = await _fetch_json(f"{_POLY_API_BASE}/events", params=params)
        if not isinstance(data, list):
            return None
        return [snap for event in data if (snap := _poly_event_to_snapshot(event))]

    async def get_price_history(
        self,
        market_id: str,
        *,
        hours: int = 24,
    ) -> PriceHistorySummary | None:
        """Summarized price history for a market over the last `hours`.

        The CLOB `/prices-history` endpoint returns hundreds of price points
        which is too much to inject into a prompt. We compress to: where it
        opened, where it is now, how much it moved, total point count. Lets
        Toots say "this opened at 22%, money's been hammering it to 38% all
        week" without burning a thousand tokens on a chart.

        Returns None on failure or if fewer than 2 points exist.
        """
        if not market_id:
            return None
        start = time.monotonic()
        result = await self._get_price_history_cached(market_id, hours)
        cache_hit = bool(getattr(self._get_price_history_cached, "_last_was_hit", False))
        if result is None:
            _emit_fetch(
                source="polymarket", query=f"history:{market_id}:{hours}h", ok=False,
                start=start, cache_hit=cache_hit, error="fetch_failed",
            )
            return None
        _emit_fetch(
            source="polymarket", query=f"history:{market_id}:{hours}h", ok=True,
            start=start, cache_hit=cache_hit, result_count=result.history_points,
        )
        return result

    @async_lru_cache(maxsize=_CACHE_SIZE)
    async def _get_price_history_cached(
        self,
        market_id: str,
        hours: int,
    ) -> PriceHistorySummary | None:
        # CLOB API lives on a different host than Gamma.
        start_ts = int(time.time()) - hours * 3600
        data = await _fetch_json(
            "https://clob.polymarket.com/prices-history",
            params={
                "market": market_id,
                "startTs": str(start_ts),
                "fidelity": "60",
            },
        )
        if not isinstance(data, dict):
            return None
        history = data.get("history") or []
        if not isinstance(history, list) or len(history) < 2:
            return None
        try:
            open_price = float(history[0].get("p", 0))
            current_price = float(history[-1].get("p", 0))
        except (KeyError, TypeError, ValueError, AttributeError):
            return None
        if open_price == 0:
            return None
        change = current_price - open_price
        change_pct = change / open_price
        return PriceHistorySummary(
            market_id=market_id,
            current_price=current_price,
            open_price=open_price,
            change=change,
            change_pct=change_pct,
            history_points=len(history),
        )


def _poly_event_to_snapshot(event: dict[str, Any]) -> MarketSnapshot | None:
    """Flatten a Polymarket event into one normalized snapshot.

    Polymarket events nest a `markets` array. For binary events (`will X
    happen`), the first market's YES price IS the probability and `outcomes`
    stays empty. For multi-outcome events (election with 8 candidates, Oscars
    with 5 nominees), `outcomes` is populated with each market's name -> YES
    probability and `probability` carries the leader's price for quick
    reference. Lets the format helper show all candidates at once instead of
    silently dropping all but the first.
    """
    if not isinstance(event, dict):
        return None
    title = event.get("title") or ""
    slug = event.get("slug") or ""
    if not title:
        return None
    markets = event.get("markets") or []
    market_ids: list[str] = []
    # First pass: collect every market's YES price + best-guess label.
    market_prices: list[tuple[str, float]] = []
    if isinstance(markets, list):
        for market in markets:
            if not isinstance(market, dict):
                continue
            market_id = market.get("id") or market.get("conditionId") or ""
            if market_id and isinstance(market_id, str):
                market_ids.append(market_id)
            yes_price = _extract_market_yes_price(market)
            if yes_price is None:
                continue
            label_raw = market.get("groupItemTitle") or market.get("question") or ""
            label = label_raw.strip() if isinstance(label_raw, str) else ""
            market_prices.append((label, yes_price))

    # Decide binary vs multi-outcome based on how many distinct labeled markets
    # we found. A single market (or many markets with no usable labels) is
    # binary and uses the first market's YES price as `probability`.
    probability: float | None = None
    outcomes: dict[str, float] = {}
    labeled = {label: price for label, price in market_prices if label}
    if len(labeled) > 1:
        outcomes = labeled
        probability = max(outcomes.values())
    elif market_prices:
        probability = market_prices[0][1]

    return MarketSnapshot(
        source="polymarket",
        title=str(title),
        url=f"https://polymarket.com/event/{slug}" if slug else "",
        probability=probability,
        outcomes=outcomes,
        meta={
            "volume": event.get("volume"),
            "liquidity": event.get("liquidity"),
            "end_date": event.get("endDate"),
            "market_ids": market_ids,
        },
    )


def _extract_market_yes_price(market: dict[str, Any]) -> float | None:
    """Pull the YES price from a Polymarket market dict.

    Tries outcomePrices first (canonical), falls back to bestBid. Returns None
    if neither is parseable.
    """
    prices = _parse_prices(market.get("outcomePrices"))
    if prices:
        return prices[0]
    best_bid = market.get("bestBid")
    if isinstance(best_bid, int | float):
        return float(best_bid)
    return None


def _parse_prices(raw: Any) -> list[float]:
    """outcomePrices is sometimes a JSON string, sometimes a list of strings."""
    if isinstance(raw, str):
        try:
            import json
            raw = json.loads(raw)
        except (ValueError, TypeError):
            return []
    if not isinstance(raw, list):
        return []
    out: list[float] = []
    for v in raw:
        try:
            out.append(float(v))
        except (TypeError, ValueError):
            continue
    return out


# ---- Kalshi -----------------------------------------------------------------


class KalshiClient:
    """CFTC-regulated US prediction markets. Public reads, no auth.

    All market data (prices, order books, market details) is publicly
    available per Kalshi's docs. Trading endpoints require RSA-signed
    requests; we don't trade, so no signing here. If trading ever lands,
    add KALSHI_API_KEY_ID + KALSHI_PRIVATE_KEY wiring.
    """

    async def get_open_markets(
        self,
        *,
        query: str = "",
        limit: int = 5,
    ) -> list[MarketSnapshot] | None:
        start = time.monotonic()
        result = await self._get_open_cached(query, limit)
        cache_hit = bool(getattr(self._get_open_cached, "_last_was_hit", False))
        if result is None:
            _emit_fetch(
                source="kalshi", query=query or "open", ok=False,
                start=start, cache_hit=cache_hit, error="fetch_failed",
            )
            return None
        _emit_fetch(
            source="kalshi", query=query or "open", ok=True,
            start=start, cache_hit=cache_hit, result_count=len(result),
        )
        return result

    @async_lru_cache(maxsize=_CACHE_SIZE)
    async def _get_open_cached(
        self,
        query: str,
        limit: int,
    ) -> list[MarketSnapshot] | None:
        params: dict[str, str] = {"status": "open", "limit": str(limit)}
        if query:
            # Kalshi doesn't have a free-text search; the `tickers` param
            # accepts a CSV of tickers. For free-text we hit `series` first
            # in a future revision. For v1 we just pull open markets and
            # let the caller filter by title match downstream.
            pass
        data = await _fetch_json(f"{_KALSHI_API_BASE}/markets", params=params)
        if not isinstance(data, dict):
            return None
        markets = data.get("markets") or []
        if not isinstance(markets, list):
            return None
        return [snap for market in markets if (snap := _kalshi_market_to_snapshot(market))]


def _kalshi_market_to_snapshot(market: dict[str, Any]) -> MarketSnapshot | None:
    if not isinstance(market, dict):
        return None
    ticker = market.get("ticker") or ""
    event_ticker = market.get("event_ticker") or ""
    title = market.get("title") or ""
    if not title:
        return None
    # Kalshi quotes in dollars [0, 1]. Use the midpoint of yes_bid and yes_ask
    # when both are present; fall back to whichever exists.
    yes_bid = market.get("yes_bid_dollars")
    yes_ask = market.get("yes_ask_dollars")
    probability: float | None = None
    if isinstance(yes_bid, int | float) and isinstance(yes_ask, int | float):
        probability = (float(yes_bid) + float(yes_ask)) / 2.0
    elif isinstance(yes_bid, int | float):
        probability = float(yes_bid)
    elif isinstance(yes_ask, int | float):
        probability = float(yes_ask)
    return MarketSnapshot(
        source="kalshi",
        title=str(title),
        url=_kalshi_web_url(event_ticker, ticker, title),
        probability=probability,
        meta={
            "ticker": ticker,
            "event_ticker": event_ticker,
            "volume": market.get("volume_fp"),
            "liquidity": market.get("liquidity_dollars"),
            "expiration": market.get("expiration_time"),
        },
    )


def _kalshi_web_url(event_ticker: str, ticker: str, title: str) -> str:
    """Construct the canonical kalshi.com web URL for a market.

    Verified pattern from a real Kalshi URL:
        https://kalshi.com/markets/kxmlbgame/professional-baseball-game

    Two path segments:
      1. series_ticker (lowercase): extracted from event_ticker by taking
         everything before the first "-" (e.g. "KXMLBGAME-S20265B7F" -> "kxmlbgame")
      2. event slug (lowercase, alphanumeric + hyphens): slugified from title

    Falls back to a single-segment URL if we can't derive both halves cleanly.
    """
    series = ""
    if event_ticker:
        series = event_ticker.split("-", 1)[0].lower()
    slug = _slugify(title)
    if series and slug:
        return f"https://kalshi.com/markets/{series}/{slug}"
    if series:
        return f"https://kalshi.com/markets/{series}"
    if ticker:
        return f"https://kalshi.com/markets/{ticker.lower()}"
    return ""


def _slugify(text: str) -> str:
    """Lowercase + replace non-alphanumeric with hyphens, collapse repeats."""
    out = []
    prev_dash = False
    for ch in text.lower():
        if ch.isalnum():
            out.append(ch)
            prev_dash = False
        elif not prev_dash:
            out.append("-")
            prev_dash = True
    return "".join(out).strip("-")


# ---- prompt formatter -------------------------------------------------------


def format_markets_for_prompt(snapshots: list[MarketSnapshot]) -> str:
    """Render market snapshots as a Claude-friendly block.

    Empty list returns empty string so callers can concatenate unconditionally.
    Multi-outcome events render each candidate inline so Toots sees the whole
    race, not just the leader.
    """
    if not snapshots:
        return ""
    lines: list[str] = [
        "MARKET CONTEXT (pre-fetched live data, cite the URLs when riffing on "
        "specific lines; never invent a number that isn't in this block):",
    ]
    for snap in snapshots:
        if snap.source == "sgo":
            odds_str = ", ".join(f"{k}={v}" for k, v in snap.odds.items()) or "no lines"
            lines.append(f"  - [SGO] {snap.title}  {odds_str}  {snap.url}")
        elif snap.source in ("polymarket", "kalshi"):
            label = "Polymarket" if snap.source == "polymarket" else "Kalshi"
            vol = snap.meta.get("volume")
            vol_str = f" vol ${vol:,.0f}" if isinstance(vol, int | float) else ""
            if snap.outcomes:
                # Multi-outcome: show all candidates sorted by probability.
                top = sorted(snap.outcomes.items(), key=lambda kv: -kv[1])[:6]
                outcomes_str = ", ".join(f"{name} {p * 100:.0f}%" for name, p in top)
                lines.append(
                    f"  - [{label}] {snap.title}{vol_str}  {snap.url}\n"
                    f"      outcomes: {outcomes_str}"
                )
            else:
                prob_str = (
                    f"{snap.probability * 100:.0f}%" if snap.probability is not None else "?"
                )
                lines.append(
                    f"  - [{label}] {snap.title}  yes={prob_str}{vol_str}  {snap.url}"
                )
    return "\n".join(lines)


def format_price_history_for_prompt(summary: PriceHistorySummary) -> str:
    """Render a single price-history summary as a one-line block."""
    direction = "up" if summary.change > 0 else ("down" if summary.change < 0 else "flat")
    return (
        f"PRICE MOVEMENT ({summary.market_id}): opened {summary.open_price * 100:.0f}%, "
        f"now {summary.current_price * 100:.0f}% "
        f"({direction} {abs(summary.change) * 100:.0f}pts, "
        f"{summary.history_points} data points)."
    )


# ---- intent routing + manager ----------------------------------------------


# Keywords that signal the user is asking about a sports line / parlay / game.
# Intentionally narrow on betting vocabulary so /ask about a movie that happens
# to mention "playoffs" metaphorically doesn't trigger an odds fetch.
_SPORTS_PATTERNS: tuple[str, ...] = (
    "nba", "nfl", "mlb", "nhl", "ufc", "mls", "ucl",
    "basketball", "football", "baseball", "hockey", "soccer",
    "spread", "parlay", "moneyline", "money line", "over under", "over/under",
    "prop bet", "player prop", "point spread", "odds on", "ats",
    "sportsbook", "draftkings", "fanduel", "betmgm",
)

# Keywords + the canonical "will X happen by Y" shape that prediction markets cover.
_PM_PATTERNS: tuple[str, ...] = (
    "polymarket", "kalshi", "prediction market", "prediction markets",
    "election", "presidential", "primary", "primaries",
    "odds of ", "chance of ",
)
_WILL_X_BY_RE = re.compile(r"\bwill\b.{1,80}\b(by|before)\b", re.IGNORECASE)

# League keyword groups for routing the SGO fetch. League names + sport names
# only; team-name detection would need ~150 entries per sport to be useful and
# is out of scope for v1. If a user asks about a specific game without naming
# the league, we default to NBA below.
_LEAGUE_KEYWORDS: dict[str, tuple[str, ...]] = {
    # College leagues first so "college football" beats "football" -> NFL.
    "CFB": ("college football", "cfb"),
    "CBB": ("college basketball", "cbb"),
    "NBA": ("nba", "basketball"),
    "NFL": ("nfl", "football"),
    "MLB": ("mlb", "baseball"),
    "NHL": ("nhl", "hockey"),
    "UFC": ("ufc",),
    "MLS": ("mls",),
    "UCL": ("ucl", "champions league"),
}

Intent = Literal["sports", "prediction_market"]


def classify_intent(query: str) -> Intent | None:
    """Best-effort keyword router. Returns None if no clear market intent."""
    if not query:
        return None
    q = query.lower()
    if any(p in q for p in _SPORTS_PATTERNS):
        return "sports"
    if any(p in q for p in _PM_PATTERNS) or _WILL_X_BY_RE.search(query):
        return "prediction_market"
    return None


def detect_league(query: str) -> str | None:
    """Return canonical league ID (e.g. 'NBA') if the query names one, else None."""
    if not query:
        return None
    q = query.lower()
    for league, keywords in _LEAGUE_KEYWORDS.items():
        if any(k in q for k in keywords):
            return league
    return None


IntentClassifier = Callable[[str], Awaitable[dict[str, Any] | None]]


class MarketsManager:
    """Single entry point for cogs. Routes a query to the right client(s)
    and returns a list of MarketSnapshot (or None if nothing relevant).

    SGO is conditional on a key; Polymarket and Kalshi always work since
    their read APIs are public.

    `intent_classifier`: optional async callable that takes the user query and
    returns a routing dict {intent, league, search_terms} or None. When wired
    up (in bot.py we pass ClaudeClient.classify_market_intent), Haiku handles
    intent + league detection + search-term extraction in one call. When None,
    we fall back to the legacy regex (classify_intent + detect_league here).
    """

    def __init__(
        self,
        sgo_api_key: str | None,
        intent_classifier: IntentClassifier | None = None,
    ) -> None:
        self.sgo = SportsGameOddsClient(sgo_api_key)
        self.polymarket = PolymarketClient()
        self.kalshi = KalshiClient()
        self._classifier = intent_classifier

    async def get_context(self, query: str) -> list[MarketSnapshot] | None:
        """Fetch live market snapshots for a query if intent matches.

        Returns a list of MarketSnapshot ready to pass into claude.* methods
        for prompt injection, or None if no intent / no useful data.
        Fail-open: any downstream error returns None, never raises.
        """
        intent_data = await self._classify(query)
        if intent_data is None:
            return None
        intent = intent_data.get("intent")
        try:
            if intent == "sports":
                league = intent_data.get("league") or "NBA"
                return await self._sports_snapshots(league)
            if intent == "prediction_market":
                search_terms = intent_data.get("search_terms") or query
                return await self._pm_snapshots(search_terms)
        except Exception as exc:  # belt + suspenders; clients already swallow
            emit_error(
                source="markets_manager",
                exc=exc,
                recoverable=True,
                context={"intent": intent, "query_chars": len(query)},
            )
        return None

    async def _classify(self, query: str) -> dict[str, Any] | None:
        """Route to the Haiku classifier if wired up, else fall back to regex."""
        if self._classifier is not None:
            try:
                return await self._classifier(query)
            except Exception as exc:
                emit_error(
                    source="markets_classifier",
                    exc=exc,
                    recoverable=True,
                    context={"query_chars": len(query)},
                )
                # Fall through to regex on classifier failure.
        intent = classify_intent(query)
        if intent is None:
            return None
        out: dict[str, Any] = {"intent": intent, "search_terms": query}
        if intent == "sports":
            league = detect_league(query)
            if league:
                out["league"] = league
        return out

    async def _sports_snapshots(self, league: str) -> list[MarketSnapshot] | None:
        if not self.sgo.enabled:
            return None
        snaps = await self.sgo.get_event_odds(league, purpose="ask")
        return snaps[:5] if snaps else None

    async def _pm_snapshots(self, search_terms: str) -> list[MarketSnapshot] | None:
        # Polymarket search first (broader topic coverage), Kalshi fallback.
        snaps = await self.polymarket.search_markets(search_terms, limit=5)
        if snaps:
            return snaps
        kalshi_snaps = await self.kalshi.get_open_markets(limit=5)
        return kalshi_snaps[:3] if kalshi_snaps else None
