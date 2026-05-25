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
_TIMEOUT_SECONDS = 5.0


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
        failure. Emits a `perplexity_search` event for dashboard tracking.
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
                        "perplexity_search",
                        purpose=purpose,
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
                    "perplexity_search",
                    purpose=purpose,
                    ok=False,
                    duration_ms=int((time.monotonic() - start) * 1000),
                    error="no_choices",
                )
                return None

            text = (choices[0].get("message") or {}).get("content") or ""
            text = text.strip()
            if not text:
                ok = False

            emit(
                "perplexity_search",
                purpose=purpose,
                ok=ok,
                duration_ms=int((time.monotonic() - start) * 1000),
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                response_chars=len(text),
            )
            return text if text else None

        except (aiohttp.ClientError, TimeoutError, ValueError, KeyError) as exc:
            emit_error(
                source="perplexity_search",
                exc=exc,
                recoverable=True,
                context={"purpose": purpose},
            )
            emit(
                "perplexity_search",
                purpose=purpose,
                ok=False,
                duration_ms=int((time.monotonic() - start) * 1000),
                error=type(exc).__name__,
            )
            return None


_CATEGORY_QUERIES: dict[str, str] = {
    "nba": (
        "What's happening in the NBA right now? Latest scores, trades, "
        "signings, beef, and what Twitter/X is debating. Include any "
        "breaking news from the last few hours."
    ),
    "sports": (
        "What are the biggest sports stories right now? Scores, upsets, "
        "trades, drama, records broken. What's trending on Twitter/X in "
        "sports today? Include NFL, NBA, soccer, UFC, anything popping."
    ),
    "hiphop": (
        "What's happening in hip hop right now? New drops, beefs, "
        "surprise releases, viral moments, chart milestones, producer "
        "drama. What's Twitter/X debating in rap and R&B today?"
    ),
    "pop": (
        "What's trending in pop culture right now? Celebrity news, viral "
        "moments, music drops, TV premieres, social media drama, memes. "
        "What's everyone on Twitter/X talking about today?"
    ),
    "cinema": (
        "What's happening in movies and TV right now? Box office results, "
        "new trailers, casting announcements, streaming drops, hot takes "
        "on recent releases. What's Twitter/X debating in film and TV?"
    ),
}

_DEFAULT_TRENDING = (
    "What's trending right now on Twitter/X and social media? Cover the "
    "biggest stories across sports, music, pop culture, and entertainment "
    "in the last few hours. Include specific names, scores, takes, and "
    "any viral moments or breaking news."
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
            f"What's the latest news and what are people saying on "
            f"Twitter/X about: {user_input}\n"
            "Include breaking developments, trending takes, relevant "
            "scores or stats, and any facts from the last 24 hours."
        )

    if surface == "discourse":
        if category and category in _CATEGORY_QUERIES:
            return _CATEGORY_QUERIES[category]
        if channel_name:
            inferred = _infer_category_from_channel(channel_name)
            if inferred:
                return _CATEGORY_QUERIES[inferred]
        return _DEFAULT_TRENDING

    if surface == "recap":
        return (
            f"What's the latest news about: {user_input}\n"
            "Focus on the last few hours. Include scores, results, "
            "breaking developments, Twitter/X reactions, and any facts "
            "that would help someone catch up on what happened."
        )

    if surface == "chimein":
        return (
            f"What's the latest on: {user_input}\n"
            "Any breaking news, scores, hot takes, or new developments "
            "from the last few hours? Be specific with names and facts."
        )

    return _DEFAULT_TRENDING


def _infer_category_from_channel(name: str) -> str | None:
    """Best-effort category guess from a channel name."""
    lower = name.lower().replace("-", " ").replace("_", " ")
    if any(w in lower for w in ("nba", "basketball", "hoops")):
        return "nba"
    if any(w in lower for w in ("sport", "football", "nfl", "soccer", "ufc", "mlb")):
        return "sports"
    if any(w in lower for w in ("hip hop", "hiphop", "rap", "music", "rnb")):
        return "hiphop"
    if any(w in lower for w in ("movie", "film", "cinema", "tv", "show", "stream")):
        return "cinema"
    if any(w in lower for w in ("pop", "culture", "celeb", "gossip", "tea")):
        return "pop"
    return None


def format_perplexity_for_prompt(result: str) -> str:
    """Format Perplexity search results for injection into a Claude prompt."""
    return (
        "REAL-TIME SEARCH CONTEXT (from Perplexity, current trending discourse "
        "and social media; use to ground your take in what people are actually "
        "saying right now, but do NOT quote this block or mention Perplexity):\n"
        f"{result}"
    )
