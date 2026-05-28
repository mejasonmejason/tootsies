"""/menu, interactive setup AND current-state view, one command.

Layout (5 rows, Discord's hard cap, all selects with auto-save).
Ordered by importance: mod roles gates every admin command, so it
comes first. Bot-logs and feeds (the read/write plumbing channels)
are grouped together at the bottom.
  row 0: mod roles select
  row 1: discourse channel select
  row 2: mood select (chill / yaps / off)
  row 3: bot-logs channel select
  row 4: feed channels select

Every change saves immediately to the DB, no confirm button. The view
re-renders the summary embed after each change so the mod sees the
current state. If they want to undo, just pick a different value.

Pre-population: saved settings from the DB (if /menu was run before),
otherwise empty (mod has to pick).
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, cast

import discord
from discord import app_commands
from discord.ext import commands

from models import MoodMode
from utils import voice
from utils.metrics import track_command
from utils.permissions import is_mod

if TYPE_CHECKING:
    from bot import TootsiesBot

MOOD_OPTIONS = ("chill", "yaps", "off")

log = logging.getLogger(__name__)


async def _load_initial_state(bot: TootsiesBot, guild: discord.Guild) -> dict[str, object]:
    """Load saved settings from the DB. Empty selects if nothing saved yet."""
    saved_settings = await bot.db.all_settings(guild.id)
    saved_mod_roles = await bot.db.get_mod_roles(guild.id)
    saved_discourse_channels = await bot.db.get_discourse_channels(guild.id)
    saved_feed_channels = await bot.db.get_feed_channels(guild.id)
    saved_schedule = await bot.db.get_schedule(guild.id)

    state: dict[str, object] = {}
    if "bot_logs_channel" in saved_settings:
        state["bot_logs_channel"] = int(saved_settings["bot_logs_channel"])
    if saved_discourse_channels:
        state["discourse_channel_ids"] = saved_discourse_channels
    if saved_mod_roles:
        state["mod_role_ids"] = list(saved_mod_roles)
    if saved_feed_channels:
        state["feed_channel_ids"] = [cid for cid, _cat in saved_feed_channels]
    state["mood"] = saved_schedule.mood.value
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
    """Return a default_values entry for a channel ID, only if it still exists."""
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

        self.add_item(_ModRoleSelect(
            self, _safe_role_defaults(guild, prefill.get("mod_role_ids")),
        ))
        self.add_item(_DiscourseSelect(
            self, _safe_channel_defaults(guild, prefill.get("discourse_channel_ids")),
        ))
        self.add_item(_BotLogsSelect(
            self, _safe_channel_default(guild, prefill.get("bot_logs_channel")),
        ))
        self.add_item(_MoodSelect(
            self, str(self.selected.get("mood", "chill")),
        ))
        self.add_item(_FeedSelect(
            self, _safe_channel_defaults(guild, prefill.get("feed_channel_ids")),
        ))
        self._refresh_select_defaults()

    # ---- embed -----------------------------------------------------------------

    def help_embed(self) -> discord.Embed:
        return discord.Embed(
            title="toots' menu",
            description=(
                "pick from each dropdown. saves as you go.\n\n"
                "👮 **mod roles**: who can boss me around\n"
                "💬 **discourse**: where i post + chime in\n"
                "😎 **mood**: chill / yaps / off\n"
                "📊 **bot-logs**: where i post order status + errors\n"
                "📰 **feeds**: read-only sources (optional)"
            ),
            color=0x9b59b6,
        )

    async def _check_actor(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.actor_id:
            await interaction.response.send_message(
                "not your menu, regular.", ephemeral=True,
            )
            return False
        return True

    def _refresh_select_defaults(self) -> None:
        """Sync each select's `default_values` (or `default` per-option for the
        StringSelect mood picker) to current `self.selected` before any
        `edit_message(view=self)`. Without this, re-rendering the view reverts
        dropdowns to their construction-time defaults, visually clearing
        whatever the user just picked."""
        for child in self.children:
            if isinstance(child, _BotLogsSelect):
                cid = self.selected.get("bot_logs_channel")
                child.default_values = _safe_channel_default(self.guild, cid)
                ch = self.guild.get_channel(int(cid)) if isinstance(cid, int) else None
                child.placeholder = (
                    f"{child.LABEL}: #{ch.name}"
                    if isinstance(ch, discord.TextChannel) else child.LABEL
                )
            elif isinstance(child, _DiscourseSelect):
                ids = self.selected.get("discourse_channel_ids")
                child.default_values = _safe_channel_defaults(self.guild, ids)
                child.placeholder = (
                    f"{child.LABEL}: {len(ids)} picked"
                    if isinstance(ids, list) and ids else child.LABEL
                )
            elif isinstance(child, _ModRoleSelect):
                ids = self.selected.get("mod_role_ids")
                child.default_values = _safe_role_defaults(self.guild, ids)
                child.placeholder = (
                    f"{child.LABEL}: {len(ids)} picked"
                    if isinstance(ids, list) and ids else child.LABEL
                )
            elif isinstance(child, _FeedSelect):
                ids = self.selected.get("feed_channel_ids")
                child.default_values = _safe_channel_defaults(self.guild, ids)
                child.placeholder = (
                    f"{child.LABEL}: {len(ids)} picked"
                    if isinstance(ids, list) and ids else child.LABEL
                )
            elif isinstance(child, _MoodSelect):
                # StringSelect uses per-option .default booleans, not
                # default_values (which is for ChannelSelect/RoleSelect).
                current = str(self.selected.get("mood", "chill"))
                for opt in child.options:
                    opt.default = (opt.value == current)
                child.placeholder = f"😎 mood: {current}"

    # ---- autosave -------------------------------------------------------------

    async def autosave(
        self,
        interaction: discord.Interaction,
        key: str,
    ) -> None:
        """Persist the just-changed setting + re-render the summary embed.

        Called from each select's callback after self.selected[key] is updated.
        Each setting writes to a different table, so we route by key. The
        embed is re-rendered so the mod sees the new state immediately, and
        the selects' default_values are refreshed so other selections don't
        visually clear (Discord re-renders the whole view on edit_message).
        """
        if not await self._check_actor(interaction):
            return
        if interaction.guild is None:
            return
        guild_id = interaction.guild.id

        try:
            await self._persist_key(guild_id, key)
        except Exception:
            log.exception("autosave persist failed: key=%s guild=%s", key, guild_id)
            # Don't block the visual update; the value is already in self.selected
            # so a re-select will retry the write.

        # Mark configured once required settings are all present. Idempotent.
        required = ("bot_logs_channel", "discourse_channel_ids", "mod_role_ids")
        if all(self.selected.get(k) for k in required):
            try:
                await self.bot.db.mark_configured(guild_id)
            except Exception:
                log.exception("mark_configured failed for guild=%s", guild_id)

        await self.bot.db.audit(
            guild_id, self.actor_id, f"menu_set_{key}",
            after={key: self.selected.get(key)},
        )

        # Refresh select defaults BEFORE edit_message or other selects will
        # visually clear. Same gotcha that existed back when mood was a button.
        self._refresh_select_defaults()
        await interaction.response.edit_message(
            embed=self._state_embed(), view=self,
        )

    async def _persist_key(self, guild_id: int, key: str) -> None:
        """Route a single just-set value to its storage backend."""
        val = self.selected.get(key)
        if key == "bot_logs_channel":
            if val:
                await self.bot.db.set_setting(
                    guild_id, key, int(cast("int", val)), self.actor_id,
                )
        elif key == "discourse_channel_ids":
            if isinstance(val, list):
                await self.bot.db.set_discourse_channels(
                    guild_id, [int(c) for c in val],
                )
        elif key == "mod_role_ids":
            if isinstance(val, list):
                await self.bot.db.set_mod_roles(
                    guild_id, [int(r) for r in cast("list[int]", val)],
                )
        elif key == "feed_channel_ids":
            if isinstance(val, list):
                await self.bot.db.set_feed_channels(
                    guild_id, [(int(c), None) for c in val],
                )
        elif key == "mood" and isinstance(val, str):
            await self.bot.db.set_schedule(
                guild_id, MoodMode(val), self.actor_id,
            )

    def _state_embed(self) -> discord.Embed:
        """Live state embed shown after each autosave."""
        required = ("bot_logs_channel", "discourse_channel_ids", "mod_role_ids")
        ready = all(self.selected.get(k) for k in required)
        title = "locked in. bar's open." if ready else "saving as you pick."
        color = 0x2ecc71 if ready else 0x9b59b6
        return discord.Embed(
            title=title,
            color=color,
            description=self._summary(),
        )

    def _summary(self) -> str:
        bot_logs = self.selected.get("bot_logs_channel")
        discourse_ids = cast("list[int]", self.selected.get("discourse_channel_ids") or [])
        discourse_label = (
            "_(pick at least one)_" if not discourse_ids
            else ", ".join(f"<#{c}>" for c in discourse_ids)
        )
        mod_roles = self.selected.get("mod_role_ids") or []
        feeds = cast("list[int]", self.selected.get("feed_channel_ids") or [])
        feeds_label = (
            "_(none)_" if not feeds
            else ", ".join(f"<#{c}>" for c in feeds)
        )
        return (
            f"**mod roles:** "
            f"{', '.join(f'<@&{r}>' for r in cast('list[int]', mod_roles)) or '_(pick at least one)_'}\n"
            f"**discourse:** {discourse_label}\n"
            f"**mood:** {self.selected.get('mood', 'chill')}\n"
            f"**bot-logs:** {f'<#{bot_logs}>' if bot_logs else '_(pick one)_'}\n"
            f"**feeds:** {feeds_label}"
        )


# ---- selects (each takes one row) -------------------------------------------------


class _BotLogsSelect(discord.ui.ChannelSelect):
    LABEL = "📊 bot-logs channel"

    def __init__(
        self, parent: MenuView, defaults: list[discord.SelectDefaultValue],
    ) -> None:
        super().__init__(
            placeholder=self.LABEL,
            min_values=1, max_values=1, row=3,
            channel_types=[discord.ChannelType.text],
            default_values=defaults,
        )
        self.parent_view = parent

    async def callback(self, interaction: discord.Interaction) -> None:
        self.parent_view.selected["bot_logs_channel"] = self.values[0].id
        self.placeholder = f"{self.LABEL}: #{self.values[0].name}"
        await self.parent_view.autosave(interaction, "bot_logs_channel")


class _DiscourseSelect(discord.ui.ChannelSelect):
    LABEL = "💬 discourse channels"

    def __init__(
        self, parent: MenuView, defaults: list[discord.SelectDefaultValue],
    ) -> None:
        super().__init__(
            placeholder=self.LABEL,
            min_values=1, max_values=25, row=1,
            channel_types=[discord.ChannelType.text],
            default_values=defaults,
        )
        self.parent_view = parent

    async def callback(self, interaction: discord.Interaction) -> None:
        self.parent_view.selected["discourse_channel_ids"] = [c.id for c in self.values]
        self.placeholder = f"{self.LABEL}: {len(self.values)} picked"
        await self.parent_view.autosave(interaction, "discourse_channel_ids")


class _ModRoleSelect(discord.ui.RoleSelect):
    LABEL = "👮 mod roles"

    def __init__(
        self, parent: MenuView, defaults: list[discord.SelectDefaultValue],
    ) -> None:
        super().__init__(
            placeholder=self.LABEL,
            min_values=1, max_values=10, row=0,
            default_values=defaults,
        )
        self.parent_view = parent

    async def callback(self, interaction: discord.Interaction) -> None:
        self.parent_view.selected["mod_role_ids"] = [r.id for r in self.values]
        self.placeholder = f"{self.LABEL}: {len(self.values)} picked"
        await self.parent_view.autosave(interaction, "mod_role_ids")


class _FeedSelect(discord.ui.ChannelSelect):
    LABEL = "📰 feed channels (optional)"

    def __init__(
        self, parent: MenuView, defaults: list[discord.SelectDefaultValue],
    ) -> None:
        super().__init__(
            placeholder=self.LABEL,
            min_values=0, max_values=25, row=4,
            channel_types=[discord.ChannelType.text],
            default_values=defaults,
        )
        self.parent_view = parent

    async def callback(self, interaction: discord.Interaction) -> None:
        self.parent_view.selected["feed_channel_ids"] = [c.id for c in self.values]
        self.placeholder = (
            f"{self.LABEL}: {len(self.values)} picked" if self.values else self.LABEL
        )
        await self.parent_view.autosave(interaction, "feed_channel_ids")


class _MoodSelect(discord.ui.Select):
    """Mood select on its own row. Replaces the cycling button so the option
    list is visible up front instead of hidden behind clicks. Auto-saves like
    the other selects."""

    def __init__(self, parent: MenuView, current_mood: str) -> None:
        super().__init__(
            placeholder=f"😎 mood: {current_mood}",
            min_values=1, max_values=1, row=2,
            options=[
                discord.SelectOption(
                    label="chill", value="chill",
                    description="2 scheduled posts/day, up to 5 chime-ins/day",
                    default=(current_mood == "chill"),
                ),
                discord.SelectOption(
                    label="yaps", value="yaps",
                    description="5 scheduled posts/day, up to 10 chime-ins/day",
                    default=(current_mood == "yaps"),
                ),
                discord.SelectOption(
                    label="off", value="off",
                    description="silent on both scheduled posts and chime-in",
                    default=(current_mood == "off"),
                ),
            ],
        )
        self.parent_view = parent

    async def callback(self, interaction: discord.Interaction) -> None:
        self.parent_view.selected["mood"] = self.values[0]
        # Update placeholder so the live state reads correctly when the embed
        # re-renders (placeholder is what shows when nothing is "actively
        # selected" in this turn, which happens after edit_message).
        self.placeholder = f"😎 mood: {self.values[0]}"
        await self.parent_view.autosave(interaction, "mood")


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Settings(cast("TootsiesBot", bot)))
