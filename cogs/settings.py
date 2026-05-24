"""/menu — interactive setup wizard with smart defaults.

Per the plan, `/menu` prefills with Toots's best guesses based on channel/role names so it
becomes a confirmation step rather than a typing exercise.
"""

from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING, cast

import discord
from discord import app_commands
from discord.ext import commands

from utils import voice
from utils.permissions import is_mod

if TYPE_CHECKING:
    from bot import TootsiesBot

log = logging.getLogger(__name__)

# Heuristics for prefill — case-insensitive substring matches against channel/role names.
CHANNEL_PATTERNS = {
    "bot_logs_channel": [r"^bot-?logs$", r"^back-?of-?house$"],
    "the_bar_channel": [r"^the-?bar$", r"^bar$"],
    "discourse_channel": [r"^chatter$", r"^general$", r"^lounge$"],
}
MOD_ROLE_PATTERNS = [r"^Promoters$", r"^Bouncers$", r"^Janitors$", r"^Moderators?$", r"^Mods?$", r"^Admin$"]
FEED_PATTERNS = [r"feed", r"alerts", r"x-?feed", r"tweets", r"news"]


def _match(name: str, patterns: list[str]) -> bool:
    return any(re.search(p, name, re.IGNORECASE) for p in patterns)


class Settings(commands.Cog):
    def __init__(self, bot: TootsiesBot) -> None:
        self.bot = bot

    @app_commands.command(name="menu", description="set toots up. (mods only)")
    async def menu(self, interaction: discord.Interaction) -> None:
        member = interaction.user
        guild = interaction.guild
        if guild is None or not isinstance(member, discord.Member):
            await interaction.response.send_message(voice.pick(voice.PERMISSION_DENIED), ephemeral=True)
            return
        # Allow guild owner / manage_guild even before /menu has set up mod roles.
        if not (
            member.guild.owner_id == member.id
            or member.guild_permissions.manage_guild
            or await is_mod(self.bot.db, member)
        ):
            await interaction.response.send_message(voice.pick(voice.PERMISSION_DENIED), ephemeral=True)
            return

        prefill = _prefill(guild)
        view = MenuView(self.bot, prefill, member.id)
        await interaction.response.send_message(
            embed=view.summary_embed(), view=view, ephemeral=True
        )

    @app_commands.command(name="menu_view", description="see current settings.")
    async def menu_view(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None:
            return
        cfg = await self.bot.db.all_settings(interaction.guild.id)
        mod_roles = await self.bot.db.get_mod_roles(interaction.guild.id)
        feeds = await self.bot.db.get_feed_channels(interaction.guild.id)
        mood = await self.bot.db.get_schedule(interaction.guild.id)
        lines = [
            f"**mood:** {mood.mode.value}",
            f"**mod roles:** {', '.join(f'<@&{r}>' for r in mod_roles) or '(none)'}",
            f"**feeds:** {len(feeds)} channel(s)",
        ]
        for k, v in sorted(cfg.items()):
            lines.append(f"**{k}:** {v}")
        await interaction.response.send_message("\n".join(lines), ephemeral=True)


# ---- prefill ---------------------------------------------------------------------


def _prefill(guild: discord.Guild) -> dict[str, object]:
    """Walk the guild's channels/roles and pull out probable defaults."""
    out: dict[str, object] = {}
    for key, patterns in CHANNEL_PATTERNS.items():
        for ch in guild.text_channels:
            if _match(ch.name, patterns):
                out[key] = ch.id
                break
    out["mod_role_ids"] = [r.id for r in guild.roles if _match(r.name, MOD_ROLE_PATTERNS)]
    out["feed_channel_ids"] = [
        ch.id for ch in guild.text_channels if _match(ch.name, FEED_PATTERNS)
    ]
    out["per_user_daily_limit"] = 20
    out["per_server_daily_limit"] = 20
    out["mood"] = "chill"
    return out


# ---- views -----------------------------------------------------------------------


class MenuView(discord.ui.View):
    """Top-level menu — buttons for each section + a 'save' that commits the prefill as-is."""

    def __init__(self, bot: TootsiesBot, prefill: dict[str, object], actor_id: int) -> None:
        super().__init__(timeout=600)
        self.bot = bot
        self.prefill = prefill
        self.actor_id = actor_id

    def summary_embed(self) -> discord.Embed:
        e = discord.Embed(
            title="toots' menu",
            description="here's what i'm guessing. confirm or change.",
            color=0x9b59b6,
        )
        e.add_field(
            name="channels",
            value=self._fmt_channels(),
            inline=False,
        )
        e.add_field(
            name="mod roles",
            value=self._fmt_roles(),
            inline=False,
        )
        e.add_field(
            name="feeds",
            value=self._fmt_feeds(),
            inline=False,
        )
        e.add_field(
            name="limits",
            value=f"per-user {self.prefill.get('per_user_daily_limit', 20)}/day · "
                  f"per-server {self.prefill.get('per_server_daily_limit', 20)}/day",
            inline=False,
        )
        e.add_field(name="mood", value=str(self.prefill.get("mood", "chill")), inline=False)
        return e

    def _fmt_channels(self) -> str:
        lines = []
        for key in ("bot_logs_channel", "the_bar_channel", "discourse_channel"):
            val = self.prefill.get(key)
            lines.append(f"`{key}`: {f'<#{val}>' if val else '_(unset — pick one)_'}")
        return "\n".join(lines)

    def _fmt_roles(self) -> str:
        ids = self.prefill.get("mod_role_ids") or []
        if not isinstance(ids, list) or not ids:
            return "_(none detected — pick at least one)_"
        return ", ".join(f"<@&{rid}>" for rid in ids)

    def _fmt_feeds(self) -> str:
        ids = self.prefill.get("feed_channel_ids") or []
        if not isinstance(ids, list) or not ids:
            return "_(none detected)_"
        return ", ".join(f"<#{cid}>" for cid in ids[:8]) + ("…" if len(ids) > 8 else "")

    async def _check_actor(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.actor_id:
            await interaction.response.send_message("not your menu, regular.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="confirm & save", style=discord.ButtonStyle.success, row=0)
    async def confirm(self, interaction: discord.Interaction, _btn: discord.ui.Button) -> None:
        if not await self._check_actor(interaction):
            return
        if interaction.guild is None:
            return
        guild_id = interaction.guild.id

        for key in ("bot_logs_channel", "the_bar_channel", "discourse_channel"):
            val = self.prefill.get(key)
            if val:
                await self.bot.db.set_setting(guild_id, key, val, self.actor_id)
        await self.bot.db.set_setting(
            guild_id, "per_user_daily_limit",
            int(cast("int", self.prefill.get("per_user_daily_limit", 20))), self.actor_id,
        )
        await self.bot.db.set_setting(
            guild_id, "per_server_daily_limit",
            int(cast("int", self.prefill.get("per_server_daily_limit", 20))), self.actor_id,
        )
        mood = self.prefill.get("mood", "chill")
        if isinstance(mood, str):
            from models import MoodMode
            await self.bot.db.set_schedule(guild_id, MoodMode(mood), self.actor_id)

        roles = self.prefill.get("mod_role_ids") or []
        if isinstance(roles, list):
            await self.bot.db.set_mod_roles(guild_id, [int(r) for r in roles])

        feeds = self.prefill.get("feed_channel_ids") or []
        if isinstance(feeds, list):
            await self.bot.db.set_feed_channels(guild_id, [(int(c), None) for c in feeds])

        await self.bot.db.mark_configured(guild_id)
        await self.bot.db.audit(
            guild_id, self.actor_id, "menu_saved", after=self.prefill
        )
        for child in self.children:
            if isinstance(child, discord.ui.Button):
                child.disabled = True
        await interaction.response.edit_message(
            content="locked in. bar's open.", embed=self.summary_embed(), view=self,
        )

    @discord.ui.button(label="channels", style=discord.ButtonStyle.secondary, row=1)
    async def channels(self, interaction: discord.Interaction, _btn: discord.ui.Button) -> None:
        if not await self._check_actor(interaction):
            return
        await interaction.response.send_message(
            "pick channels:", ephemeral=True, view=ChannelPickerView(self, interaction.guild),
        )

    @discord.ui.button(label="mod roles", style=discord.ButtonStyle.secondary, row=1)
    async def roles(self, interaction: discord.Interaction, _btn: discord.ui.Button) -> None:
        if not await self._check_actor(interaction):
            return
        await interaction.response.send_message(
            "pick mod roles:", ephemeral=True, view=RolePickerView(self, interaction.guild),
        )

    @discord.ui.button(label="feeds", style=discord.ButtonStyle.secondary, row=1)
    async def feeds(self, interaction: discord.Interaction, _btn: discord.ui.Button) -> None:
        if not await self._check_actor(interaction):
            return
        await interaction.response.send_message(
            "pick feed channels:", ephemeral=True, view=FeedPickerView(self, interaction.guild),
        )

    @discord.ui.button(label="mood", style=discord.ButtonStyle.secondary, row=2)
    async def mood(self, interaction: discord.Interaction, _btn: discord.ui.Button) -> None:
        if not await self._check_actor(interaction):
            return
        await interaction.response.send_message(
            "pick mood:", ephemeral=True, view=MoodPickerView(self),
        )


class ChannelPickerView(discord.ui.View):
    def __init__(self, parent: MenuView, guild: discord.Guild | None) -> None:
        super().__init__(timeout=300)
        self.parent = parent
        self.guild = guild
        self.add_item(_ChannelSelect(parent, "bot_logs_channel", "#bot-logs channel"))
        self.add_item(_ChannelSelect(parent, "discourse_channel", "discourse channel"))


class _ChannelSelect(discord.ui.ChannelSelect):
    def __init__(self, parent: MenuView, key: str, label: str) -> None:
        super().__init__(
            placeholder=label, min_values=1, max_values=1,
            channel_types=[discord.ChannelType.text],
        )
        self.parent_view = parent
        self.key = key

    async def callback(self, interaction: discord.Interaction) -> None:
        self.parent_view.prefill[self.key] = self.values[0].id
        await interaction.response.send_message(
            f"saved `{self.key}` → <#{self.values[0].id}>. open `/menu` again to see updates.",
            ephemeral=True,
        )


class RolePickerView(discord.ui.View):
    def __init__(self, parent: MenuView, guild: discord.Guild | None) -> None:
        super().__init__(timeout=300)
        self.parent = parent
        self.add_item(_RoleSelect(parent))


class _RoleSelect(discord.ui.RoleSelect):
    def __init__(self, parent: MenuView) -> None:
        super().__init__(placeholder="mod roles", min_values=1, max_values=10)
        self.parent_view = parent

    async def callback(self, interaction: discord.Interaction) -> None:
        self.parent_view.prefill["mod_role_ids"] = [r.id for r in self.values]
        await interaction.response.send_message("mod roles updated.", ephemeral=True)


class FeedPickerView(discord.ui.View):
    def __init__(self, parent: MenuView, guild: discord.Guild | None) -> None:
        super().__init__(timeout=300)
        self.add_item(_FeedSelect(parent))


class _FeedSelect(discord.ui.ChannelSelect):
    def __init__(self, parent: MenuView) -> None:
        super().__init__(
            placeholder="feed channels", min_values=0, max_values=25,
            channel_types=[discord.ChannelType.text],
        )
        self.parent_view = parent

    async def callback(self, interaction: discord.Interaction) -> None:
        self.parent_view.prefill["feed_channel_ids"] = [c.id for c in self.values]
        await interaction.response.send_message(
            f"feed channels: {len(self.values)} selected.", ephemeral=True
        )


class MoodPickerView(discord.ui.View):
    def __init__(self, parent: MenuView) -> None:
        super().__init__(timeout=300)
        self.add_item(_MoodSelect(parent))


class _MoodSelect(discord.ui.Select):
    def __init__(self, parent: MenuView) -> None:
        super().__init__(
            placeholder="default mood",
            options=[
                discord.SelectOption(label="chill (2/day)", value="chill", default=True),
                discord.SelectOption(label="yaps (4/day)", value="yaps"),
                discord.SelectOption(label="off", value="off"),
            ],
        )
        self.parent_view = parent

    async def callback(self, interaction: discord.Interaction) -> None:
        self.parent_view.prefill["mood"] = self.values[0]
        await interaction.response.send_message(f"mood → {self.values[0]}.", ephemeral=True)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Settings(cast("TootsiesBot", bot)))
