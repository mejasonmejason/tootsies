"""Perplexity Sonar search: real-time web context for trending topics and social discourse.

Complements the existing web_search tool and link_enrich pipeline. Where
link_enrich pre-fetches specific URLs (a tweet, a TikTok) and web_search is a
generic search engine the model calls mid-generation, Perplexity fills the gap
on "what's trending right now" and Twitter/X discourse that neither of the
other two can see.

Uses the OpenAI-compatible chat completions endpoint (POST /v1/chat/completions)
with the `sonar` model. No new dependencies needed, just aiohttp which we already
have.

Fail-open by design: if PERPLEXITY_API_KEY is unset, or the API errors, or the
response is unparseable, the caller gets None and falls through to the existing
web_search + link_enrich pipeline. We NEVER bubble an exception into a cog.
"""

from __future__ import annotations

import logging
import time
from typing import Any

import aiohttp

from utils.events import emit, emit_error
from utils.url_guardrail import ensure_protocol

log = logging.getLogger(__name__)

_API_URL = "https://api.perplexity.ai/chat/completions"
_MODEL = "sonar"
# 5s was too tight: production saw recurring TimeoutErrors on /ask under normal
# Sonar latency. 8s captures the slow-but-functional tail without extending
# user-facing latency (the call runs in parallel with Claude, which dominates).
_TIMEOUT_SECONDS = 8.0

# Per-surface Sonar search controls. Without these the API inherits its defaults,
# the worst of which is search_context_size="low" (shallowest retrieval), which
# was producing low-signal evergreen filler and "I can't verify live trends"
# hedging. We bump every surface to "medium" context (deeper retrieval, ~modest
# cost bump vs the doubling that "high" would cost across all 5 callers) and add
# a recency window where freshness is the whole point.
#
#   - music / discourse: trend surfaces, want THIS WEEK's drops, not evergreen.
#   - recap / chimein:    breaking news / live events, want the last day.
#   - ask:                NO recency filter. It leads with fact verification
#                         (record counts, chart totals, "first since" claims)
#                         that live on evergreen authoritative pages (Wikipedia,
#                         Billboard discographies); a day/week window would hide
#                         exactly the pages it needs.
#
# recency values: hour/day/week/month/year. Cannot combine with date filters.
#
# FUTURE (cinema): the "cinema" discourse category inherits discourse's `week`
# window, but film/TV discourse has a longer half-life than sports/pop -- awards
# buzz spans months, and people keep debating a release for weeks after it drops.
# `week` may clamp out a movie from 2-3 weeks ago that the room is still on. This
# config is keyed by PURPOSE, not category, so giving cinema its own (e.g.
# `month`) recency means threading `category` into the param resolution here +
# in the callers, not just `purpose`. Revisit if cinema discourse feels stale.
_DEFAULT_SEARCH_CONFIG: dict[str, Any] = {"context": "medium", "recency": None}
_SEARCH_CONFIG: dict[str, dict[str, Any]] = {
    "ask": {"context": "medium", "recency": None},
    "discourse": {"context": "medium", "recency": "week"},
    "recap": {"context": "medium", "recency": "day"},
    "chimein": {"context": "medium", "recency": "day"},
    "music": {"context": "medium", "recency": "month"},
}

# Sentinel so callers can explicitly pass recency=None to DISABLE the per-surface
# recency default (used by the eval harness to A/B recency on vs off), distinct
# from omitting the arg (use the per-surface default).
_UNSET: Any = object()

# Phrases that mean Sonar punted instead of retrieving ("I can't verify live
# trends", "results are mostly playlist pages"). This is the exact symptom the
# per-surface search params exist to kill, so we tag each live response with a
# `hedged` flag to track the rate on a dashboard (not just in the offline eval).
# Single source of truth: scripts/eval_perplexity_params.py imports this.
_HEDGE_MARKERS: tuple[str, ...] = (
    "can't verify",
    "cannot verify",
    "couldn't find",
    "could not find",
    "do not have",
    "don't have access",
    "i'm not able to",
    "unable to verify",
    "no verifiable",
    "mostly youtube mixes",
    "playlist pages",
)


def is_hedged(text: str) -> bool:
    """True if a Sonar response reads as a non-answer (hedge) rather than facts.

    Substring match against `_HEDGE_MARKERS`, excluding the appended SOURCES
    block so a real URL containing a marker word can't false-positive.
    """
    body = text.split("SOURCES:", 1)[0].lower()
    return any(m in body for m in _HEDGE_MARKERS)


class PerplexityClient:
    """Thin async wrapper around the Perplexity Sonar API."""

    def __init__(self, api_key: str) -> None:
        self._api_key = api_key
        self._session: aiohttp.ClientSession | None = None

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                headers={
                    "Authorization": f"Bearer {self._api_key}",
                    "Content-Type": "application/json",
                },
                timeout=aiohttp.ClientTimeout(total=_TIMEOUT_SECONDS),
            )
        return self._session

    async def close(self) -> None:
        if self._session is not None and not self._session.closed:
            await self._session.close()
        self._session = None

    async def search(
        self,
        query: str,
        *,
        purpose: str = "unknown",
        search_context_size: str | None = None,
        recency: Any = _UNSET,
    ) -> str | None:
        """Run a search query through Perplexity Sonar.

        Search controls (retrieval depth + recency window) are resolved
        per-`purpose` from `_SEARCH_CONFIG`. Callers normally just pass
        `purpose`; `search_context_size` and `recency` are explicit overrides
        for the eval harness (pass `recency=None` to disable the window).

        Returns the text response (with citations baked in) or None on any
        failure. Emits a `pplx_<purpose>` event for dashboard tracking.
        """
        start = time.monotonic()
        ok = True
        input_tokens = 0
        output_tokens = 0
        cfg = _SEARCH_CONFIG.get(purpose, _DEFAULT_SEARCH_CONFIG)
        context_size = search_context_size or cfg["context"]
        recency_filter = cfg.get("recency") if recency is _UNSET else recency
        try:
            sess = await self._get_session()
            payload: dict[str, Any] = {
                "model": _MODEL,
                "messages": [
                    {"role": "user", "content": query},
                ],
                "web_search_options": {"search_context_size": context_size},
            }
            if recency_filter:
                payload["search_recency_filter"] = recency_filter
            async with sess.post(_API_URL, json=payload) as resp:
                if resp.status != 200:
                    ok = False
                    emit(
                        f"pplx_{purpose}",
                        ok=False,
                        duration_ms=int((time.monotonic() - start) * 1000),
                        error=f"HTTP {resp.status}",
                    )
                    return None
                data = await resp.json()

            usage = data.get("usage") or {}
            input_tokens = usage.get("prompt_tokens", 0)
            output_tokens = usage.get("completion_tokens", 0)

            choices = data.get("choices") or []
            if not choices:
                ok = False
                emit(
                    f"pplx_{purpose}",
                    ok=False,
                    duration_ms=int((time.monotonic() - start) * 1000),
                    error="no_choices",
                )
                return None

            text = (choices[0].get("message") or {}).get("content") or ""
            text = text.strip()
            if not text:
                ok = False

            # Sonar returns citations either as a top-level `citations` list of URL
            # strings (older shape) or a `search_results` list of {title,url,date}
            # objects (newer shape). Append them as a SOURCES block so prompts can
            # actually link the underlying article/post instead of paraphrasing.
            citation_urls: list[str] = []
            raw_citations = data.get("citations")
            if isinstance(raw_citations, list):
                citation_urls = [c for c in raw_citations if isinstance(c, str) and c]
            if not citation_urls:
                raw_results = data.get("search_results")
                if isinstance(raw_results, list):
                    citation_urls = [
                        sr["url"] for sr in raw_results
                        if isinstance(sr, dict) and isinstance(sr.get("url"), str)
                    ]
            source_count = 0
            if citation_urls and text:
                citation_urls = [ensure_protocol(u) for u in citation_urls]
                shown = citation_urls[:10]
                source_count = len(shown)
                sources_lines = "\n".join(
                    f"  [{i + 1}] {url}" for i, url in enumerate(shown)
                )
                text = f"{text}\n\nSOURCES:\n{sources_lines}"

            emit(
                f"pplx_{purpose}",
                ok=ok,
                duration_ms=int((time.monotonic() - start) * 1000),
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                response_chars=len(text),
                hedged=is_hedged(text) if text else False,
                source_count=source_count,
                context_size=context_size,
                recency=recency_filter or "off",
            )
            return text if text else None

        except (aiohttp.ClientError, TimeoutError, ValueError, KeyError) as exc:
            emit_error(
                source=f"pplx_{purpose}",
                exc=exc,
                recoverable=True,
            )
            emit(
                f"pplx_{purpose}",
                ok=False,
                duration_ms=int((time.monotonic() - start) * 1000),
                error=type(exc).__name__,
            )
            return None


_SOURCES = (
    "Pull from Twitter/X, Reddit, Instagram/Threads, TikTok, YouTube, "
    "and news outlets. Include specific names, numbers, and quotes."
)

_CATEGORY_QUERIES: dict[str, str] = {
    "nba": (
        "What's happening in the NBA right now? Latest scores, trades, "
        "signings, beef, injury updates, and standout performances. "
        "Check Twitter/X, r/nba, ESPN, The Athletic, and Shaderoom. "
        "Include what fans are debating and any viral moments. " + _SOURCES
    ),
    "sports": (
        "What are the biggest sports stories right now? Scores, upsets, "
        "trades, drama, records broken across NFL, NBA, soccer, UFC, MLB, "
        "and anything else popping. Check Twitter/X, Reddit sports subs, "
        "ESPN, Bleacher Report, and SportsCenter. " + _SOURCES
    ),
    "hiphop": (
        "What's happening in hip hop and R&B right now? New drops, beefs, "
        "surprise releases, viral moments, chart milestones, producer drama, "
        "sample discoveries, tour announcements. Check Twitter/X, "
        "r/hiphopheads, Complex, Pitchfork, Genius, YouTube new releases, "
        "and TikTok trending sounds. " + _SOURCES
    ),
    "rnb": (
        "What's happening in R&B and soul music right now? New releases, "
        "comeback albums, collaborations, viral covers, underrated drops, "
        "Grammy buzz. Check Twitter/X, r/rnb, r/hiphopheads, Complex, "
        "Pitchfork, Genius, Apple Music editorial, and TikTok trending "
        "sounds. " + _SOURCES
    ),
    "afrobeats": (
        "What's happening in afrobeats, amapiano, and dancehall right now? "
        "New releases, crossover hits, festival lineups, viral moments, "
        "collaborations with Western artists. Check Twitter/X, Apple Music "
        "Africa charts, Audiomack trending, Complex, OkayAfrica, and "
        "TikTok trending sounds. " + _SOURCES
    ),
    "neo-soul": (
        "What's happening in neo-soul, alternative R&B, and gospel-adjacent "
        "music right now? New releases, underground drops, indie artists "
        "breaking through, live session videos, sample-heavy production. "
        "Check Twitter/X, Bandcamp trending, r/rnb, Pitchfork, COLORS "
        "sessions, and NPR Tiny Desk. " + _SOURCES
    ),
    "music": (
        "What's the biggest music news right now across hip hop, R&B, pop, "
        "afrobeats, and Latin? New album drops, surprise releases, beefs, "
        "chart milestones, viral moments, tour announcements, producer "
        "drama, sample discoveries. Check Twitter/X, r/hiphopheads, "
        "Complex, Pitchfork, Genius, Apple Music editorial, and TikTok "
        "trending sounds. " + _SOURCES
    ),
    "pop": (
        "What's trending in pop culture right now? Celebrity news, viral "
        "moments, music drops, TV premieres, social media drama, memes, "
        "fashion moments, relationship news. Check Twitter/X, Instagram/"
        "Threads, TikTok trends, TMZ, People, Shaderoom, and Reddit. "
        + _SOURCES
    ),
    "cinema": (
        "What's happening in movies and TV right now? Box office results, "
        "new trailers, casting announcements, streaming drops, hot takes "
        "on recent releases, awards buzz, behind-the-scenes drama. Check "
        "Twitter/X, r/movies, r/television, Letterboxd trending, YouTube "
        "trailers, Variety, and Deadline. " + _SOURCES
    ),
}

_DEFAULT_TRENDING = (
    "What are the biggest stories trending RIGHT NOW across sports, "
    "hip hop, pop culture, movies/TV, and entertainment? Give me the "
    "top 3-5 things people are talking about in the last few hours.\n\n"
    "For each story include: what happened, who's involved, and what "
    "the hottest take or debate is.\n\n"
    "Look at: Twitter/X trending, Reddit front page and popular posts "
    "on r/nba r/hiphopheads r/movies r/popculture, Instagram/Threads "
    "viral posts, TikTok trending, YouTube trending, and news from "
    "ESPN, TMZ, Complex, Pitchfork, Shaderoom, Variety, and Bleacher "
    "Report. " + _SOURCES
)


def build_search_query(
    user_input: str,
    *,
    surface: str,
    category: str | None = None,
    channel_name: str | None = None,
    channel_topic: str | None = None,
) -> str:
    """Build a Perplexity search query tailored to the surface.

    The goal is to pull real-time trending context that the existing
    web_search tool and link_enrich pipeline miss: what Twitter/X is
    saying right now, breaking news, fresh facts, ambient culture signal.
    """
    if surface == "ask":
        return (
            f"Question: {user_input}\n\n"
            "First, surface any VERIFIABLE FACTS the question depends on "
            "(current counts, records, totals, chart positions, dates, "
            '"most ever" / "first since" claims). Pull exact values from '
            "authoritative sources (Wikipedia, Billboard, ESPN, official "
            "league sites, label/artist pages, news outlets). If a specific "
            "number or record is being asked about, give the precise current "
            "value with the source.\n\n"
            "Then, the latest news and discourse on the topic. Check "
            "Twitter/X, Reddit, Instagram/Threads, TikTok, YouTube, and "
            "news outlets for breaking developments, trending takes, fan "
            "reactions from the last 24 hours."
        )

    if surface == "discourse":
        if category and category in _CATEGORY_QUERIES:
            return _CATEGORY_QUERIES[category]
        # Prefer the channel's description (theme) over its bare name: "movies,
        # tv, film talk" steers the search far better than "screening-room".
        topic = category or (channel_topic or "").strip() or channel_name or ""
        if topic:
            return (
                f"What's trending right now that's relevant to '{topic}'? "
                "Give me the top 3-5 stories people are talking about in "
                "the last few hours.\n\n"
                "For each story include: what happened, who's involved, "
                "and what the hottest take or debate is.\n\n" + _SOURCES
            )
        return _DEFAULT_TRENDING

    if surface == "recap":
        return (
            f"What's the latest news about: {user_input}\n"
            "Check Twitter/X, Reddit, Instagram/Threads, TikTok, YouTube, "
            "and news outlets. Focus on the last few hours. Include scores, "
            "results, breaking developments, fan reactions, and any facts "
            "that would help someone catch up on what happened."
        )

    if surface == "chimein":
        return (
            f"What's the latest on: {user_input}\n"
            "Check Twitter/X, Reddit, TikTok, and news outlets. Any "
            "breaking news, scores, hot takes, viral moments, or new "
            "developments from the last few hours? Be specific."
        )

    return _DEFAULT_TRENDING


def format_perplexity_for_prompt(result: str) -> str:
    """Format Perplexity search results for injection into a Claude prompt."""
    return (
        "REAL-TIME SEARCH CONTEXT (from Perplexity, pulled live from authoritative "
        "sources and social media for THIS question). Treat the specific numbers, "
        "dates, names, counts, and records in this block as the current ground "
        "truth and use them VERBATIM. If a value here disagrees with what you "
        "remember from training, the value here wins, your training data is months "
        "stale. Don't mention Perplexity by name. URLs in the SOURCES block are "
        "real and linkable:\n"
        f"{result}"
    )
