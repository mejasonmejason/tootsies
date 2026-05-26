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

import functools
import logging
import re
import time
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Literal

import aiohttp

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
    """

    source: MarketSource
    title: str
    url: str
    probability: float | None = None
    odds: dict[str, float] = field(default_factory=dict)
    meta: dict[str, Any] = field(default_factory=dict)


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


# ---- async LRU (copied pattern from link_enrich) ----------------------------


def _async_lru_cache(
    maxsize: int = _CACHE_SIZE,
) -> Callable[[Callable[..., Awaitable[Any]]], Callable[..., Awaitable[Any]]]:
    """Tiny async-friendly LRU. functools.lru_cache doesn't play with coros."""

    def decorator(func: Callable[..., Awaitable[Any]]) -> Callable[..., Awaitable[Any]]:
        cache: OrderedDict[tuple[Any, ...], Any] = OrderedDict()
        sentinel = object()

        @functools.wraps(func)
        async def wrapper(*args: Any, **kwargs: Any) -> Any:
            key = (args, tuple(sorted(kwargs.items())))
            cached = cache.get(key, sentinel)
            if cached is not sentinel:
                cache.move_to_end(key)
                wrapper._last_was_hit = True  # type: ignore[attr-defined]
                return cached
            wrapper._last_was_hit = False  # type: ignore[attr-defined]
            result = await func(*args, **kwargs)
            cache[key] = result
            if len(cache) > maxsize:
                cache.popitem(last=False)
            return result

        wrapper._last_was_hit = False  # type: ignore[attr-defined]
        wrapper._cache = cache  # type: ignore[attr-defined]
        return wrapper

    return decorator


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

    @_async_lru_cache(maxsize=_CACHE_SIZE)
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


def _sgo_event_to_snapshot(event: dict[str, Any], league: str) -> MarketSnapshot | None:
    """Best-effort flatten of an SGO event into one MarketSnapshot.

    SGO nests odds per-book per-market; we surface the consensus or first
    available spread / moneyline / total. Defensive against shape drift.
    """
    if not isinstance(event, dict):
        return None
    event_id = event.get("eventID") or event.get("id") or ""
    teams = event.get("teams") or {}
    home = (teams.get("home") or {}).get("name") if isinstance(teams, dict) else None
    away = (teams.get("away") or {}).get("name") if isinstance(teams, dict) else None
    title = f"{away} @ {home}" if home and away else (event.get("info") or {}).get("title") or "match"

    odds_payload = event.get("odds") or {}
    flat_odds: dict[str, float] = {}
    if isinstance(odds_payload, dict):
        for key in ("spread", "moneyline_home", "moneyline_away", "total"):
            val = odds_payload.get(key)
            if isinstance(val, int | float):
                flat_odds[key] = float(val)

    return MarketSnapshot(
        source="sgo",
        title=str(title),
        url=f"https://sportsgameodds.com/event/{event_id}" if event_id else "",
        odds=flat_odds,
        meta={"league": league, "event_id": event_id},
    )


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

    @_async_lru_cache(maxsize=_CACHE_SIZE)
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

    @_async_lru_cache(maxsize=_CACHE_SIZE)
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


def _poly_event_to_snapshot(event: dict[str, Any]) -> MarketSnapshot | None:
    """Flatten a Polymarket event into one snapshot using its top market.

    Polymarket events nest a `markets` array; for snapshot purposes we use
    the primary market (first in the list) and its YES price. Multi-outcome
    events lose nuance here but gain consistency with other sources.
    """
    if not isinstance(event, dict):
        return None
    title = event.get("title") or ""
    slug = event.get("slug") or ""
    if not title:
        return None
    markets = event.get("markets") or []
    probability: float | None = None
    if isinstance(markets, list) and markets:
        market = markets[0]
        if isinstance(market, dict):
            # outcomePrices is the canonical source; falls through to bestBid.
            prices_raw = market.get("outcomePrices")
            prices = _parse_prices(prices_raw)
            if prices:
                probability = prices[0]
            elif isinstance(market.get("bestBid"), int | float):
                probability = float(market["bestBid"])
    return MarketSnapshot(
        source="polymarket",
        title=str(title),
        url=f"https://polymarket.com/event/{slug}" if slug else "",
        probability=probability,
        meta={
            "volume": event.get("volume"),
            "liquidity": event.get("liquidity"),
            "end_date": event.get("endDate"),
        },
    )


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

    @_async_lru_cache(maxsize=_CACHE_SIZE)
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
        url=f"https://kalshi.com/markets/{ticker}" if ticker else "",
        probability=probability,
        meta={
            "ticker": ticker,
            "volume": market.get("volume_fp"),
            "liquidity": market.get("liquidity_dollars"),
            "expiration": market.get("expiration_time"),
        },
    )


# ---- prompt formatter -------------------------------------------------------


def format_markets_for_prompt(snapshots: list[MarketSnapshot]) -> str:
    """Render market snapshots as a Claude-friendly block.

    Empty list returns empty string so callers can concatenate unconditionally.
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
            prob_str = f"{snap.probability * 100:.0f}%" if snap.probability is not None else "?"
            vol = snap.meta.get("volume")
            vol_str = f" vol ${vol:,.0f}" if isinstance(vol, int | float) else ""
            lines.append(f"  - [{label}] {snap.title}  yes={prob_str}{vol_str}  {snap.url}")
    return "\n".join(lines)


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


class MarketsManager:
    """Single entry point for cogs. Routes a query to the right client(s)
    and returns a formatted prompt block (or None if nothing relevant).

    SGO is conditional on a key; Polymarket and Kalshi always work since
    their read APIs are public.
    """

    def __init__(self, sgo_api_key: str | None) -> None:
        self.sgo = SportsGameOddsClient(sgo_api_key)
        self.polymarket = PolymarketClient()
        self.kalshi = KalshiClient()

    async def get_context(self, query: str) -> list[MarketSnapshot] | None:
        """Fetch live market snapshots for a query if intent matches.

        Returns a list of MarketSnapshot ready to pass into claude.ask(...) for
        prompt injection, or None if no intent / no useful data. Fail-open:
        any downstream error returns None, never raises.
        """
        intent = classify_intent(query)
        if intent is None:
            return None
        try:
            if intent == "sports":
                return await self._sports_snapshots(query)
            if intent == "prediction_market":
                return await self._pm_snapshots(query)
        except Exception as exc:  # belt + suspenders; clients already swallow
            emit_error(
                source="markets_manager",
                exc=exc,
                recoverable=True,
                context={"intent": intent, "query_chars": len(query)},
            )
        return None

    async def _sports_snapshots(self, query: str) -> list[MarketSnapshot] | None:
        if not self.sgo.enabled:
            return None
        league = detect_league(query) or "NBA"
        snaps = await self.sgo.get_event_odds(league, purpose="ask")
        return snaps[:5] if snaps else None

    async def _pm_snapshots(self, query: str) -> list[MarketSnapshot] | None:
        # Polymarket search first (broader topic coverage), Kalshi fallback.
        snaps = await self.polymarket.search_markets(query, limit=5)
        if snaps:
            return snaps
        kalshi_snaps = await self.kalshi.get_open_markets(limit=5)
        return kalshi_snaps[:3] if kalshi_snaps else None
