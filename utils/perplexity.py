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


def build_search_query(
    user_input: str,
    *,
    surface: str,
    category: str | None = None,
    channel_name: str | None = None,
) -> str:
    """Build a Perplexity search query tailored to the surface.

    The goal is to get real-time trending context that the existing web_search
    tool and link_enrich pipeline would miss: what Twitter/X is saying right
    now, trending topics, breaking discourse.
    """
    if surface == "ask":
        return (
            f"What are people saying on Twitter/X and social media about: {user_input}\n"
            "Include any trending discourse, hot takes, and recent developments."
        )

    if surface == "discourse":
        topic = category or channel_name or "culture"
        return (
            f"What's trending right now on Twitter/X about {topic}? "
            "What are the hottest takes and debates happening in the last few hours? "
            "Include specific tweets, takes, and discourse if possible."
        )

    if surface == "recap":
        return (
            f"What's the latest news and Twitter/X discourse about: {user_input}\n"
            "Focus on what happened in the last few hours. "
            "Include trending takes and reactions."
        )

    if surface == "chimein":
        return (
            f"What's Twitter/X saying right now about: {user_input}\n"
            "What are the hottest takes in the last hour?"
        )

    return f"Latest trending discussion about: {user_input}"


def format_perplexity_for_prompt(result: str) -> str:
    """Format Perplexity search results for injection into a Claude prompt."""
    return (
        "REAL-TIME SEARCH CONTEXT (from Perplexity, current trending discourse "
        "and social media; use to ground your take in what people are actually "
        "saying right now, but do NOT quote this block or mention Perplexity):\n"
        f"{result}"
    )
