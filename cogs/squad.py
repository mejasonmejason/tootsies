"""/squad family, tell Toots who her girls are.

/squad show              list the roles Toots treats as her girls
/squad add <role>        add a role to her girls (mods only)
/squad remove <role>     drop a role from her girls (mods only)
/squad clear             clear the whole list (mods only)

Anyone wearing one of these roles gets the extra-warm, feminine, sisterly
treatment in /ask + @mentions (see claude_client.ask's girls_context). Storage
is the settings KV table via db.get/set_girls_roles, so it's a small per-guild
list of role ids, no schema change.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, cast

import discord
from discord import app_commands
from discord.ext import commands

from utils import voice
from utils.gates import require_configured
from utils.metrics import track_command
from utils.permissions import is_mod

if TYPE_CHECKING:
    from bot import TootsiesBot

log = logging.getLogger(__name__)

# Suppress pings: rendering a role mention should show the colored name, not
# notify everyone wearing it every time a mod tweaks the list.
_NO_PING = discord.AllowedMentions.none()


class Squad(commands.GroupCog, name="squad"):  # type: ignore[call-arg]
    def __init__(self, bot: TootsiesBot) -> None:
        self.bot = bot
        super().__init__()

    @app_commands.command(name="show", description="see which roles toots treats as her girls.")
    @track_command("squad show")
    async def show(self, interaction: discord.Interaction) -> None:
        if not await require_configured(interaction, self.bot.db):
            return
        assert interaction.guild is not None
        role_ids = await self.bot.db.get_girls_roles(interaction.guild.id)
        if not role_ids:
            await interaction.response.send_message(
                "haven't met my girls yet. a mod can point them out with `/squad add`.",
                ephemeral=True,
            )
            return
        mentions = []
        for rid in role_ids:
            role = interaction.guild.get_role(rid)
            mentions.append(role.mention if role else f"(role {rid}, gone)")
        await interaction.response.send_message(
            "my girls: " + ", ".join(mentions),
            ephemeral=True,
            allowed_mentions=_NO_PING,
        )

    @app_commands.command(name="add", description="add a role to toots' girls. (mods only)")
    @app_commands.describe(role="the role to treat as one of the girls")
    @track_command("squad add")
    async def add(self, interaction: discord.Interaction, role: discord.Role) -> None:
        if not await self._mod_gate(interaction):
            return
        assert interaction.guild is not None
        guild_id = interaction.guild.id
        role_ids = await self.bot.db.get_girls_roles(guild_id)
        if role.id in role_ids:
            await interaction.response.send_message(
                f"{role.mention} already runs with my girls.",
                ephemeral=True,
                allowed_mentions=_NO_PING,
            )
            return
        role_ids.append(role.id)
        await self.bot.db.set_girls_roles(guild_id, role_ids, actor_id=interaction.user.id)
        await self.bot.db.audit(
            guild_id, interaction.user.id, "squad_add", after={"role_id": role.id}
        )
        await interaction.response.send_message(
            f"{role.mention}'s one of my girls now. i got them.",
            allowed_mentions=_NO_PING,
        )

    @app_commands.command(name="remove", description="drop a role from toots' girls. (mods only)")
    @app_commands.describe(role="the role to stop treating as one of the girls")
    @track_command("squad remove")
    async def remove(self, interaction: discord.Interaction, role: discord.Role) -> None:
        if not await self._mod_gate(interaction):
            return
        assert interaction.guild is not None
        guild_id = interaction.guild.id
        role_ids = await self.bot.db.get_girls_roles(guild_id)
        if role.id not in role_ids:
            await interaction.response.send_message(
                f"{role.mention} wasn't on the list anyway.",
                ephemeral=True,
                allowed_mentions=_NO_PING,
            )
            return
        role_ids = [rid for rid in role_ids if rid != role.id]
        await self.bot.db.set_girls_roles(guild_id, role_ids, actor_id=interaction.user.id)
        await self.bot.db.audit(
            guild_id, interaction.user.id, "squad_remove", after={"role_id": role.id}
        )
        await interaction.response.send_message(
            f"took {role.mention} off the list. no drama.",
            allowed_mentions=_NO_PING,
        )

    @app_commands.command(name="clear", description="clear toots' whole girls list. (mods only)")
    @track_command("squad clear")
    async def clear(self, interaction: discord.Interaction) -> None:
        if not await self._mod_gate(interaction):
            return
        assert interaction.guild is not None
        guild_id = interaction.guild.id
        if not await self.bot.db.get_girls_roles(guild_id):
            await interaction.response.send_message(
                "list's already empty.", ephemeral=True
            )
            return
        await self.bot.db.set_girls_roles(guild_id, [], actor_id=interaction.user.id)
        await self.bot.db.audit(guild_id, interaction.user.id, "squad_clear")
        await interaction.response.send_message("cleared the list. fresh slate.")

    async def _mod_gate(self, interaction: discord.Interaction) -> bool:
        if not await require_configured(interaction, self.bot.db):
            return False
        member = interaction.user
        if not isinstance(member, discord.Member):
            await interaction.response.send_message(
                voice.pick(voice.PERMISSION_DENIED), ephemeral=True
            )
            return False
        if not await is_mod(self.bot.db, member):
            await interaction.response.send_message(
                voice.pick(voice.PERMISSION_DENIED), ephemeral=True
            )
            return False
        return True


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Squad(cast("TootsiesBot", bot)))
