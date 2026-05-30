"""Post-generation link enrichment: resolve a named track to a streaming URL.

The mirror image of the pre-enrichment pipeline (utils/link_enrich): pre-enrich
takes a URL the room shared and fetches its content INTO the prompt; post-enrich
takes a name the MODEL produced and resolves a real URL to append AFTER
generation. Same spirit (deterministic side-fetch, model doesn't drive it),
opposite direction.

Extensible by design: providers are tried in registration order and the first
hit wins, so adding Spotify later is a one-line append to _PROVIDERS, no caller
change. Today there's one provider (Apple Music via iTunes Search); music posts
go to a links-only channel and Apple Music is the house default.

Fail-open: a provider that errors or misses returns None and we fall through to
the next; if none hit, the caller gets None and decides what to do (retry / skip).
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable

from utils.apple_music import resolve_apple_music_url
from utils.events import emit

log = logging.getLogger(__name__)

# A music-link provider: (free-text "artist - title" query) -> URL or None.
# Async so providers can hit a network API. Register new ones (e.g. Spotify)
# by appending to _PROVIDERS; resolve_music_url tries them in order.
MusicLinkProvider = Callable[[str], Awaitable[str | None]]

_PROVIDERS: list[tuple[str, MusicLinkProvider]] = [
    ("apple_music", resolve_apple_music_url),
    # ("spotify", resolve_spotify_url),  # planned: add alongside, first hit wins
]


async def resolve_music_url(query: str) -> str | None:
    """Resolve a track name to a streaming URL, trying each provider in order.

    Returns the first provider's hit, or None if the query is empty or no
    provider has it. Emits `music_link_resolved` with which provider landed it
    (or none) for dashboards.
    """
    q = (query or "").strip()
    if not q:
        return None
    for name, provider in _PROVIDERS:
        try:
            url = await provider(q)
        except Exception:
            log.exception("music link provider %s failed", name)
            url = None
        if url:
            emit("music_link_resolved", provider=name, ok=True)
            return url
    emit("music_link_resolved", provider="none", ok=False)
    return None
