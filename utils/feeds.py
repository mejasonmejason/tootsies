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


def is_channel_dead(messages: list[discord.Message]) -> bool:
    """Hard floor only: deflect when the channel is *literally* empty over the period.

    Previously this required 3+ messages of >5 chars each, which filtered out reactions,
    short replies, link drops, and one-word chat. In a real channel that's everyone.
    The Claude /recap prompt already instructs Toots to quip when the content is thin,
    so we trust her judgment for anything non-zero.
    """
    return not messages


def channel_dead_diagnostic(
    channel: discord.TextChannel | discord.Thread,
    me: discord.Member,
    messages: list[discord.Message],
) -> dict[str, object]:
    """Why did the channel look dead? Returns structured fields for events + bot-logs.

    Always called only when `is_channel_dead(messages)` is True, so we know
    `messages` is empty. The interesting question is whether that's because the
    period was empty or because the bot couldn't read.
    """
    perms = channel.permissions_for(me) if isinstance(
        channel, discord.TextChannel | discord.Thread
    ) else None
    return {
        "channel_id": channel.id,
        "channel_name": channel.name,
        "can_view": bool(perms and perms.view_channel),
        "can_read_history": bool(perms and perms.read_message_history),
        "total_messages": len(messages),
        "reason": (
            "no_permission" if perms and not perms.read_message_history
            else "no_messages"
        ),
    }
