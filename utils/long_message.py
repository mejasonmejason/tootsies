"""Ensure responses fit within Discord's 2000-character message limit."""

from __future__ import annotations

import discord

DISCORD_MAX = 2000


def _truncate(text: str, limit: int = DISCORD_MAX) -> str:
    if len(text) <= limit:
        return text
    cut = text.rfind("\n", 0, limit)
    if cut <= 0:
        cut = text.rfind(" ", 0, limit)
    if cut <= 0:
        cut = limit
    return text[:cut].rstrip()


async def send_long(
    text: str,
    *,
    followup: discord.Webhook | None = None,
    reply_to: discord.Message | None = None,
    channel: discord.abc.Messageable | None = None,
) -> None:
    """Send *text*, truncating at a clean line boundary if it exceeds 2000 chars.

    Exactly one of *followup*, *reply_to*, or *channel* must be provided.
    """
    safe = _truncate(text)

    if followup:
        await followup.send(safe)
    elif reply_to:
        await reply_to.reply(safe, mention_author=False)
    elif channel:
        await channel.send(safe)
