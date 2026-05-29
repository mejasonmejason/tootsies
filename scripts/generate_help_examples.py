"""Run each user-facing prompt against the live Claude API with fabricated but
plausible inputs, print the outputs in Discord-ready markdown.

Use case: generate the "what she sounds like" block for the mod intro message
so the examples are real Toots outputs, not handwritten approximations.

Usage:
    export ANTHROPIC_API_KEY=sk-ant-...
    python scripts/generate_help_examples.py

Cost: ~$0.15 total per run (4 Sonnet calls + 4 Haiku calls + web search on a
couple, all small contexts).

The fabricated inputs are chosen to exercise the prompts realistically:
  - /ask: a factual question (web search) + an opinion question (no search)
  - /recap: a believable channel buffer (mix of takes, reactions, links)
  - /discourse: a feed-like sources_blob
  - chime-in: a buffer where the room is debating something + a vibe Toots
    should engage with
  - scheduled discourse: no category, reads the room
  - deflect: an example situation

Outputs print as markdown so you can copy/paste straight into Discord.
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

# Make repo importable when run from anywhere.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# We need the env stubs the bot expects, but only ANTHROPIC_API_KEY needs to be real.
for k, v in {
    "DISCORD_TOKEN": "x",
    "GITHUB_TOKEN": "x",
    "GITHUB_REPO": "x/x",
    "DATABASE_URL": "postgres://x:x@x/x",
}.items():
    os.environ.setdefault(k, v)

if not os.environ.get("ANTHROPIC_API_KEY"):
    print("ERROR: set ANTHROPIC_API_KEY in your environment first.", file=sys.stderr)
    sys.exit(1)

from claude_client import ClaudeClient  # noqa: E402

# ---- fabricated inputs ----------------------------------------------------

ASK_QUESTIONS = [
    "who are you",  # identity probe, exercises the RESTATE rule's short-question exception
    "is drake done",
    "best pizza in miami",
]

# A plausible #general channel buffer, oldest first. Mix of takes, reactions,
# a link. Names match the persona's regulars examples for tone parity.
RECAP_BUFFER = """\
[14:02] gaza: yo did y'all see the penguin face reveal
[14:02] gaza: https://www.youtube.com/watch?v=fakeexample
[14:03] flash: 47 minutes is insane for a face reveal
[14:03] flash: nobody asked for the director's cut
[14:04] martini: nah this was actually fire, the lead up was the whole point
[14:05] gaza: martini's right, the production was crazy
[14:06] desi: ...he kinda looks like my cousin lol
[14:06] uhlant: WHAT
[14:07] flash: he looks like he sounds tbh
[14:08] martini: y'all are not appreciating the craft
[14:09] gaza: the moment when he hit the camera was unreal
[14:10] desi: i was expecting more of a chris-evans type but
[14:10] desi: this is fine i guess
[14:11] uhlant: 5 years of mystery for this
[14:12] flash: 5 years for a guy who looks like he eats cereal for dinner
[14:13] gaza: lmaoo
"""

# A fake sources_blob for /discourse, formatted the way feeds.format_for_prompt
# emits real ones. Mix of NBA + music tweets so Sonnet can pick the more
# talk-worthy thread.
DISCOURSE_SOURCES = """\
[#nba, 14 min ago, @ShamsCharania, 412 reactions]
Joel Embiid is OUT for tonight's game vs the Knicks (left knee management).
Sixers now 0-7 in games Embiid has missed this season.

[#hiphop, 38 min ago, @complexmusic, 287 reactions]
Kendrick Lamar's 'GNX' has officially passed 'good kid, m.A.A.d city' in
total streams (Spotify). Took 6 months.

[#nba, 1h ago, @TheSteinLine, 156 reactions]
The Lakers are quietly shopping D'Angelo Russell ahead of the deadline,
per sources. No traction yet.
"""

# Plausible chime-in buffer: room is debating kendrick vs drake legacy.
CHIMEIN_BUFFER = """\
[15:42] flash: i still think gnx is overrated
[15:43] flash: like meet the grahams was the moment, not the album
[15:44] gaza: brodie the production carried gnx
[15:44] gaza: tv off, peekaboo, hey now, those beats are insane
[15:45] martini: gaza's cooking
[15:45] martini: the production is half the conversation, the writing is the other half
[15:46] flash: the writing is fine
[15:46] flash: drake was just outwritten that summer
[15:47] gaza: outwritten is wild
[15:47] gaza: not like beat for me was a fair fight
"""

DEFLECT_SITUATION = (
    "user hit their /ask daily cap of 20 and is asking another question. "
    "deflect them in voice."
)


# ---- driver ---------------------------------------------------------------


async def main() -> None:
    client = ClaudeClient()

    print("# Toots example outputs (live from the prompts in `claude_client.py`)\n")
    print(
        "Generated with `scripts/generate_help_examples.py`. Fabricated inputs, "
        "real prompts + real model calls. Copy any section into Discord as-is.\n"
    )

    # /ask
    print("---\n")
    print("## /ask\n")
    for q in ASK_QUESTIONS:
        print(f"**`/ask {q}`**")
        result = await client.ask(question=q, use_web=True)
        print(f"> {result.strip()}\n")

    # /recap
    print("---\n")
    print("## /recap\n")
    print("Input: ~15 messages of the room reacting to a youtube face reveal.\n")
    print("**`/recap period: last hour`**")
    recap = await client.recap(channel_name="general", messages_blob=RECAP_BUFFER)
    print(f"> {recap.strip()}\n")

    # /discourse
    print("---\n")
    print("## /discourse\n")
    print(
        "Input: 3 feed posts (NBA injury, hip-hop streaming milestone, NBA trade rumor).\n"
    )
    print("**`/discourse category: nba`**")
    discourse_nba = await client.discourse(
        category="nba", sources_blob=DISCOURSE_SOURCES, must_post=True,
    )
    print(f"> {discourse_nba.strip()}\n")

    print("**`/discourse category: hiphop`**")
    discourse_hh = await client.discourse(
        category="hiphop", sources_blob=DISCOURSE_SOURCES, must_post=True,
    )
    print(f"> {discourse_hh.strip()}\n")

    # scheduled discourse (no category, reads the room)
    print("---\n")
    print("## scheduled discourse post (mood: chill or yaps)\n")
    print("No category, reads the room and picks what's fresh.\n")
    print("**(scheduled tick fires)**")
    scheduled = await client.discourse(
        category=None, sources_blob=DISCOURSE_SOURCES, must_post=False,
    )
    print(f"> {scheduled.strip() or '_(EMPTY, slot skipped)_'}\n")

    # chime-in (score + post)
    print("---\n")
    print("## chime-in\n")
    print(
        "Input: ~10-message buffer where the room is debating kendrick vs drake "
        "production / writing.\n"
    )
    print("**(chime-in tick fires, scoring first)**")
    score, vibe, hook, reaction, target = await client.chimein_score(CHIMEIN_BUFFER)
    print(
        f"> _scorer:_ `score={score:.2f}, vibe={vibe}, hook={hook!r}, "
        f"reaction={reaction!r}, target={target}`\n"
    )
    if score >= 0.6:
        line = await client.chimein_post(CHIMEIN_BUFFER, hook=hook)
        print(f"> {line.strip()}\n")
    else:
        print("> _(scored below the lowest threshold (0.6 yaps), skipped)_\n")

    # deflect
    print("---\n")
    print("## deflect (cap hit, error fallback, etc.)\n")
    print(f"Situation passed: _{DEFLECT_SITUATION}_\n")
    deflection = await client.deflect(DEFLECT_SITUATION)
    print(f"> {deflection.strip()}\n")

    print("---\n")
    print("Done. Pick the lines you like; not every one will land on first roll.")


if __name__ == "__main__":
    asyncio.run(main())
