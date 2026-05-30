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
import os
from unittest.mock import patch

import claude_client
from claude_client import ClaudeClient
from utils.music_links import resolve_music_url
from utils.perplexity import PerplexityClient, build_search_query

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
    # Wire Perplexity exactly like cogs/music.py does, so the eval sees the real
    # production input (current music news/trends shaping the pick). Skips
    # cleanly if PERPLEXITY_API_KEY isn't set.
    pplx_key = os.getenv("PERPLEXITY_API_KEY")
    pplx = PerplexityClient(pplx_key) if pplx_key else None
    print(f"Perplexity: {'ON' if pplx else 'OFF (no PERPLEXITY_API_KEY)'}")

    try:
        for i, sc in enumerate(SCENARIOS, 1):
            print(f"\n{'=' * 70}\n[{i}] {sc['name']}  (genre_hint={sc['genre'] or 'none'})\n{'=' * 70}")
            pplx_context = None
            if pplx:
                pplx_context = await pplx.search(
                    build_search_query(
                        "", surface="discourse",
                        category=sc["genre"], channel_name="music",
                    ),
                    purpose="music",
                )
                print(f"\n--- perplexity context ({len(pplx_context or '')} chars) ---")
                print((pplx_context or "(none)")[:600])
            # Spy on the resolver so we can tell apart the two failure modes:
            # the model emitted no TRACK line (resolver never called) vs. it
            # named a track iTunes couldn't resolve (called, returned None).
            seen: list[str] = []

            async def _spy(query: str, _seen: list[str] = seen) -> str | None:
                _seen.append(query)
                return await resolve_music_url(query)

            with patch.object(claude_client, "resolve_music_url", _spy):
                raw = await client.music_post(
                    sources_blob=sc["blob"],
                    recent_posts=sc["recent"],
                    channel_name="music",
                    genre_hint=sc["genre"],
                    perplexity_context=pplx_context,
                )
            # music_post already strips the TRACK line + appends the resolved
            # URL, so `raw` is the rendered post. Report whether a link landed.
            print("\n--- rendered post (what the room sees) ---")
            print(raw or "(empty)")

            last_line = raw.strip().splitlines()[-1] if raw.strip() else ""
            has_link = "music.apple.com" in last_line
            print("\n--- diagnostics ---")
            print(f"TRACK line emitted: {bool(seen)}" + (f"  -> queried: {seen[0]!r}" if seen else ""))
            print(f"link resolved + appended: {has_link}")
            if has_link:
                print(f"link: {last_line.strip()}")
            elif seen:
                print("MISS: model named a track but iTunes didn't resolve it")
            else:
                print("MISS: model emitted NO TRACK line (prompt-adherence failure)")
    finally:
        if pplx:
            await pplx.close()


if __name__ == "__main__":
    asyncio.run(main())
