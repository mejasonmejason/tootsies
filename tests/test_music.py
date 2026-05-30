"""Tests for the music-lounge feature."""

from cogs.music import _has_music_link
from utils.perplexity import _CATEGORY_QUERIES, build_search_query


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


# ---- Fresh-pick (first drop of the day favors recent releases) ---------------

def test_new_releases_search_category_exists():
    assert "new-releases" in _CATEGORY_QUERIES


def test_new_releases_search_query_targets_recent_drops():
    query = build_search_query(
        "", surface="discourse", category="new-releases", channel_name="music-lounge",
    )
    lowered = query.lower()
    assert "new" in lowered
    # Targets recent drops, any day, not gated to a specific weekday.
    assert "last few days" in lowered
    assert "friday" not in lowered
    # Should be the dedicated new-releases query, not the channel-name fallback.
    assert "music-lounge" not in lowered
