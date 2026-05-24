"""Channel-history readers used by /ask, /recap, /discourse."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import discord

from utils.permissions import can_read


def _truncate(text: str, n: int) -> str:
    return text if len(text) <= n else text[: n - 1] + "…"


async def recent_messages(
    channel: discord.TextChannel | discord.Thread,
    me: discord.Member,
    limit: int = 30,
    within: timedelta | None = None,
    include_bots: bool = False,
) -> list[discord.Message]:
    """Pull recent messages, skipping empty content.

    `include_bots` defaults False because for /ask the context is "what are humans
    chatting about?". For /recap we want to summarize EVERYTHING that happened, and
    for /discourse feed-channel reads the bot/webhook posts ARE the content, so the
    caller passes True there.
    """
    if not can_read(channel, me):
        return []
    cutoff = datetime.now(UTC) - within if within else None
    msgs: list[discord.Message] = []
    async for msg in channel.history(limit=limit * 2):
        if msg.author.bot and not include_bots:
            continue
        if not msg.content.strip():
            continue
        if cutoff and msg.created_at < cutoff:
            break
        msgs.append(msg)
        if len(msgs) >= limit:
            break
    msgs.reverse()
    return msgs


def format_for_prompt(messages: list[discord.Message], include_reactions: bool = False) -> str:
    """Render a message list for inclusion in a Claude prompt.

    Uses display names (no IDs) and truncates content. Reactions optionally appended so /recap
    can weight popular messages.
    """
    if not messages:
        return "(no recent messages)"
    lines: list[str] = []
    for m in messages:
        name = m.author.display_name
        body = _truncate(m.content.replace("\n", " "), 200)
        line = f"{name}: {body}"
        if include_reactions and m.reactions:
            counts = sum(r.count for r in m.reactions)
            if counts:
                line += f"  [{counts} reactions]"
        lines.append(line)
    return "\n".join(lines)


def is_channel_dead(messages: list[discord.Message], min_messages: int = 3) -> bool:
    """Heuristic for /recap deflection."""
    real = [m for m in messages if len(m.content) > 5]
    return len(real) < min_messages
