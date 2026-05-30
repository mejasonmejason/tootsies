"""Tests for the music-lounge feature."""

from cogs.music import _NOTHING_HITTING, _has_music_link, _resolve_linkless


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


# ---- links-only invariant: never post a linkless message --------------------


def test_resolve_linkless_scheduled_skips_slot():
    # Scheduled post with no link after retry -> "" so the slot is skipped.
    assert _resolve_linkless("take, no link", "still no link", must_post=False) == ""


def test_resolve_linkless_manual_never_posts_a_linkless_take():
    # Manual /music must NOT return the linkless take; it returns the voiced
    # non-answer instead. This is the invariant for the links-only channel.
    linkless_take = "this track slaps fr"  # no music.apple.com / spotify link
    out = _resolve_linkless(linkless_take, linkless_take, must_post=True)
    assert out == _NOTHING_HITTING
    assert not _has_music_link(out)


def test_resolve_linkless_manual_handles_none_lines():
    # Both attempts empty/None -> still the voiced non-answer, never None/blank.
    assert _resolve_linkless(None, None, must_post=True) == _NOTHING_HITTING
