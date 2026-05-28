"""/logs, mod-only command to set the channel where toots posts order
status updates, deploy notifications, and error notices.

Opens an ephemeral channel-select dropdown (same pattern as /menu's
selectors), saves as soon as the mod picks.
"""

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


def _saved_default(
    guild: discord.Guild, channel_id: int | None,
) -> list[discord.SelectDefaultValue]:
    if channel_id is None:
        return []
    ch = guild.get_channel(channel_id)
    if not isinstance(ch, discord.TextChannel):
        return []
    return [
        discord.SelectDefaultValue(
            id=ch.id, type=discord.SelectDefaultValueType.channel,
        ),
    ]


class Logs(commands.Cog):
    def __init__(self, bot: TootsiesBot) -> None:
        self.bot = bot

    @app_commands.command(
        name="logs",
        description="pick the channel where i post order status + errors (mods only).",
    )
    @track_command("logs")
    async def logs(self, interaction: discord.Interaction) -> None:
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

        saved = await self.bot.db.get_setting(guild.id, "bot_logs_channel")
        saved_id = int(saved) if saved else None
        view = _LogsView(self.bot, guild, member.id, _saved_default(guild, saved_id))
        current_label = f"<#{saved_id}>" if saved_id else "_(none picked)_"
        embed = discord.Embed(
            title="logs channel",
            description=(
                "pick where i post order status + errors. "
                "saves when you pick.\n\n"
                f"currently: {current_label}"
            ),
            color=0x9b59b6,
        )
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)


class _LogsView(discord.ui.View):
    def __init__(
        self,
        bot: TootsiesBot,
        guild: discord.Guild,
        actor_id: int,
        defaults: list[discord.SelectDefaultValue],
    ) -> None:
        super().__init__(timeout=300)
        self.bot = bot
        self.guild = guild
        self.actor_id = actor_id
        self.add_item(_LogsChannelSelect(self, defaults))


class _LogsChannelSelect(discord.ui.ChannelSelect):
    def __init__(
        self, parent: _LogsView, defaults: list[discord.SelectDefaultValue],
    ) -> None:
        super().__init__(
            placeholder="pick logs channel",
            min_values=1, max_values=1, row=0,
            channel_types=[discord.ChannelType.text],
            default_values=defaults,
        )
        self.parent_view = parent

    async def callback(self, interaction: discord.Interaction) -> None:
        if interaction.user.id != self.parent_view.actor_id:
            await interaction.response.send_message(
                "not your menu, regular.", ephemeral=True,
            )
            return
        channel = self.values[0]
        guild_id = self.parent_view.guild.id
        await self.parent_view.bot.db.set_setting(
            guild_id, "bot_logs_channel", channel.id, interaction.user.id,
        )
        await self.parent_view.bot.db.audit(
            guild_id, interaction.user.id, "logs_channel_set",
            after={"channel_id": channel.id},
        )
        self.default_values = [
            discord.SelectDefaultValue(
                id=channel.id, type=discord.SelectDefaultValueType.channel,
            ),
        ]
        embed = discord.Embed(
            title="locked in.",
            description=f"logs going to <#{channel.id}> from here on.",
            color=0x2ecc71,
        )
        await interaction.response.edit_message(embed=embed, view=self.parent_view)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Logs(cast("TootsiesBot", bot)))
