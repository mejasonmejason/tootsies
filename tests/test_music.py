"""Tests for the music-lounge feature: voice fallbacks + dedup."""

from utils.voice import MUSIC_FALLBACK, pick


def test_music_fallback_quips():
    assert len(MUSIC_FALLBACK) >= 3
    quip = pick(MUSIC_FALLBACK)
    assert isinstance(quip, str)
    assert len(quip) > 10


def test_music_fallback_variety():
    seen: set[str] = set()
    for _ in range(50):
        seen.add(pick(MUSIC_FALLBACK))
    assert len(seen) >= 2
