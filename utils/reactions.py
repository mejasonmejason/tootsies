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
    emoji: str | discord.Emoji | discord.PartialEmoji,
    *,
    source: str,
) -> bool:
    """Add a single reaction to a message. Returns True only if it landed.

    `emoji` may be a unicode string or a custom-emoji object (e.g. when piling
    onto a reaction the room already used, which surfaces as an Emoji /
    PartialEmoji rather than a plain string).

    Guards:
      - guild context + add_reactions/read perms (via can_react)
      - one reaction per message: skips if Toots has already reacted to this
        message with ANY emoji, so she never stacks a second reaction on the
        same message across ticks.

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

    # Already reacted to this message at all? `r.me` is True when the bot is
    # among the reactors. One reaction per message, regardless of emoji.
    if any(r.me for r in message.reactions):
        return False

    emoji_key = str(emoji)

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
        emoji=emoji_key,
    )
    return True
