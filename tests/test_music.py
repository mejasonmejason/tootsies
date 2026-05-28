"""Tests for the music-lounge feature."""

from cogs.music import _has_music_link


def test_has_music_link_apple():
    assert _has_music_link("great track\nhttps://music.apple.com/us/album/foo/123")


def test_has_music_link_spotify():
    assert _has_music_link("great track\nhttps://open.spotify.com/track/abc")


def test_has_music_link_spotify_short():
    assert _has_music_link("great track\nhttps://spotify.link/abc")


def test_has_music_link_missing():
    assert not _has_music_link("great track but no link")


def test_has_music_link_other_url():
    assert not _has_music_link("check this out\nhttps://youtube.com/watch?v=abc")
