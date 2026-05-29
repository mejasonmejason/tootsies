"""Permission helpers, mod role checks + channel-permission introspection."""

from __future__ import annotations

import discord

from db import DB


async def is_mod(db: DB, member: discord.Member) -> bool:
    """True if member has any configured mod role, OR is the guild owner, OR has manage_guild."""
    # Owners and manage_guild always pass, safety net for un-configured guilds.
    if member.guild.owner_id == member.id:
        return True
    if member.guild_permissions.manage_guild:
        return True
    mod_role_ids = set(await db.get_mod_roles(member.guild.id))
    if not mod_role_ids:
        return False
    return any(r.id in mod_role_ids for r in member.roles)


def member_has_role(member: discord.Member, role_ids: set[int]) -> bool:
    """True if the member wears any of the given role ids. Used for the
    "girls" role check (whether Toots treats this patron as one of her girls)."""
    if not role_ids:
        return False
    return any(r.id in role_ids for r in member.roles)


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


def can_react(
    channel: discord.abc.GuildChannel | discord.Thread, me: discord.Member
) -> bool:
    """Check whether the bot can add a reaction to a message in this channel.

    Needs add_reactions plus the read perms: you can't react to a message you
    can't see, and Discord requires read_message_history to react to anything
    that isn't brand-new in cache.
    """
    if not isinstance(channel, discord.TextChannel | discord.Thread):
        return False
    perms = channel.permissions_for(me)
    return perms.view_channel and perms.read_message_history and perms.add_reactions
