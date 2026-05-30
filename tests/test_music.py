"""Tests for the music-lounge feature."""

from datetime import datetime

from cogs.music import _has_music_link, _is_release_day
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


# ---- New Music Friday release-day pick ---------------------------------------

def test_is_release_day_friday():
    # 2026-05-29 is a Friday (New Music Friday).
    assert _is_release_day(datetime(2026, 5, 29, 11, 0))


def test_is_release_day_not_friday():
    # Saturday, Sunday, Monday are not release day.
    assert not _is_release_day(datetime(2026, 5, 30, 11, 0))  # Sat
    assert not _is_release_day(datetime(2026, 5, 31, 11, 0))  # Sun
    assert not _is_release_day(datetime(2026, 6, 1, 11, 0))   # Mon


def test_new_releases_search_category_exists():
    assert "new-releases" in _CATEGORY_QUERIES


def test_new_releases_search_query_targets_this_week_drops():
    query = build_search_query(
        "", surface="discourse", category="new-releases", channel_name="music-lounge",
    )
    lowered = query.lower()
    assert "new" in lowered
    assert "this week" in lowered
    # Should be the dedicated NMF query, not the generic channel-name fallback.
    assert "music-lounge" not in lowered
