"""Tests for the music-lounge feature: apple_music charts + voice fallbacks."""

from utils.apple_music import ChartEntry, format_charts_for_prompt
from utils.voice import MUSIC_FALLBACK, pick


def test_format_charts_empty():
    assert format_charts_for_prompt({}) == ""


def test_format_charts_basic():
    charts = {
        "hiphop": [
            ChartEntry(rank=1, name="Not Like Us", artist="Kendrick Lamar", url="https://music.apple.com/us/song/1", album="GNX"),
            ChartEntry(rank=2, name="Luther", artist="Kendrick Lamar", url="https://music.apple.com/us/song/2", album="GNX"),
        ],
    }
    result = format_charts_for_prompt(charts, limit=5)
    assert "APPLE MUSIC CHARTS" in result
    assert "HIPHOP TOP 2" in result
    assert "Not Like Us" in result
    assert "Kendrick Lamar" in result
    assert "https://music.apple.com/us/song/1" in result


def test_format_charts_limit():
    entries = [
        ChartEntry(rank=i, name=f"Song {i}", artist=f"Artist {i}", url=f"https://music.apple.com/{i}")
        for i in range(1, 20)
    ]
    result = format_charts_for_prompt({"pop": entries}, limit=3)
    assert "Song 3" in result
    assert "Song 4" not in result


def test_music_fallback_quips():
    assert len(MUSIC_FALLBACK) >= 3
    quip = pick(MUSIC_FALLBACK)
    assert isinstance(quip, str)
    assert len(quip) > 10
