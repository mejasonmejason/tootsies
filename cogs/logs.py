"""/logs, mod-only command to set the channel where toots posts order
status updates, deploy notifications, and error notices."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, cast

import discord
from discord import app_commands
from discord.ext import commands

from utils import voice
from utils.metrics import track_command
from utils.permissions import is_mod

if TYPE_CHECKING:
    from bot import TootsiesBot

log = logging.getLogger(__name__)


class Logs(commands.Cog):
    def __init__(self, bot: TootsiesBot) -> None:
        self.bot = bot

    @app_commands.command(
        name="logs",
        description="set the channel where i post order status + errors (mods only).",
    )
    @app_commands.describe(channel="text channel for log posts")
    @track_command("logs")
    async def logs(
        self,
        interaction: discord.Interaction,
        channel: discord.TextChannel,
    ) -> None:
        member = interaction.user
        guild = interaction.guild
        if guild is None or not isinstance(member, discord.Member):
            await interaction.response.send_message(
                voice.pick(voice.PERMISSION_DENIED), ephemeral=True,
            )
            return
        if not (
            member.guild.owner_id == member.id
            or member.guild_permissions.manage_guild
            or await is_mod(self.bot.db, member)
        ):
            await interaction.response.send_message(
                voice.pick(voice.PERMISSION_DENIED), ephemeral=True,
            )
            return

        await self.bot.db.set_setting(
            guild.id, "bot_logs_channel", channel.id, member.id,
        )
        await self.bot.db.audit(
            guild.id, member.id, "logs_channel_set",
            after={"channel_id": channel.id},
        )
        await interaction.response.send_message(
            f"logs going to <#{channel.id}> from here on.", ephemeral=True,
        )


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Logs(cast("TootsiesBot", bot)))
