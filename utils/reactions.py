"""Toots adding reactions to messages.

A reaction is a lighter-touch engagement than a chime-in post: "I clocked that"
without dropping a paragraph into the room. This is the write-side counterpart to
the reactor-reading in utils/feeds.py.

Best-effort by design: a reaction is never load-bearing, so permission gaps and
Discord API hiccups are swallowed (logged + skipped), never raised into the
caller's tick.
"""

from __future__ import annotations

import logging

import discord

from utils.events import emit
from utils.permissions import can_react

log = logging.getLogger(__name__)


async def react(
    message: discord.Message,
    emoji: str,
    *,
    source: str,
) -> bool:
    """Add a single reaction to a message. Returns True only if it landed.

    Guards:
      - guild context + add_reactions/read perms (via can_react)
      - idempotent: skips if Toots already reacted to this message with `emoji`,
        so repeated chimein ticks on the same buffered message don't double-react.

    Emits a `reaction_added` event on success.
    """
    guild = message.guild
    channel = message.channel
    # Narrow off DMs/group/partial channels: reactions only fire in guild text
    # channels and threads, which is also all can_react knows how to check.
    if guild is None or not isinstance(channel, discord.TextChannel | discord.Thread):
        return False
    me = guild.me
    if me is None or not can_react(channel, me):
        return False

    # Already reacted with this emoji? `r.me` is True when the bot is among the
    # reactors. Avoids stacking the same emoji across ticks.
    for r in message.reactions:
        if r.me and str(r.emoji) == emoji:
            return False

    try:
        await message.add_reaction(emoji)
    except discord.DiscordException:
        log.exception("react failed: source=%s message=%s", source, message.id)
        return False

    emit(
        "reaction_added",
        source=source,
        guild_id=guild.id,
        channel_id=channel.id,
        message_id=message.id,
        emoji=emoji,
    )
    return True
