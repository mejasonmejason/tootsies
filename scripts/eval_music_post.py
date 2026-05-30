"""Dry-run eval: what does a music drop actually look like end to end?

Runs the real `music_post` (Sonnet + web_search for discovery) across a few
room scenarios, then shows the rendered post AND resolves the named track to an
Apple Music URL the same way production does (utils.music_links). Lets a human
eyeball: is the take in-voice and short? did it name a real track? did the link
resolve to a streamable Apple Music page (not a buy/Store link)?

Not a pytest test (makes live API + iTunes calls). Run:
  python scripts/eval_music_post.py
"""
from __future__ import annotations

import asyncio

from claude_client import ClaudeClient, _extract_track_line
from utils.music_links import resolve_music_url

# Each scenario is a room state music_post would see (the sources_blob), plus a
# genre_hint like the scheduler rotates. recent_posts seeds the dedup clause.
SCENARIOS: list[dict[str, str]] = [
    {
        "name": "quiet channel, afrobeats hint",
        "blob": "(quiet channel)",
        "genre": "afrobeats",
        "recent": "",
    },
    {
        "name": "room talking Kendrick, no genre hint",
        "blob": (
            "#music:\n"
            "[12m ago] deshawn: gnx still in heavy rotation fr\n"
            "[9m ago] mara: luther with sza is the one though\n"
            "[6m ago] kel: nah tv off is the hardest beat switch this year\n"
        ),
        "genre": "",
        "recent": "",
    },
    {
        "name": "r&b hint, dedup pushes off SZA",
        "blob": (
            "#music:\n"
            "[20m ago] jules: need slow jams for tonight's shift\n"
            "[15m ago] sam: something neo-soul, not the usual\n"
        ),
        "genre": "rnb",
        "recent": "- SZA - Snooze\n- Kendrick Lamar - luther",
    },
]


async def main() -> None:
    client = ClaudeClient()
    for i, sc in enumerate(SCENARIOS, 1):
        print(f"\n{'=' * 70}\n[{i}] {sc['name']}  (genre_hint={sc['genre'] or 'none'})\n{'=' * 70}")
        raw = await client.music_post(
            sources_blob=sc["blob"],
            recent_posts=sc["recent"],
            channel_name="music",
            genre_hint=sc["genre"],
        )
        # music_post already strips TRACK + appends the resolved URL; show that
        # rendered post, then separately re-derive the track + re-resolve so we
        # can display what was named vs. what resolved.
        print("\n--- rendered post (what the room sees) ---")
        print(raw or "(empty)")

        body, track = _extract_track_line(
            raw if "TRACK:" in raw else f"{raw}\nTRACK: "
        )
        # If music_post worked, raw has no TRACK line; recover the named track
        # by asking the resolver what the trailing URL points to is moot — so
        # instead just report whether a link landed.
        last_line = raw.strip().splitlines()[-1] if raw.strip() else ""
        has_link = "music.apple.com" in last_line
        print("\n--- diagnostics ---")
        print(f"link resolved + appended: {has_link}")
        if has_link:
            print(f"link: {last_line.strip()}")
        else:
            print("NO LINK — would hit the links-only retry/skip path")


if __name__ == "__main__":
    asyncio.run(main())
