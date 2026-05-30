"""Tests for the music-lounge feature."""

import inspect

from cogs.music import _has_music_link


def test_has_music_link_apple():
    assert _has_music_link("great track\nhttps://music.apple.com/us/album/foo/123")


def test_has_music_link_spotify():
    assert _has_music_link("vibes\nhttps://open.spotify.com/track/abc")


def test_has_music_link_spotify_short():
    assert _has_music_link("vibes https://spotify.link/xyz")


def test_has_music_link_missing():
    assert not _has_music_link("no link here, just vibes")


def test_has_music_link_other_url():
    assert not _has_music_link("check https://youtube.com/watch?v=123")


def test_music_post_prompt_has_freshness_weight():
    """The music prompt should carry the 2:1 freshness thumb-on-the-scale so a
    must-listen recent drop leans over back-catalog, without a separate routing
    fork. It's a weighting inside the pick logic, not a per-call branch.
    """
    from claude_client import ClaudeClient

    src = inspect.getsource(ClaudeClient.music_post)
    assert "FRESHNESS WEIGHT" in src
    assert "2:1" in src
