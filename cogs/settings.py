"""/menu — interactive setup wizard.

All channel/role/feed selectors live on the main view so a mod sees the actual Discord state
in one screen. Pattern matching against Tootsies-style naming (`the-bar`, `chatter`, `bot-logs`,
`Promoters`/`Bouncers`/`Janitors`) becomes the pre-selected default; you can override anything
with one click. No nested sub-menus.
"""

from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING, cast

import discord
from discord import app_commands
from discord.ext import commands

from models import MoodMode
from utils import voice
from utils.permissions import is_mod

if TYPE_CHECKING:
    from bot import TootsiesBot

log = logging.getLogger(__name__)

# Heuristics for prefill — case-insensitive matches against channel/role names.
CHANNEL_PATTERNS = {
    "bot_logs_channel": [r"^bot-?logs$", r"^back-?of-?house$"],
    "discourse_channel": [r"^chatter$", r"^general$", r"^lounge$", r"^the-?bar$"],
}
MOD_ROLE_PATTERNS = [r"^Promoters$", r"^Bouncers$", r"^Janitors$", r"^Moderators?$", r"^Mods?$", r"^Admin$"]
FEED_PATTERNS = [r"feed", r"alerts", r"x-?feed", r"tweets", r"news"]
MOOD_CYCLE = ["chill", "yaps", "off"]


def _match(name: str, patterns: list[str]) -> bool:
    return any(re.search(p, name, re.IGNORECASE) for p in patterns)


def _prefill(guild: discord.Guild) -> dict[str, object]:
    """Best-guess defaults derived from the guild's current channels and roles."""
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
    out["mood"] = "chill"
    return out


# ---- cog ------------------------------------------------------------------------


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
        # Owner / manage_guild are always allowed even pre-configuration, so the first menu run
        # works on a fresh install.
        if not (
            member.guild.owner_id == member.id
            or member.guild_permissions.manage_guild
            or await is_mod(self.bot.db, member)
        ):
            await interaction.response.send_message(voice.pick(voice.PERMISSION_DENIED), ephemeral=True)
            return

        view = MenuView(self.bot, guild, _prefill(guild), member.id)
        await interaction.response.send_message(
            embed=view.help_embed(), view=view, ephemeral=True
        )

    @app_commands.command(name="menu_view", description="see current settings.")
    async def menu_view(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None:
            return
        cfg = await self.bot.db.all_settings(interaction.guild.id)
        mod_roles = await self.bot.db.get_mod_roles(interaction.guild.id)
        feeds = await self.bot.db.get_feed_channels(interaction.guild.id)
        schedule = await self.bot.db.get_schedule(interaction.guild.id)
        lines = [
            f"**mood:** {schedule.mode.value}",
            f"**mod roles:** {', '.join(f'<@&{r}>' for r in mod_roles) or '(none)'}",
            f"**feeds:** {len(feeds)} channel(s)",
        ]
        for k, v in sorted(cfg.items()):
            lines.append(f"**{k}:** {v}")
        await interaction.response.send_message("\n".join(lines), ephemeral=True)


# ---- view -----------------------------------------------------------------------


def _safe_channel_default(
    guild: discord.Guild, channel_id: object
) -> list[discord.SelectDefaultValue]:
    """Return a default_values entry for a channel ID — only if it still exists."""
    if not isinstance(channel_id, int):
        return []
    ch = guild.get_channel(channel_id)
    if not isinstance(ch, discord.TextChannel):
        return []
    return [discord.SelectDefaultValue(id=ch.id, type=discord.SelectDefaultValueType.channel)]


def _safe_channel_defaults(
    guild: discord.Guild, channel_ids: object
) -> list[discord.SelectDefaultValue]:
    if not isinstance(channel_ids, list):
        return []
    out: list[discord.SelectDefaultValue] = []
    for cid in channel_ids:
        if not isinstance(cid, int):
            continue
        ch = guild.get_channel(cid)
        if isinstance(ch, discord.TextChannel):
            out.append(
                discord.SelectDefaultValue(id=ch.id, type=discord.SelectDefaultValueType.channel)
            )
    return out


def _safe_role_defaults(
    guild: discord.Guild, role_ids: object
) -> list[discord.SelectDefaultValue]:
    if not isinstance(role_ids, list):
        return []
    out: list[discord.SelectDefaultValue] = []
    for rid in role_ids:
        if not isinstance(rid, int):
            continue
        if guild.get_role(rid) is not None:
            out.append(
                discord.SelectDefaultValue(id=rid, type=discord.SelectDefaultValueType.role)
            )
    return out


class MenuView(discord.ui.View):
    """One view, four selectors, three buttons. No sub-menus."""

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
        self.prefill = prefill
        self.actor_id = actor_id
        # Channel/role state mirrors the selects' current values. We initialize from prefill so
        # `Confirm` works even if the mod didn't touch any dropdown.
        self.selected: dict[str, object] = dict(prefill)

        bot_logs = _BotLogsSelect(self, _safe_channel_default(guild, prefill.get("bot_logs_channel")))
        discourse = _DiscourseSelect(self, _safe_channel_default(guild, prefill.get("discourse_channel")))
        mod_roles = _ModRoleSelect(self, _safe_role_defaults(guild, prefill.get("mod_role_ids")))
        feeds = _FeedSelect(self, _safe_channel_defaults(guild, prefill.get("feed_channel_ids")))

        self.add_item(bot_logs)
        self.add_item(discourse)
        self.add_item(mod_roles)
        self.add_item(feeds)

    # ---- embed -----------------------------------------------------------------

    def help_embed(self) -> discord.Embed:
        e = discord.Embed(
            title="toots' menu",
            description=(
                "pick the channels and roles below — i pre-selected my best guesses, "
                "tweak whatever's wrong. then hit **confirm & save**."
            ),
            color=0x9b59b6,
        )
        e.add_field(
            name="what each one means",
            value=(
                "**bot-logs channel** — where i narrate `/order` status (🟡→🍳→✅).\n"
                "**discourse channel** — where scheduled discourse posts land.\n"
                "**mod roles** — who can run `/order`, `/close`, `/open`, `/undo`, `/menu`.\n"
                "**feed channels** — read-only sources i pull from for `/discourse` "
                "(X feeds, news, alerts, etc.).\n"
                "**mood** — chill (2/day), yaps (4/day), or off. Cycles on click."
            ),
            inline=False,
        )
        return e

    async def _check_actor(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.actor_id:
            await interaction.response.send_message("not your menu, regular.", ephemeral=True)
            return False
        return True

    # ---- buttons (row 4) -------------------------------------------------------

    @discord.ui.button(label="confirm & save", style=discord.ButtonStyle.success, row=4)
    async def confirm(self, interaction: discord.Interaction, _btn: discord.ui.Button) -> None:
        if not await self._check_actor(interaction):
            return
        if interaction.guild is None:
            return
        guild_id = interaction.guild.id

        # Validate the bare minimum — bot_logs, discourse, and at least one mod role.
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
                f"need to pick: {', '.join(missing)}.", ephemeral=True
            )
            return

        for key in ("bot_logs_channel", "discourse_channel"):
            val = self.selected.get(key)
            if val:
                await self.bot.db.set_setting(guild_id, key, int(cast("int", val)), self.actor_id)

        await self.bot.db.set_setting(
            guild_id, "per_user_daily_limit", 20, self.actor_id,
        )
        await self.bot.db.set_setting(
            guild_id, "per_server_daily_limit", 20, self.actor_id,
        )

        mood = self.selected.get("mood", "chill")
        if isinstance(mood, str):
            await self.bot.db.set_schedule(guild_id, MoodMode(mood), self.actor_id)

        await self.bot.db.set_mod_roles(
            guild_id, [int(r) for r in cast("list[int]", mod_role_ids)]
        )

        feeds = self.selected.get("feed_channel_ids") or []
        if isinstance(feeds, list):
            await self.bot.db.set_feed_channels(guild_id, [(int(c), None) for c in feeds])

        await self.bot.db.mark_configured(guild_id)
        await self.bot.db.audit(
            guild_id, self.actor_id, "menu_saved", after=self.selected
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

    @discord.ui.button(label="mood: chill", style=discord.ButtonStyle.secondary, row=4)
    async def cycle_mood(self, interaction: discord.Interaction, btn: discord.ui.Button) -> None:
        if not await self._check_actor(interaction):
            return
        current = str(self.selected.get("mood", "chill"))
        i = MOOD_CYCLE.index(current) if current in MOOD_CYCLE else 0
        new = MOOD_CYCLE[(i + 1) % len(MOOD_CYCLE)]
        self.selected["mood"] = new
        btn.label = f"mood: {new}"
        await interaction.response.edit_message(view=self)

    @discord.ui.button(label="cancel", style=discord.ButtonStyle.secondary, row=4)
    async def cancel(self, interaction: discord.Interaction, _btn: discord.ui.Button) -> None:
        if not await self._check_actor(interaction):
            return
        for child in self.children:
            if isinstance(child, discord.ui.Button | discord.ui.Select):
                child.disabled = True
        await interaction.response.edit_message(
            content="closed without saving.", embed=None, view=self
        )

    def _summary(self) -> str:
        bot_logs = self.selected.get("bot_logs_channel")
        discourse = self.selected.get("discourse_channel")
        mod_roles = self.selected.get("mod_role_ids") or []
        feeds = self.selected.get("feed_channel_ids") or []
        return (
            f"**bot-logs:** <#{bot_logs}>\n"
            f"**discourse:** <#{discourse}>\n"
            f"**mod roles:** {', '.join(f'<@&{r}>' for r in cast('list[int]', mod_roles))}\n"
            f"**feeds:** {len(cast('list[int]', feeds))} channel(s)\n"
            f"**mood:** {self.selected.get('mood', 'chill')}"
        )


# ---- selects (each takes one row) -------------------------------------------------


class _BotLogsSelect(discord.ui.ChannelSelect):
    def __init__(
        self, parent: MenuView, defaults: list[discord.SelectDefaultValue]
    ) -> None:
        super().__init__(
            placeholder="bot-logs channel (where status posts go)",
            min_values=1, max_values=1, row=0,
            channel_types=[discord.ChannelType.text],
            default_values=defaults,
        )
        self.parent_view = parent

    async def callback(self, interaction: discord.Interaction) -> None:
        self.parent_view.selected["bot_logs_channel"] = self.values[0].id
        await interaction.response.defer()  # silent — embed doesn't need refresh


class _DiscourseSelect(discord.ui.ChannelSelect):
    def __init__(
        self, parent: MenuView, defaults: list[discord.SelectDefaultValue]
    ) -> None:
        super().__init__(
            placeholder="discourse channel (where scheduled posts go)",
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
        self, parent: MenuView, defaults: list[discord.SelectDefaultValue]
    ) -> None:
        super().__init__(
            placeholder="mod roles (Promoters, Bouncers, Janitors…)",
            min_values=1, max_values=10, row=2,
            default_values=defaults,
        )
        self.parent_view = parent

    async def callback(self, interaction: discord.Interaction) -> None:
        self.parent_view.selected["mod_role_ids"] = [r.id for r in self.values]
        await interaction.response.defer()


class _FeedSelect(discord.ui.ChannelSelect):
    def __init__(
        self, parent: MenuView, defaults: list[discord.SelectDefaultValue]
    ) -> None:
        super().__init__(
            placeholder="feed channels (X feeds, news — optional)",
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
