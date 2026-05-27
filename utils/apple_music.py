"""Apple Music RSS charts fetcher.

Apple publishes free RSS feeds of top charts at
    https://rss.applemarketingtools.com/api/v2/{country}/music/most-played/{limit}/songs.json

No auth needed. We fetch top songs for hip-hop and pop, cache for 4 hours
(charts update ~daily), and format for Claude prompts. Fail-open: if the
fetch fails, callers just don't get chart context.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Any

import aiohttp

from utils.events import emit

log = logging.getLogger(__name__)

_TIMEOUT_SECONDS = 5.0
_CACHE_TTL_SECONDS = 4 * 3600  # 4 hours

_BASE = "https://rss.applemarketingtools.com/api/v2"

# Genre IDs from Apple's RSS generator:
# https://rss.applemarketingtools.com
_GENRE_FEEDS: dict[str, str] = {
    "all": f"{_BASE}/us/music/most-played/25/songs.json",
    "hiphop": f"{_BASE}/us/music/most-played/25/songs/genre=18/songs.json",
    "pop": f"{_BASE}/us/music/most-played/25/songs/genre=14/songs.json",
    "rnb": f"{_BASE}/us/music/most-played/25/songs/genre=15/songs.json",
}

_session: aiohttp.ClientSession | None = None


async def _get_session() -> aiohttp.ClientSession:
    global _session
    if _session is None or _session.closed:
        _session = aiohttp.ClientSession(
            headers={"User-Agent": "tootsies-bot (apple-music-charts)"},
            timeout=aiohttp.ClientTimeout(total=_TIMEOUT_SECONDS),
        )
    return _session


async def close_session() -> None:
    global _session
    if _session is not None and not _session.closed:
        await _session.close()
    _session = None


@dataclass
class ChartEntry:
    rank: int
    name: str
    artist: str
    url: str
    album: str = ""


# In-memory cache: genre -> (entries, fetched_at)
_cache: dict[str, tuple[list[ChartEntry], float]] = {}


async def _fetch_chart(genre: str) -> list[ChartEntry]:
    """Fetch a single genre chart from Apple's RSS feed."""
    url = _GENRE_FEEDS.get(genre)
    if not url:
        return []
    start = time.monotonic()
    try:
        sess = await _get_session()
        async with sess.get(url) as resp:
            if resp.status >= 400:
                emit(
                    "apple_music_fetch",
                    genre=genre, ok=False,
                    duration_ms=int((time.monotonic() - start) * 1000),
                    error=f"HTTP {resp.status}",
                )
                return []
            data: dict[str, Any] = await resp.json(content_type=None)
    except (aiohttp.ClientError, TimeoutError, ValueError) as exc:
        emit(
            "apple_music_fetch",
            genre=genre, ok=False,
            duration_ms=int((time.monotonic() - start) * 1000),
            error=type(exc).__name__,
        )
        return []

    results = (data.get("feed") or {}).get("results") or []
    entries: list[ChartEntry] = []
    for i, item in enumerate(results):
        if not isinstance(item, dict):
            continue
        name = item.get("name") or ""
        artist = item.get("artistName") or ""
        entry_url = item.get("url") or ""
        album = item.get("collectionName") or ""
        if name and artist:
            entries.append(ChartEntry(
                rank=i + 1, name=name, artist=artist,
                url=entry_url, album=album,
            ))

    emit(
        "apple_music_fetch",
        genre=genre, ok=True,
        duration_ms=int((time.monotonic() - start) * 1000),
        result_count=len(entries),
    )
    return entries


async def get_chart(genre: str = "all") -> list[ChartEntry]:
    """Get chart entries for a genre, with 4-hour cache."""
    now = time.monotonic()
    cached = _cache.get(genre)
    if cached:
        entries, fetched_at = cached
        if now - fetched_at < _CACHE_TTL_SECONDS:
            return entries

    entries = await _fetch_chart(genre)
    if entries:
        _cache[genre] = (entries, now)
    elif cached:
        return cached[0]
    return entries


async def get_charts_for_music_lounge() -> dict[str, list[ChartEntry]]:
    """Fetch hiphop + pop + rnb charts in parallel for music-lounge context."""
    genres = ["hiphop", "pop", "rnb"]
    results = await asyncio.gather(
        *(get_chart(g) for g in genres),
        return_exceptions=True,
    )
    out: dict[str, list[ChartEntry]] = {}
    for genre, result in zip(genres, results, strict=True):
        if isinstance(result, list):
            out[genre] = result
        else:
            log.warning("chart fetch failed for %s: %s", genre, result)
    return out


def format_charts_for_prompt(charts: dict[str, list[ChartEntry]], limit: int = 10) -> str:
    """Render chart data as a Claude-friendly block."""
    if not charts:
        return ""
    lines: list[str] = ["APPLE MUSIC CHARTS (what's trending right now):"]
    for genre, entries in charts.items():
        if not entries:
            continue
        lines.append(f"\n  {genre.upper()} TOP {min(limit, len(entries))}:")
        for entry in entries[:limit]:
            lines.append(
                f"    #{entry.rank} {entry.name} - {entry.artist}"
                + (f" ({entry.album})" if entry.album else "")
            )
            if entry.url:
                lines.append(f"      {entry.url}")
    return "\n".join(lines)
