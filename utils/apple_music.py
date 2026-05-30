"""Deterministic Apple Music link resolution via the public iTunes Search API.

The music-lounge channel is links-only: every post must end with a real
`music.apple.com` URL or mods delete it. Originally the model web_searched for
that exact link itself, which spiraled into 5-7 serial `site:music.apple.com`
searches chasing one URL (25-73s music_post calls, the latency bug capped in
#155).

This resolves the link in code instead: the model only has to NAME the track
(`TRACK: <artist> - <title>`); we hit the iTunes Search API to get the canonical
Apple Music URL. The API is free, needs no auth, returns real `music.apple.com`
links, and returns zero results on a genuine miss, so the link is always real
or absent, never hallucinated, and resolution is one fast request instead of a
search spiral.

Fail-open by design: any error, timeout, or miss returns None and the caller
decides what to do (pick another track / skip the slot). We NEVER raise into a
cog. No new dependency, just aiohttp which we already have.
"""

from __future__ import annotations

import logging
import time
from typing import Any

import aiohttp

from utils.events import emit, emit_error

log = logging.getLogger(__name__)

_API_URL = "https://itunes.apple.com/search"
# Apple's endpoint is usually <300ms; 6s captures the slow tail without
# extending music_post latency (this replaces a multi-search spiral).
_TIMEOUT_SECONDS = 6.0


def pick_apple_music_url(payload: dict[str, Any]) -> str | None:
    """Pull the canonical music.apple.com URL out of an iTunes Search response.

    Prefers the per-track view URL (`trackViewUrl`) and falls back to the
    collection/album URL. Only accepts music.apple.com links (the API can also
    return podcast/app results if the entity filter is loose). Pure function so
    the parsing is unit-testable without a network call.
    """
    results = payload.get("results")
    if not isinstance(results, list):
        return None
    for result in results:
        if not isinstance(result, dict):
            continue
        for key in ("trackViewUrl", "collectionViewUrl"):
            url = result.get(key)
            if isinstance(url, str) and "music.apple.com" in url:
                return url
    return None


async def resolve_apple_music_url(
    query: str, *, session: aiohttp.ClientSession | None = None,
) -> str | None:
    """Resolve a free-text 'artist title' query to an Apple Music URL, or None.

    Fail-open: returns None on empty query, HTTP error, timeout, bad JSON, or no
    match. Emits an `apple_music_lookup` event either way for dashboards.
    """
    q = (query or "").strip()
    if not q:
        return None

    params = {"term": q, "entity": "song", "limit": "3"}
    start = time.monotonic()
    own_session = session is None
    sess = session or aiohttp.ClientSession(
        timeout=aiohttp.ClientTimeout(total=_TIMEOUT_SECONDS)
    )
    try:
        async with sess.get(_API_URL, params=params) as resp:
            resp.raise_for_status()
            payload = await resp.json(content_type=None)
        url = pick_apple_music_url(payload)
        emit(
            "apple_music_lookup",
            query=q[:120], ok=True, hit=url is not None,
            duration_ms=int((time.monotonic() - start) * 1000),
        )
        return url
    except Exception as exc:
        emit_error(source="apple_music", exc=exc, recoverable=True)
        emit(
            "apple_music_lookup",
            query=q[:120], ok=False, hit=False,
            duration_ms=int((time.monotonic() - start) * 1000),
        )
        return None
    finally:
        if own_session:
            await sess.close()
