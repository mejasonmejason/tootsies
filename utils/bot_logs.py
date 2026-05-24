"""Bot-logs channel writer + verbosity gating."""

from __future__ import annotations

import logging

import discord

from db import DB
from utils.permissions import can_send_in

log = logging.getLogger(__name__)

# Verbosity ladder, events are emitted with a level; the server's BOT_LOGS_VERBOSITY decides
# whether we actually post.
LEVELS = {"errors": 0, "milestones": 1, "full": 2}


async def post(
    bot: discord.Client,
    db: DB,
    guild_id: int,
    message: str,
    *,
    level: str = "milestones",
    verbosity: str = "milestones",
) -> None:
    """Best-effort post into the configured #bot-logs channel."""
    if LEVELS.get(level, 1) > LEVELS.get(verbosity, 1):
        return
    channel_id = await db.get_setting(guild_id, "bot_logs_channel")
    if not channel_id:
        return
    guild = bot.get_guild(guild_id)
    if guild is None:
        return
    channel = guild.get_channel(int(channel_id))
    if channel is None or not isinstance(channel, discord.TextChannel):
        return
    me = guild.me
    if me is None or not can_send_in(channel, me):
        return
    try:
        await channel.send(message)
    except discord.DiscordException as exc:
        log.warning("bot_logs post failed: %s", exc)
