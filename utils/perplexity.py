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

log = logging.getLogger(__name__)

_API_URL = "https://api.perplexity.ai/chat/completions"
_MODEL = "sonar"
# 5s was too tight: production saw recurring TimeoutErrors on /ask under normal
# Sonar latency. 8s captures the slow-but-functional tail without extending
# user-facing latency (the call runs in parallel with Claude, which dominates).
_TIMEOUT_SECONDS = 8.0


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

    async def search(self, query: str, *, purpose: str = "unknown") -> str | None:
        """Run a search query through Perplexity Sonar.

        Returns the text response (with citations baked in) or None on any
        failure. Emits a `pplx_<purpose>` event for dashboard tracking.
        """
        start = time.monotonic()
        ok = True
        input_tokens = 0
        output_tokens = 0
        try:
            sess = await self._get_session()
            payload: dict[str, Any] = {
                "model": _MODEL,
                "messages": [
                    {"role": "user", "content": query},
                ],
            }
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
            if citation_urls and text:
                sources_lines = "\n".join(
                    f"  [{i + 1}] {url}" for i, url in enumerate(citation_urls[:10])
                )
                text = f"{text}\n\nSOURCES:\n{sources_lines}"

            emit(
                f"pplx_{purpose}",
                ok=ok,
                duration_ms=int((time.monotonic() - start) * 1000),
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                response_chars=len(text),
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
) -> str:
    """Build a Perplexity search query tailored to the surface.

    The goal is to pull real-time trending context that the existing
    web_search tool and link_enrich pipeline miss: what Twitter/X is
    saying right now, breaking news, fresh facts, ambient culture signal.
    """
    if surface == "ask":
        return (
            f"What's the latest news and discourse about: {user_input}\n"
            "Check Twitter/X, Reddit, Instagram/Threads, TikTok, YouTube, "
            "and news outlets. Include breaking developments, trending "
            "takes, relevant scores or stats, fan reactions, and any "
            "facts from the last 24 hours."
        )

    if surface == "discourse":
        if category and category in _CATEGORY_QUERIES:
            return _CATEGORY_QUERIES[category]
        topic = category or channel_name or ""
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
        "REAL-TIME SEARCH CONTEXT (from Perplexity, current trending discourse "
        "and social media; use to ground your take in what people are actually "
        "saying right now. Don't mention Perplexity by name, but URLs in the "
        "SOURCES block are real and linkable):\n"
        f"{result}"
    )
