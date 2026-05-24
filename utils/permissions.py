"""Permission helpers — mod role checks + channel-permission introspection."""

from __future__ import annotations

import discord

from db import DB


async def is_mod(db: DB, member: discord.Member) -> bool:
    """True if member has any configured mod role, OR is the guild owner, OR has manage_guild."""
    # Owners and manage_guild always pass — safety net for un-configured guilds.
    if member.guild.owner_id == member.id:
        return True
    if member.guild_permissions.manage_guild:
        return True
    mod_role_ids = set(await db.get_mod_roles(member.guild.id))
    if not mod_role_ids:
        return False
    return any(r.id in mod_role_ids for r in member.roles)


def can_send_in(
    channel: discord.abc.GuildChannel | discord.Thread, me: discord.Member
) -> bool:
    """Check whether the bot can post in a given text channel or thread."""
    if not isinstance(channel, discord.TextChannel | discord.Thread):
        return False
    perms = channel.permissions_for(me)
    return perms.send_messages and perms.view_channel


def can_read(
    channel: discord.abc.GuildChannel | discord.Thread, me: discord.Member
) -> bool:
    if not isinstance(channel, discord.TextChannel | discord.Thread):
        return False
    perms = channel.permissions_for(me)
    return perms.view_channel and perms.read_message_history
