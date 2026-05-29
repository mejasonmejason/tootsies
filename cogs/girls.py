"""/girls, name the roles Toots treats as her girls.

One command, one autosaving role multi-select (same pattern as the `/menu`
pickers). Pick the role(s) for the girls (e.g. @Habibtis); it saves as you go,
deselect everything to clear. Anyone wearing one of these roles gets the
extra-warm, feminine, sisterly treatment in /ask + @mentions (see
claude_client.ask's girls_context). Storage is the settings KV table via
db.get/set_girls_roles, so it's a small per-guild list of role ids, no schema
change.
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


def _role_defaults(
    guild: discord.Guild, role_ids: list[int],
) -> list[discord.SelectDefaultValue]:
    """Pre-populate the select with roles that still exist in the guild."""
    out: list[discord.SelectDefaultValue] = []
    for rid in role_ids:
        if guild.get_role(rid) is not None:
            out.append(discord.SelectDefaultValue(
                id=rid, type=discord.SelectDefaultValueType.role,
            ))
    return out


class _GirlsRoleSelect(discord.ui.RoleSelect):
    LABEL = "the girls"

    def __init__(
        self, parent: GirlsView, defaults: list[discord.SelectDefaultValue],
    ) -> None:
        # min_values=0 so deselecting everything clears the list.
        super().__init__(
            placeholder=self.LABEL,
            min_values=0, max_values=25,
            default_values=defaults,
        )
        self.parent_view = parent

    async def callback(self, interaction: discord.Interaction) -> None:
        self.parent_view.selected = [r.id for r in self.values]
        await self.parent_view.autosave(interaction)


class GirlsView(discord.ui.View):
    """One role select, autosaves on every pick. Only the invoking mod can use it."""

    def __init__(
        self,
        bot: TootsiesBot,
        guild: discord.Guild,
        role_ids: list[int],
        actor_id: int,
    ) -> None:
        super().__init__(timeout=600)
        self.bot = bot
        self.guild = guild
        self.actor_id = actor_id
        self.selected: list[int] = list(role_ids)
        self.add_item(_GirlsRoleSelect(self, _role_defaults(guild, role_ids)))
        self._sync_select()

    def embed(self) -> discord.Embed:
        if self.selected:
            mentions = []
            for rid in self.selected:
                role = self.guild.get_role(rid)
                mentions.append(role.mention if role else f"(role {rid}, gone)")
            current = "my girls: " + ", ".join(mentions)
        else:
            current = "no girls picked yet."
        return discord.Embed(
            title="who are my girls?",
            description=(
                f"{current}\n\n"
                "pick the role(s) i should treat as my girls (i'm extra warm "
                "with them). saves as you go, deselect all to clear."
            ),
            color=0x9b59b6,
        )

    def _sync_select(self) -> None:
        """Re-sync the select's default_values + placeholder to self.selected
        before any edit_message, or the view visually clears on re-render.
        (Not named _refresh: discord.ui.View already has a private _refresh.)"""
        for child in self.children:
            if isinstance(child, _GirlsRoleSelect):
                child.default_values = _role_defaults(self.guild, self.selected)
                child.placeholder = (
                    f"{child.LABEL}: {len(self.selected)} picked"
                    if self.selected else child.LABEL
                )

    async def autosave(self, interaction: discord.Interaction) -> None:
        if interaction.user.id != self.actor_id:
            await interaction.response.send_message(
                "not your list, regular.", ephemeral=True,
            )
            return
        guild_id = self.guild.id
        try:
            await self.bot.db.set_girls_roles(
                guild_id, self.selected, actor_id=self.actor_id,
            )
        except Exception:
            log.exception("girls roles autosave failed for guild=%s", guild_id)
            # Don't block the visual update; the value is in self.selected so a
            # re-select retries the write.
        else:
            await self.bot.db.audit(
                guild_id, self.actor_id, "girls_set",
                after={"role_ids": self.selected},
            )
        self._sync_select()
        await interaction.response.edit_message(embed=self.embed(), view=self)


class Girls(commands.Cog):
    def __init__(self, bot: TootsiesBot) -> None:
        self.bot = bot

    @app_commands.command(
        name="girls",
        description="name the girls i'm extra warm with. (mods only)",
    )
    @track_command("girls")
    async def girls(self, interaction: discord.Interaction) -> None:
        if not await require_configured(interaction, self.bot.db):
            return
        member = interaction.user
        guild = interaction.guild
        if guild is None or not isinstance(member, discord.Member):
            await interaction.response.send_message(
                voice.pick(voice.PERMISSION_DENIED), ephemeral=True,
            )
            return
        if not await is_mod(self.bot.db, member):
            await interaction.response.send_message(
                voice.pick(voice.PERMISSION_DENIED), ephemeral=True,
            )
            return
        role_ids = await self.bot.db.get_girls_roles(guild.id)
        view = GirlsView(self.bot, guild, role_ids, member.id)
        await interaction.response.send_message(
            embed=view.embed(), view=view, ephemeral=True,
        )


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Girls(cast("TootsiesBot", bot)))
