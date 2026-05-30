"""Apple Music link resolution via the public iTunes Search API.

One provider behind the post-enrichment pattern (utils/music_links): given a
track NAME the model produced, return a real Apple Music streaming URL. The API
is free, needs no auth, returns canonical music.apple.com links, and returns
zero results on a genuine miss, so the link is always real or absent, never
hallucinated, resolved in one fast request instead of a model search spiral.

"Apple Music, not buy a song": we only accept streamable song results and strip
the iTunes-Store affiliate/tracking param (uo) so the link opens the track on
Apple Music rather than routing through the Store. The `i=` deep-link param
(the specific track within its album) is kept.

Fail-open by design: any error, timeout, or miss returns None. We NEVER raise
into a caller. No new dependency, just aiohttp which we already have.

Why no extra artist/title verification: adversarial probing of the live API
showed iTunes is robust where it matters. Misspellings fuzzy-match correctly
("Kendrik Lamarr - Luthor" -> Kendrick Lamar "luther"); nonexistent tracks,
fake artists, and garbled titles all return zero results (resolve to None, no
hallucinated link); and the artist name genuinely anchors the match (Adele /
Beyonce / Lionel Richie "Hello" each return the right artist's song). The only
residual gap is wrong-artist ATTRIBUTION ("Drake - Sicko Mode" -> Travis
Scott's "SICKO MODE"): the title wins, but the link still points at a real,
correctly-titled song, only the model's prose had the wrong artist. Deemed not
worth a verification layer that would risk rejecting legit fuzzy matches.
"""

from __future__ import annotations

import logging
import time
from typing import Any
from urllib.parse import parse_qs, urlencode, urlsplit, urlunsplit

import aiohttp

from utils.events import emit, emit_error

log = logging.getLogger(__name__)

_API_URL = "https://itunes.apple.com/search"
# Apple's endpoint is usually <300ms; 6s captures the slow tail without
# extending music_post latency (this replaces a multi-search spiral).
_TIMEOUT_SECONDS = 6.0
# Query params we keep on the Apple Music URL. `i` deep-links the specific
# track within its album page; everything else (notably `uo`, the iTunes-Store
# affiliate tracking param) is dropped so the link is a clean Apple Music page.
_KEEP_PARAMS = {"i"}


def _clean_apple_url(url: str) -> str:
    """Strip Store/affiliate tracking params, keep the track deep-link (`i`)."""
    parts = urlsplit(url)
    kept = {
        k: v for k, v in parse_qs(parts.query).items() if k in _KEEP_PARAMS
    }
    query = urlencode({k: v[0] for k, v in kept.items()})
    return urlunsplit((parts.scheme, parts.netloc, parts.path, query, ""))


def pick_apple_music_url(payload: dict[str, Any]) -> str | None:
    """Pull a clean, streamable Apple Music URL out of an iTunes Search response.

    Only accepts results that are songs, streamable on Apple Music, and whose
    view URL is on music.apple.com (the API can also return non-streamable or
    store-only items). Prefers the per-track view URL, falls back to the
    collection URL. Pure function so the parsing is unit-testable without a
    network call.
    """
    results = payload.get("results")
    if not isinstance(results, list):
        return None
    for result in results:
        if not isinstance(result, dict):
            continue
        # Apple Music streaming only: a non-streamable result is buy-only.
        if result.get("isStreamable") is False:
            continue
        if result.get("kind") not in (None, "song"):
            continue
        for key in ("trackViewUrl", "collectionViewUrl"):
            url = result.get(key)
            if isinstance(url, str) and "music.apple.com" in url:
                return _clean_apple_url(url)
    return None


async def resolve_apple_music_url(
    query: str, *, session: aiohttp.ClientSession | None = None,
) -> str | None:
    """Resolve a free-text 'artist title' query to an Apple Music URL, or None.

    Fail-open: returns None on empty query, HTTP error, timeout, bad JSON, or no
    streamable match. Emits an `apple_music_lookup` event either way.
    """
    q = (query or "").strip()
    if not q:
        return None

    params = {"term": q, "entity": "song", "limit": "5"}
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
