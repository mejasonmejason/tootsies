"""Apple Music link resolution (utils.apple_music) + the music_post TRACK split.

The music channel is links-only. Instead of the model web_searching for an
exact music.apple.com URL (the 25-73s spiral), it names the track and we resolve
the link deterministically via the iTunes Search API. These tests cover the pure
response parser, the resolver's fail-open behavior, and claude_client's
_extract_track_line.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from utils.apple_music import pick_apple_music_url, resolve_apple_music_url


def test_pick_url_prefers_track_view() -> None:
    payload = {"results": [{
        "trackViewUrl": "https://music.apple.com/us/song/x/1?i=2",
        "collectionViewUrl": "https://music.apple.com/us/album/x/1",
    }]}
    assert pick_apple_music_url(payload) == "https://music.apple.com/us/song/x/1?i=2"


def test_pick_url_falls_back_to_collection() -> None:
    payload = {"results": [{"collectionViewUrl": "https://music.apple.com/us/album/x/1"}]}
    assert pick_apple_music_url(payload) == "https://music.apple.com/us/album/x/1"


def test_pick_url_rejects_non_apple_and_empty() -> None:
    # A non-apple URL (e.g. a podcast/app result) is not accepted.
    assert pick_apple_music_url({"results": [{"trackViewUrl": "https://example.com/x"}]}) is None
    # Genuine miss -> None (never hallucinates).
    assert pick_apple_music_url({"resultCount": 0, "results": []}) is None
    # Malformed payloads -> None, not an exception.
    assert pick_apple_music_url({}) is None
    assert pick_apple_music_url({"results": "nope"}) is None
    assert pick_apple_music_url({"results": [None, 5]}) is None


@pytest.mark.asyncio
async def test_resolve_empty_query_skips_network() -> None:
    # No query -> None without touching the network.
    assert await resolve_apple_music_url("   ") is None


@pytest.mark.asyncio
async def test_resolve_parses_live_shape() -> None:
    """resolve_apple_music_url returns the parsed URL given an iTunes-shaped
    response. We patch the session so no real HTTP happens."""
    payload = {"results": [{
        "trackViewUrl": "https://music.apple.com/us/song/father/1888707289?i=1",
    }]}

    class _Resp:
        def raise_for_status(self) -> None: ...
        async def json(self, content_type: Any = None) -> dict[str, Any]:
            return payload
        async def __aenter__(self) -> _Resp:
            return self
        async def __aexit__(self, *a: Any) -> None: ...

    class _Session:
        def get(self, *a: Any, **k: Any) -> _Resp:
            return _Resp()
        async def close(self) -> None: ...

    with patch("utils.apple_music.aiohttp.ClientSession", return_value=_Session()):
        url = await resolve_apple_music_url("Travis Scott - FATHER")
    assert url == "https://music.apple.com/us/song/father/1888707289?i=1"


def test_extract_track_line() -> None:
    from claude_client import _extract_track_line

    # Pulls the trailing TRACK line out and returns the query separately.
    body, query = _extract_track_line(
        "been spinning this all week. travis ate.\nTRACK: Travis Scott - FATHER"
    )
    assert body == "been spinning this all week. travis ate."
    assert query == "Travis Scott - FATHER"

    # No TRACK line: body unchanged, query None.
    body2, query2 = _extract_track_line("just a take, no track")
    assert body2 == "just a take, no track"
    assert query2 is None

    # Case-insensitive marker; empty query -> None.
    body3, query3 = _extract_track_line("take\ntrack:   ")
    assert body3 == "take"
    assert query3 is None


@pytest.mark.asyncio
async def test_music_post_resolves_and_appends_link() -> None:
    """music_post strips the TRACK line, resolves the Apple Music URL in code,
    and appends it, without the model searching for a link."""
    from claude_client import ClaudeClient, ClaudeResult

    client = ClaudeClient(api_key="test")
    gen = AsyncMock(return_value=ClaudeResult(
        text="been spinning this all week.\nTRACK: Tems - Love Me JeJe",
        stop_reason="end_turn", input_tokens=1, output_tokens=1, web_search_urls=[],
    ))
    with patch.object(client, "_call", gen), \
            patch(
                "claude_client.resolve_apple_music_url",
                AsyncMock(return_value="https://music.apple.com/us/song/love-me-jeje/1"),
            ):
        out = await client.music_post("room activity")

    assert "TRACK:" not in out  # the marker line is stripped
    assert out.endswith("https://music.apple.com/us/song/love-me-jeje/1")
    assert "been spinning this all week." in out
