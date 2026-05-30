"""Music link resolution: Apple Music provider (utils.apple_music) + the
post-enrichment registry (utils.music_links) + the music_post TRACK split.

The music channel is links-only. Instead of the model web_searching for an
exact music.apple.com URL (the 25-73s spiral), it names the track and we resolve
a streaming link deterministically. These tests cover the iTunes response
parser (incl. Apple-Music-not-buy filtering + URL cleaning), the resolver's
fail-open behavior, the provider registry, and claude_client wiring.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from utils.apple_music import pick_apple_music_url, resolve_apple_music_url
from utils.music_links import resolve_music_url


# ---- Apple Music response parsing ------------------------------------------


def test_pick_url_prefers_track_view() -> None:
    payload = {"results": [{
        "kind": "song", "isStreamable": True,
        "trackViewUrl": "https://music.apple.com/us/song/x/1?i=2",
        "collectionViewUrl": "https://music.apple.com/us/album/x/1",
    }]}
    assert pick_apple_music_url(payload) == "https://music.apple.com/us/song/x/1?i=2"


def test_pick_url_falls_back_to_collection() -> None:
    payload = {"results": [{
        "kind": "song", "isStreamable": True,
        "collectionViewUrl": "https://music.apple.com/us/album/x/1",
    }]}
    assert pick_apple_music_url(payload) == "https://music.apple.com/us/album/x/1"


def test_pick_url_strips_store_tracking_keeps_track_deeplink() -> None:
    """Apple Music, not buy-a-song: drop the iTunes-Store affiliate param (uo)
    but keep the `i=` deep-link to the specific track."""
    payload = {"results": [{
        "kind": "song", "isStreamable": True,
        "trackViewUrl": "https://music.apple.com/us/album/luther/178?i=999&uo=4",
    }]}
    assert pick_apple_music_url(payload) == "https://music.apple.com/us/album/luther/178?i=999"


def test_pick_url_skips_non_streamable_buy_only() -> None:
    """A non-streamable result is buy-only; skip it and take the next streamable."""
    payload = {"results": [
        {"kind": "song", "isStreamable": False,
         "trackViewUrl": "https://music.apple.com/us/song/buyonly/1?i=1"},
        {"kind": "song", "isStreamable": True,
         "trackViewUrl": "https://music.apple.com/us/song/stream/2?i=2"},
    ]}
    assert pick_apple_music_url(payload) == "https://music.apple.com/us/song/stream/2?i=2"


def test_pick_url_rejects_non_apple_and_empty() -> None:
    # Non-apple URL (e.g. a podcast/app result) is not accepted.
    assert pick_apple_music_url(
        {"results": [{"kind": "song", "trackViewUrl": "https://example.com/x"}]}
    ) is None
    # Genuine miss -> None (never hallucinates).
    assert pick_apple_music_url({"resultCount": 0, "results": []}) is None
    # Malformed payloads -> None, not an exception.
    assert pick_apple_music_url({}) is None
    assert pick_apple_music_url({"results": "nope"}) is None
    assert pick_apple_music_url({"results": [None, 5]}) is None


# ---- resolver (network-shaped, fail-open) ----------------------------------


@pytest.mark.asyncio
async def test_resolve_empty_query_skips_network() -> None:
    assert await resolve_apple_music_url("   ") is None


def _fake_session(payload: dict[str, Any]) -> Any:
    class _Resp:
        def raise_for_status(self) -> None: ...
        async def json(self, content_type: Any = None) -> dict[str, Any]:
            return payload
        async def __aenter__(self) -> Any:
            return self
        async def __aexit__(self, *a: Any) -> None: ...

    class _Session:
        def get(self, *a: Any, **k: Any) -> Any:
            return _Resp()
        async def close(self) -> None: ...

    return _Session()


@pytest.mark.asyncio
async def test_resolve_parses_live_shape() -> None:
    payload = {"results": [{
        "kind": "song", "isStreamable": True,
        "trackViewUrl": "https://music.apple.com/us/song/father/1888707289?i=1&uo=4",
    }]}
    with patch(
        "utils.apple_music.aiohttp.ClientSession",
        return_value=_fake_session(payload),
    ):
        url = await resolve_apple_music_url("Travis Scott - FATHER")
    # uo stripped, i kept.
    assert url == "https://music.apple.com/us/song/father/1888707289?i=1"


# ---- post-enrichment provider registry -------------------------------------


@pytest.mark.asyncio
async def test_resolve_music_url_returns_first_provider_hit() -> None:
    hit = AsyncMock(return_value="https://music.apple.com/us/song/x/1")
    miss = AsyncMock(return_value=None)
    # Order matters: a later provider isn't tried once an earlier one hits.
    with patch("utils.music_links._PROVIDERS", [("apple_music", hit), ("spotify", miss)]):
        assert await resolve_music_url("tems jeje") == "https://music.apple.com/us/song/x/1"
    miss.assert_not_awaited()


@pytest.mark.asyncio
async def test_resolve_music_url_falls_through_to_next_provider() -> None:
    miss = AsyncMock(return_value=None)
    hit = AsyncMock(return_value="https://open.spotify.com/track/x")
    with patch("utils.music_links._PROVIDERS", [("apple_music", miss), ("spotify", hit)]):
        assert await resolve_music_url("x") == "https://open.spotify.com/track/x"


@pytest.mark.asyncio
async def test_resolve_music_url_empty_and_all_miss() -> None:
    assert await resolve_music_url("   ") is None
    miss = AsyncMock(return_value=None)
    with patch("utils.music_links._PROVIDERS", [("apple_music", miss)]):
        assert await resolve_music_url("x") is None


@pytest.mark.asyncio
async def test_resolve_music_url_provider_error_is_swallowed() -> None:
    boom = AsyncMock(side_effect=RuntimeError("provider down"))
    hit = AsyncMock(return_value="https://music.apple.com/us/song/x/1")
    with patch("utils.music_links._PROVIDERS", [("apple_music", boom), ("spotify", hit)]):
        assert await resolve_music_url("x") == "https://music.apple.com/us/song/x/1"


# ---- claude_client wiring --------------------------------------------------


def test_extract_track_line() -> None:
    from claude_client import _extract_track_line

    body, query = _extract_track_line(
        "been spinning this all week. travis ate.\nTRACK: Travis Scott - FATHER"
    )
    assert body == "been spinning this all week. travis ate."
    assert query == "Travis Scott - FATHER"

    body2, query2 = _extract_track_line("just a take, no track")
    assert body2 == "just a take, no track"
    assert query2 is None

    body3, query3 = _extract_track_line("take\ntrack:   ")
    assert body3 == "take"
    assert query3 is None


@pytest.mark.asyncio
async def test_music_post_resolves_and_appends_link() -> None:
    """music_post strips the TRACK line, resolves the link via the provider
    registry, and appends it, without the model searching for a link."""
    from claude_client import ClaudeClient, ClaudeResult

    client = ClaudeClient(api_key="test")
    gen = AsyncMock(return_value=ClaudeResult(
        text="been spinning this all week.\nTRACK: Tems - Love Me JeJe",
        stop_reason="end_turn", input_tokens=1, output_tokens=1, web_search_urls=[],
    ))
    with patch.object(client, "_call", gen), \
            patch(
                "claude_client.resolve_music_url",
                AsyncMock(return_value="https://music.apple.com/us/song/love-me-jeje/1"),
            ):
        out = await client.music_post("room activity")

    assert "TRACK:" not in out  # the marker line is stripped
    assert out.endswith("https://music.apple.com/us/song/love-me-jeje/1")
    assert "been spinning this all week." in out


@pytest.mark.asyncio
async def test_music_post_web_search_is_uncapped() -> None:
    """web_search is for track discovery only now (link is resolved in code),
    so no max_uses cap is set."""
    from claude_client import ClaudeClient, ClaudeResult

    client = ClaudeClient(api_key="test")
    gen = AsyncMock(return_value=ClaudeResult(
        text="a take\nTRACK: x - y", stop_reason="end_turn",
        input_tokens=1, output_tokens=1, web_search_urls=[],
    ))
    with patch.object(client, "_call", gen), \
            patch("claude_client.resolve_music_url", AsyncMock(return_value=None)):
        await client.music_post("room activity")
    web = next(t for t in gen.call_args.kwargs["tools"] if t.get("name") == "web_search")
    assert "max_uses" not in web
