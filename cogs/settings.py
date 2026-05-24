"""/menu — interactive setup AND current-state view, one command.

Layout (5 rows, Discord's hard cap):
  row 0: bot-logs channel select
  row 1: discourse channel select
  row 2: mod roles select
  row 3: feed channels select
  row 4: [confirm & save] [cancel]

Mood (chill / yaps / off) is intentionally NOT in this menu. It's controlled
via `/discourse mood:<x>` so it can be changed on the fly without re-running
the whole setup wizard. New servers default to `chill` via the schema default.

Pre-population priority:
  1. Saved settings from the DB (if /menu was run before)
  2. Pattern-matched best guesses (channel/role names like Promoters, bot-logs)
  3. Empty (mod has to pick)
"""

from __future__ import annotations

import logging
import re
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

# Heuristics for first-time prefill — case-insensitive matches against
# channel/role names. Skipped entirely if the guild already has saved settings.
CHANNEL_PATTERNS = {
    "bot_logs_channel": [r"^bot-?logs$", r"^back-?of-?house$"],
    "discourse_channel": [r"^chatter$", r"^general$", r"^lounge$", r"^the-?bar$"],
}
MOD_ROLE_PATTERNS = [
    r"^Promoters$", r"^Bouncers$", r"^Janitors$",
    r"^Moderators?$", r"^Mods?$", r"^Admin$",
]
FEED_PATTERNS = [r"feed", r"alerts", r"x-?feed", r"tweets", r"news"]


def _match(name: str, patterns: list[str]) -> bool:
    return any(re.search(p, name, re.IGNORECASE) for p in patterns)


def _pattern_prefill(guild: discord.Guild) -> dict[str, object]:
    """Best-guess defaults derived from the guild's current channels and roles.
    Only used as fallback when nothing is saved yet."""
    out: dict[str, object] = {}
    for key, patterns in CHANNEL_PATTERNS.items():
        for ch in guild.text_channels:
            if _match(ch.name, patterns):
                out[key] = ch.id
                break
    out["mod_role_ids"] = [
        r.id for r in guild.roles if _match(r.name, MOD_ROLE_PATTERNS)
    ]
    out["feed_channel_ids"] = [
        ch.id for ch in guild.text_channels if _match(ch.name, FEED_PATTERNS)
    ]
    return out


async def _load_initial_state(bot: TootsiesBot, guild: discord.Guild) -> dict[str, object]:
    """Load saved settings (if any) and fill any gaps with pattern-based guesses.

    A re-run of /menu after setup should reflect what's actually configured —
    that makes /menu_view redundant. Pattern matching only kicks in for
    settings that haven't been saved yet.
    """
    saved_settings = await bot.db.all_settings(guild.id)
    saved_mod_roles = await bot.db.get_mod_roles(guild.id)
    saved_feed_channels = await bot.db.get_feed_channels(guild.id)

    state: dict[str, object] = {}
    if "bot_logs_channel" in saved_settings:
        state["bot_logs_channel"] = int(saved_settings["bot_logs_channel"])
    if "discourse_channel" in saved_settings:
        state["discourse_channel"] = int(saved_settings["discourse_channel"])
    if saved_mod_roles:
        state["mod_role_ids"] = list(saved_mod_roles)
    if saved_feed_channels:
        state["feed_channel_ids"] = [cid for cid, _cat in saved_feed_channels]

    # Fill any remaining gaps from name-pattern heuristics.
    guesses = _pattern_prefill(guild)
    for key, value in guesses.items():
        state.setdefault(key, value)
    return state


# ---- cog ------------------------------------------------------------------------


class Settings(commands.Cog):
    def __init__(self, bot: TootsiesBot) -> None:
        self.bot = bot

    @app_commands.command(
        name="menu",
        description="set toots up (or see/edit current settings). mods only.",
    )
    @track_command("menu")
    async def menu(self, interaction: discord.Interaction) -> None:
        member = interaction.user
        guild = interaction.guild
        if guild is None or not isinstance(member, discord.Member):
            await interaction.response.send_message(
                voice.pick(voice.PERMISSION_DENIED), ephemeral=True,
            )
            return
        # Owner / manage_guild are always allowed even pre-configuration, so the
        # first menu run works on a fresh install.
        if not (
            member.guild.owner_id == member.id
            or member.guild_permissions.manage_guild
            or await is_mod(self.bot.db, member)
        ):
            await interaction.response.send_message(
                voice.pick(voice.PERMISSION_DENIED), ephemeral=True,
            )
            return

        initial = await _load_initial_state(self.bot, guild)
        view = MenuView(self.bot, guild, initial, member.id)
        await interaction.response.send_message(
            embed=view.help_embed(), view=view, ephemeral=True,
        )


# ---- view -----------------------------------------------------------------------


def _safe_channel_default(
    guild: discord.Guild, channel_id: object,
) -> list[discord.SelectDefaultValue]:
    """Return a default_values entry for a channel ID — only if it still exists."""
    if not isinstance(channel_id, int):
        return []
    ch = guild.get_channel(channel_id)
    if not isinstance(ch, discord.TextChannel):
        return []
    return [
        discord.SelectDefaultValue(id=ch.id, type=discord.SelectDefaultValueType.channel),
    ]


def _safe_channel_defaults(
    guild: discord.Guild, channel_ids: object,
) -> list[discord.SelectDefaultValue]:
    if not isinstance(channel_ids, list):
        return []
    out: list[discord.SelectDefaultValue] = []
    for cid in channel_ids:
        if not isinstance(cid, int):
            continue
        ch = guild.get_channel(cid)
        if isinstance(ch, discord.TextChannel):
            out.append(discord.SelectDefaultValue(
                id=ch.id, type=discord.SelectDefaultValueType.channel,
            ))
    return out


def _safe_role_defaults(
    guild: discord.Guild, role_ids: object,
) -> list[discord.SelectDefaultValue]:
    if not isinstance(role_ids, list):
        return []
    out: list[discord.SelectDefaultValue] = []
    for rid in role_ids:
        if not isinstance(rid, int):
            continue
        if guild.get_role(rid) is not None:
            out.append(discord.SelectDefaultValue(
                id=rid, type=discord.SelectDefaultValueType.role,
            ))
    return out


class MenuView(discord.ui.View):
    """One view, four selectors, two bottom buttons. No sub-menus, no mood here."""

    def __init__(
        self,
        bot: TootsiesBot,
        guild: discord.Guild,
        prefill: dict[str, object],
        actor_id: int,
    ) -> None:
        super().__init__(timeout=600)
        self.bot = bot
        self.guild = guild
        self.actor_id = actor_id
        # Mirror of selects' current values. Initialized from prefill so `Confirm`
        # works even if the mod didn't touch any dropdown.
        self.selected: dict[str, object] = dict(prefill)

        self.add_item(_BotLogsSelect(
            self, _safe_channel_default(guild, prefill.get("bot_logs_channel")),
        ))
        self.add_item(_DiscourseSelect(
            self, _safe_channel_default(guild, prefill.get("discourse_channel")),
        ))
        self.add_item(_ModRoleSelect(
            self, _safe_role_defaults(guild, prefill.get("mod_role_ids")),
        ))
        self.add_item(_FeedSelect(
            self, _safe_channel_defaults(guild, prefill.get("feed_channel_ids")),
        ))

    # ---- embed -----------------------------------------------------------------

    def help_embed(self) -> discord.Embed:
        e = discord.Embed(
            title="toots' menu",
            description=(
                "configure the four settings below. each dropdown is labeled with "
                "what it controls. when you're happy, hit **confirm & save**.\n\n"
                "_re-running `/menu` later shows your current settings so you can "
                "tweak. for the scheduled-posting mood (chill / yaps / off), use_ "
                "`/discourse mood:<x>` _instead — it's separate so you can change "
                "it on the fly without re-running setup._"
            ),
            color=0x9b59b6,
        )
        e.add_field(
            name="📊  bot-logs channel",
            value="where I narrate `/order` status (🟡 → 🍳 → ✅).",
            inline=False,
        )
        e.add_field(
            name="💬  discourse channel",
            value="where scheduled discourse posts land.",
            inline=False,
        )
        e.add_field(
            name="👮  mod roles",
            value="who can run `/order`, `/close`, `/open`, `/undo`, `/menu`.",
            inline=False,
        )
        e.add_field(
            name="📰  feed channels",
            value="read-only sources I pull from for `/discourse` (X feeds, news, alerts). optional.",
            inline=False,
        )
        return e

    async def _check_actor(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.actor_id:
            await interaction.response.send_message(
                "not your menu, regular.", ephemeral=True,
            )
            return False
        return True

    # ---- buttons (row 4) -------------------------------------------------------

    @discord.ui.button(label="confirm & save", style=discord.ButtonStyle.success, row=4)
    async def confirm(
        self, interaction: discord.Interaction, _btn: discord.ui.Button,
    ) -> None:
        if not await self._check_actor(interaction):
            return
        if interaction.guild is None:
            return
        guild_id = interaction.guild.id

        # Validate the bare minimum.
        missing = []
        if not self.selected.get("bot_logs_channel"):
            missing.append("bot-logs channel")
        if not self.selected.get("discourse_channel"):
            missing.append("discourse channel")
        mod_role_ids = self.selected.get("mod_role_ids") or []
        if not isinstance(mod_role_ids, list) or not mod_role_ids:
            missing.append("mod role(s)")
        if missing:
            await interaction.response.send_message(
                f"need to pick: {', '.join(missing)}.", ephemeral=True,
            )
            return

        for key in ("bot_logs_channel", "discourse_channel"):
            val = self.selected.get(key)
            if val:
                await self.bot.db.set_setting(
                    guild_id, key, int(cast("int", val)), self.actor_id,
                )

        await self.bot.db.set_setting(
            guild_id, "per_user_daily_limit", 20, self.actor_id,
        )
        await self.bot.db.set_setting(
            guild_id, "per_server_daily_limit", 20, self.actor_id,
        )

        await self.bot.db.set_mod_roles(
            guild_id, [int(r) for r in cast("list[int]", mod_role_ids)],
        )

        feeds = self.selected.get("feed_channel_ids") or []
        if isinstance(feeds, list):
            await self.bot.db.set_feed_channels(
                guild_id, [(int(c), None) for c in feeds],
            )

        await self.bot.db.mark_configured(guild_id)
        await self.bot.db.audit(
            guild_id, self.actor_id, "menu_saved", after=self.selected,
        )

        for child in self.children:
            if isinstance(child, discord.ui.Button | discord.ui.Select):
                child.disabled = True

        confirm_embed = discord.Embed(
            title="locked in. bar's open.",
            color=0x2ecc71,
            description=self._summary(),
        )
        await interaction.response.edit_message(
            content=None, embed=confirm_embed, view=self,
        )

    @discord.ui.button(label="cancel", style=discord.ButtonStyle.secondary, row=4)
    async def cancel(
        self, interaction: discord.Interaction, _btn: discord.ui.Button,
    ) -> None:
        if not await self._check_actor(interaction):
            return
        for child in self.children:
            if isinstance(child, discord.ui.Button | discord.ui.Select):
                child.disabled = True
        await interaction.response.edit_message(
            content="closed without saving.", embed=None, view=self,
        )

    def _summary(self) -> str:
        bot_logs = self.selected.get("bot_logs_channel")
        discourse = self.selected.get("discourse_channel")
        mod_roles = self.selected.get("mod_role_ids") or []
        feeds = self.selected.get("feed_channel_ids") or []
        return (
            f"**bot-logs:** <#{bot_logs}>\n"
            f"**discourse:** <#{discourse}>\n"
            f"**mod roles:** "
            f"{', '.join(f'<@&{r}>' for r in cast('list[int]', mod_roles))}\n"
            f"**feeds:** {len(cast('list[int]', feeds))} channel(s)"
        )


# ---- selects (each takes one row) -------------------------------------------------


class _BotLogsSelect(discord.ui.ChannelSelect):
    def __init__(
        self, parent: MenuView, defaults: list[discord.SelectDefaultValue],
    ) -> None:
        super().__init__(
            placeholder="📊 bot-logs channel",
            min_values=1, max_values=1, row=0,
            channel_types=[discord.ChannelType.text],
            default_values=defaults,
        )
        self.parent_view = parent

    async def callback(self, interaction: discord.Interaction) -> None:
        self.parent_view.selected["bot_logs_channel"] = self.values[0].id
        await interaction.response.defer()


class _DiscourseSelect(discord.ui.ChannelSelect):
    def __init__(
        self, parent: MenuView, defaults: list[discord.SelectDefaultValue],
    ) -> None:
        super().__init__(
            placeholder="💬 discourse channel",
            min_values=1, max_values=1, row=1,
            channel_types=[discord.ChannelType.text],
            default_values=defaults,
        )
        self.parent_view = parent

    async def callback(self, interaction: discord.Interaction) -> None:
        self.parent_view.selected["discourse_channel"] = self.values[0].id
        await interaction.response.defer()


class _ModRoleSelect(discord.ui.RoleSelect):
    def __init__(
        self, parent: MenuView, defaults: list[discord.SelectDefaultValue],
    ) -> None:
        super().__init__(
            placeholder="👮 mod roles",
            min_values=1, max_values=10, row=2,
            default_values=defaults,
        )
        self.parent_view = parent

    async def callback(self, interaction: discord.Interaction) -> None:
        self.parent_view.selected["mod_role_ids"] = [r.id for r in self.values]
        await interaction.response.defer()


class _FeedSelect(discord.ui.ChannelSelect):
    def __init__(
        self, parent: MenuView, defaults: list[discord.SelectDefaultValue],
    ) -> None:
        super().__init__(
            placeholder="📰 feed channels (optional)",
            min_values=0, max_values=25, row=3,
            channel_types=[discord.ChannelType.text],
            default_values=defaults,
        )
        self.parent_view = parent

    async def callback(self, interaction: discord.Interaction) -> None:
        self.parent_view.selected["feed_channel_ids"] = [c.id for c in self.values]
        await interaction.response.defer()


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Settings(cast("TootsiesBot", bot)))
