"""Social-link enricher: pre-fetch rich content for URLs we know how to read.

When users drop X, TikTok, YouTube, Reddit, or Bluesky links in chat, the
recap / discourse / chime-in prompts want more than just the bare URL. They
want the actual tweet text, the caption, the video title, the top comments.
Today they hand the URL to Claude with the web_search tool wired in, which
works on articles but is patchy on social (login walls, video-heavy pages,
JS-only frontends).

This module hits dedicated free endpoints per platform and returns a
normalized EnrichedLink. Callers feed those into the prompts via
`format_enriched_for_prompt` so Claude doesn't have to call web_search on
URLs we already pre-fetched.

Fail-open by design: any platform-detection miss, network error, or
endpoint hiccup returns None. The caller falls through to web_search.
We NEVER bubble an exception into a cog.
"""

from __future__ import annotations

import asyncio
import functools
import logging
import re
import time
import urllib.parse
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse

import aiohttp

from utils.events import emit, emit_error

log = logging.getLogger(__name__)

# Per-request timeout. The whole point is to enrich WITHOUT slowing the user-
# facing /recap or chime-in down, so we cap aggressively and silently skip if
# an endpoint is slow. 2 seconds is enough for the working free endpoints
# (fxtwitter, oembed, reddit json) and short enough that a stuck enricher
# can't dominate response latency.
_TIMEOUT_SECONDS = 2.0

# How many concurrent enrichments we'll run in a batch. Default 10 (recap /
# discourse); chime-in passes 5 because it's latency-sensitive.
_DEFAULT_CONCURRENCY = 10

# LRU cache size. 256 URLs is plenty: typical batches enrich 5-8 URLs and
# the same URL only repeats across batches (someone shares the same tweet
# in two channels, or /recap runs twice in a row).
_CACHE_SIZE = 256


# ---- dataclass --------------------------------------------------------------


@dataclass
class EnrichedLink:
    """Normalized representation of a pre-fetched social post.

    All optional except `platform` and `url`. `text` may be empty for media-
    only posts. `engagement` carries platform-specific counters (likes,
    reposts, plays, score, comments). `media_urls` is image/video URLs we
    surfaced from the post.
    """

    platform: str
    url: str
    author: str = ""
    text: str = ""
    title: str = ""
    media_urls: list[str] = field(default_factory=list)
    engagement: dict[str, int] = field(default_factory=dict)


# ---- URL detection ----------------------------------------------------------


# X / Twitter and its fixer variants. We pull the status id from the path.
_TWITTER_HOSTS = (
    "twitter.com", "x.com", "fxtwitter.com", "vxtwitter.com",
    "fixupx.com", "fixvx.com", "twittpr.com", "nitter.net",
)
_TWITTER_STATUS_RE = re.compile(r"/status/(\d+)")

# TikTok: canonical, fixer variants, and short URLs (vm.tiktok.com/abc).
# tikwm accepts any of these so we just hand the full URL through.
_TIKTOK_HOSTS = (
    "tiktok.com", "vm.tiktok.com", "tnktok.com", "vxtiktok.com",
)

# YouTube: watch URLs, short links, shorts.
_YOUTUBE_HOSTS = ("youtube.com", "www.youtube.com", "youtu.be", "m.youtube.com")

# Reddit: canonical + old.reddit + share short links.
_REDDIT_HOSTS = ("reddit.com", "www.reddit.com", "old.reddit.com", "redd.it")

# Bluesky: only the canonical app host (fxbsky.app doesn't expose the same JSON).
_BLUESKY_HOSTS = ("bsky.app",)
_BLUESKY_POST_RE = re.compile(r"/profile/([^/]+)/post/([^/?#]+)")


def _host(url: str) -> str:
    try:
        return urlparse(url).hostname or ""
    except (ValueError, TypeError):
        return ""


def _host_matches(url: str, hosts: tuple[str, ...]) -> bool:
    host = _host(url).lower()
    return any(host == h or host.endswith("." + h) for h in hosts)


def detect_platform(url: str) -> str | None:
    """Return the platform name if the URL is one we can enrich, else None."""
    if not url or not url.lower().startswith(("http://", "https://")):
        return None
    if _host_matches(url, _TWITTER_HOSTS) and _TWITTER_STATUS_RE.search(url):
        return "twitter"
    if _host_matches(url, _TIKTOK_HOSTS):
        return "tiktok"
    if _host_matches(url, _YOUTUBE_HOSTS):
        return "youtube"
    if _host_matches(url, _REDDIT_HOSTS):
        return "reddit"
    if _host_matches(url, _BLUESKY_HOSTS) and _BLUESKY_POST_RE.search(url):
        return "bluesky"
    return None


# ---- async LRU --------------------------------------------------------------


def _async_lru_cache(
    maxsize: int = 256,
) -> Callable[[Callable[..., Awaitable[Any]]], Callable[..., Awaitable[Any]]]:
    """Tiny async-friendly LRU. functools.lru_cache doesn't play with coros."""

    def decorator(func: Callable[..., Awaitable[Any]]) -> Callable[..., Awaitable[Any]]:
        cache: OrderedDict[tuple[Any, ...], Any] = OrderedDict()
        # Track most recent lookup so callers can record cache_hit in events
        # without changing the function signature. functools.wraps copies
        # __name__/__doc__ so log lines stay readable.
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


# ---- HTTP session (module-level, github.py pattern) -------------------------


_session: aiohttp.ClientSession | None = None


async def _get_session() -> aiohttp.ClientSession:
    global _session
    if _session is None or _session.closed:
        _session = aiohttp.ClientSession(
            headers={"User-Agent": "tootsies-bot (link-enrich)"},
            timeout=aiohttp.ClientTimeout(total=_TIMEOUT_SECONDS),
        )
    return _session


async def close_session() -> None:
    """Close the shared session, called on bot shutdown."""
    global _session
    if _session is not None and not _session.closed:
        await _session.close()
    _session = None


async def _fetch_json(url: str) -> dict[str, Any] | None:
    """GET a JSON URL. Returns None on any error (network, 4xx/5xx, parse).

    On 429 we emit a recoverable error event so log monitors can spot rate-
    limit pressure on a specific endpoint host.
    """
    try:
        sess = await _get_session()
        async with sess.get(url) as resp:
            if resp.status == 429:
                emit_error(
                    source="link_enrich",
                    exc=RuntimeError("HTTP 429"),
                    recoverable=True,
                    context={"endpoint_host": _host(url), "status": 429},
                )
                return None
            if resp.status >= 400:
                return None
            return await resp.json(content_type=None)
    except (aiohttp.ClientError, TimeoutError, ValueError):
        return None


# ---- per-platform enrichers -------------------------------------------------


def _truncate(text: str, n: int = 500) -> str:
    text = (text or "").strip()
    return text if len(text) <= n else text[: n - 1] + "…"


async def _enrich_twitter(url: str) -> EnrichedLink | None:
    """fxtwitter.com JSON API. Free, no auth, mirrors the upstream tweet.

    Endpoint shape: https://api.fxtwitter.com/<status_id> returns
    {"code": 200, "tweet": {"text": ..., "author": {...}, "likes": ..., ...}}
    """
    m = _TWITTER_STATUS_RE.search(url)
    if not m:
        return None
    status_id = m.group(1)
    data = await _fetch_json(f"https://api.fxtwitter.com/{status_id}")
    if not data or data.get("code") != 200:
        return None
    tweet = data.get("tweet") or {}
    author = tweet.get("author") or {}
    media_items = (tweet.get("media") or {}).get("all") or []
    media_urls: list[str] = [
        item["url"] for item in media_items
        if isinstance(item, dict) and isinstance(item.get("url"), str)
    ]
    engagement: dict[str, int] = {}
    for key in ("likes", "retweets", "replies", "views"):
        val = tweet.get(key)
        if isinstance(val, int):
            engagement[key] = val
    handle = author.get("screen_name") or author.get("name") or ""
    return EnrichedLink(
        platform="X/Twitter",
        url=url,
        author=f"@{handle}" if handle else "",
        text=_truncate(tweet.get("text") or ""),
        media_urls=media_urls[:4],
        engagement=engagement,
    )


async def _enrich_tiktok(url: str) -> EnrichedLink | None:
    """tikwm.com JSON wrapper. Free, no auth, handles vm.tiktok short links too.

    Endpoint shape: https://www.tikwm.com/api/?url=<encoded>
    returns {"code": 0, "data": {"title": ..., "author": {...}, "play_count": ...}}
    """
    encoded = urllib.parse.quote(url, safe="")
    data = await _fetch_json(f"https://www.tikwm.com/api/?url={encoded}")
    if not data or data.get("code") != 0:
        return None
    payload = data.get("data") or {}
    author = payload.get("author") or {}
    handle = author.get("unique_id") or author.get("nickname") or ""
    engagement: dict[str, int] = {}
    for src_key, dest_key in (
        ("play_count", "plays"),
        ("digg_count", "likes"),
        ("comment_count", "comments"),
        ("share_count", "shares"),
    ):
        val = payload.get(src_key)
        if isinstance(val, int):
            engagement[dest_key] = val
    media_urls: list[str] = []
    if payload.get("cover"):
        media_urls.append(payload["cover"])
    return EnrichedLink(
        platform="TikTok",
        url=url,
        author=f"@{handle}" if handle else "",
        text=_truncate(payload.get("title") or ""),
        media_urls=media_urls,
        engagement=engagement,
    )


async def _enrich_youtube(url: str) -> EnrichedLink | None:
    """YouTube oEmbed. Free, no auth. Gives us title, author, thumbnail.

    No view/like counts (oEmbed is metadata only). For the recap / discourse
    use case this is fine: Toots mostly needs to know WHAT the video is, not
    how viral it went.
    """
    encoded = urllib.parse.quote(url, safe="")
    data = await _fetch_json(
        f"https://www.youtube.com/oembed?url={encoded}&format=json"
    )
    if not data:
        return None
    thumb = data.get("thumbnail_url")
    return EnrichedLink(
        platform="YouTube",
        url=url,
        author=data.get("author_name") or "",
        text="",
        title=_truncate(data.get("title") or "", 200),
        media_urls=[thumb] if thumb else [],
        engagement={},
    )


async def _enrich_reddit(url: str) -> EnrichedLink | None:
    """Append .json to the post URL. Free, no auth needed for public threads.

    Reddit returns [post_listing, comments_listing]. We pull title + selftext
    from the post and the top 5 comment bodies.
    """
    # Normalize: strip trailing slash, ensure .json suffix.
    clean = url.split("?", 1)[0].rstrip("/")
    if not clean.endswith(".json"):
        clean = clean + ".json"
    # Reddit blocks default UA; the module session already sets a UA, but the
    # tikwm/oembed endpoints don't care so we only matter here.
    data = await _fetch_json(clean)
    if not isinstance(data, list) or len(data) < 1:
        return None
    try:
        post = data[0]["data"]["children"][0]["data"]
    except (KeyError, IndexError, TypeError):
        return None
    title = post.get("title") or ""
    selftext = post.get("selftext") or ""
    body_parts: list[str] = []
    if selftext:
        body_parts.append(_truncate(selftext, 400))
    # Top comments (up to 5, body only). Skip removed/empty.
    if len(data) >= 2:
        try:
            children = data[1]["data"]["children"]
        except (KeyError, TypeError):
            children = []
        comment_lines: list[str] = []
        for child in children[:8]:  # walk a bit further in case the top are stickies
            if len(comment_lines) >= 5:
                break
            cdata = child.get("data") or {}
            body = cdata.get("body")
            if not body or body in ("[deleted]", "[removed]"):
                continue
            comment_lines.append(f"> {_truncate(body, 200)}")
        if comment_lines:
            body_parts.append("top replies:\n" + "\n".join(comment_lines))
    engagement: dict[str, int] = {}
    if isinstance(post.get("score"), int):
        engagement["score"] = post["score"]
    if isinstance(post.get("num_comments"), int):
        engagement["comments"] = post["num_comments"]
    author = post.get("author") or ""
    return EnrichedLink(
        platform="Reddit",
        url=url,
        author=f"u/{author}" if author else "",
        text=_truncate("\n\n".join(body_parts), 800),
        title=_truncate(title, 200),
        media_urls=[],
        engagement=engagement,
    )


async def _enrich_bluesky(url: str) -> EnrichedLink | None:
    """Bluesky public API. Free, no auth needed for public posts.

    Endpoint: getPostThread expects an AT URI of the form
        at://<handle>/app.bsky.feed.post/<rkey>
    constructed from the bsky.app web URL.
    """
    m = _BLUESKY_POST_RE.search(url)
    if not m:
        return None
    handle, rkey = m.group(1), m.group(2)
    at_uri = f"at://{handle}/app.bsky.feed.post/{rkey}"
    encoded = urllib.parse.quote(at_uri, safe="")
    api = (
        "https://public.api.bsky.app/xrpc/app.bsky.feed.getPostThread"
        f"?uri={encoded}&depth=0"
    )
    data = await _fetch_json(api)
    if not data:
        return None
    thread = data.get("thread") or {}
    post = thread.get("post") or {}
    record = post.get("record") or {}
    author = post.get("author") or {}
    engagement: dict[str, int] = {}
    for src_key, dest_key in (
        ("likeCount", "likes"),
        ("repostCount", "reposts"),
        ("replyCount", "replies"),
    ):
        val = post.get(src_key)
        if isinstance(val, int):
            engagement[dest_key] = val
    handle_out = author.get("handle") or ""
    return EnrichedLink(
        platform="Bluesky",
        url=url,
        author=f"@{handle_out}" if handle_out else "",
        text=_truncate(record.get("text") or ""),
        media_urls=[],
        engagement=engagement,
    )


# ---- public API -------------------------------------------------------------


_ENRICHERS: dict[str, Callable[[str], Awaitable[EnrichedLink | None]]] = {
    "twitter": _enrich_twitter,
    "tiktok": _enrich_tiktok,
    "youtube": _enrich_youtube,
    "reddit": _enrich_reddit,
    "bluesky": _enrich_bluesky,
}


@_async_lru_cache(maxsize=_CACHE_SIZE)
async def _enrich_cached(url: str) -> EnrichedLink | None:
    """Cached enrichment helper. Wrapped by `enrich()` for event emission."""
    platform = detect_platform(url)
    if platform is None:
        return None
    enricher = _ENRICHERS[platform]
    return await enricher(url)


async def enrich(url: str) -> EnrichedLink | None:
    """Enrich a single URL. Returns None for unknown platforms or on failure.

    Emits a `link_enrich` event on every attempt (hit or miss, ok or fail)
    so dashboards can track per-platform reliability and cache hit rate.
    """
    platform = detect_platform(url)
    if platform is None:
        return None
    start = time.monotonic()
    ok = True
    try:
        result = await _enrich_cached(url)
        if result is None:
            ok = False
    except Exception as exc:
        # Defensive: an enricher shouldn't raise (each catches its own
        # network errors) but if one slips through, log and fail-open.
        emit_error(
            source="link_enrich",
            exc=exc,
            recoverable=True,
            context={"platform": platform, "url_host": _host(url)},
        )
        result = None
        ok = False
    cache_hit = bool(getattr(_enrich_cached, "_last_was_hit", False))
    emit(
        "link_enrich",
        platform=platform,
        url_host=_host(url),
        ok=ok,
        duration_ms=int((time.monotonic() - start) * 1000),
        cache_hit=cache_hit,
    )
    return result


async def enrich_batch(
    urls: list[str],
    concurrency: int = _DEFAULT_CONCURRENCY,
) -> dict[str, EnrichedLink | None]:
    """Enrich many URLs in parallel. One failure never blocks the others.

    `concurrency` caps how many enrichments fly at once. Default 10 is fine
    for /recap and /discourse. Chime-in passes 5 because that path is the
    most latency-sensitive (background tick, blocks the next chime-in eval).
    """
    if not urls:
        return {}
    # Dedupe while preserving first-seen order.
    seen: list[str] = []
    seen_set: set[str] = set()
    for u in urls:
        if u not in seen_set:
            seen.append(u)
            seen_set.add(u)

    sem = asyncio.Semaphore(max(1, concurrency))

    async def _one(u: str) -> tuple[str, EnrichedLink | None]:
        async with sem:
            try:
                return u, await enrich(u)
            except Exception:
                # Belt-and-suspenders: enrich() already catches, but if
                # someone bypasses it we still fail-open per URL.
                return u, None

    results = await asyncio.gather(
        *(_one(u) for u in seen), return_exceptions=True,
    )
    out: dict[str, EnrichedLink | None] = {}
    for entry in results:
        if isinstance(entry, BaseException):
            # asyncio.gather wrapped an unhandled exception; we don't know
            # which URL it was tied to, so just skip and continue.
            continue
        url, result = entry
        out[url] = result
    return out


# ---- prompt formatter -------------------------------------------------------


def format_enriched_for_prompt(enriched: list[EnrichedLink]) -> str:
    """Render enriched links as a Claude-friendly block.

    Returns empty string if the list is empty so callers can concatenate
    unconditionally without ending up with stray "ENRICHED LINKS" headers
    over nothing.
    """
    if not enriched:
        return ""
    lines: list[str] = [
        "ENRICHED LINKS (pre-fetched, use these directly; do NOT re-call "
        "web_search on these URLs):",
    ]
    for link in enriched:
        header_bits: list[str] = [f"[{link.platform}]"]
        if link.author:
            header_bits.append(link.author)
        if link.title:
            header_bits.append(f'"{_truncate(link.title, 120)}"')
        if link.text:
            header_bits.append(f'"{_truncate(link.text, 280)}"')
        # First line: source + author + headline-ish.
        lines.append("  - " + " ".join(header_bits))
        # Second line: engagement + media count + url for grounding.
        meta_bits: list[str] = []
        for k, v in link.engagement.items():
            meta_bits.append(f"{_humanize_count(v)} {k}")
        if link.media_urls:
            meta_bits.append(f"{len(link.media_urls)} media")
        meta_bits.append(link.url)
        lines.append("    " + ", ".join(meta_bits))
    return "\n".join(lines)


def _humanize_count(n: int) -> str:
    """1234 -> '1.2K', 1_500_000 -> '1.5M'. Keeps the prompt compact."""
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}K"
    return str(n)
