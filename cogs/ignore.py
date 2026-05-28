"""/ignore: mod-only ignore-list management via a UserSelect view.

Single ephemeral menu (modeled on the mod-roles selector in /menu). The
dropdown is pre-populated with the currently silenced users; mods add or
remove users by editing the selection and the change autosaves.

State lives in `abuse_violations` (see db.py). The Haiku-driven path in
cogs/ask.py writes to the same table when classifying messages as abuse.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, cast

import discord
from discord import app_commands
from discord.ext import commands

from utils import abuse_tracker, voice
from utils.events import emit
from utils.gates import require_configured
from utils.metrics import track_command
from utils.permissions import is_mod

if TYPE_CHECKING:
    from bot import TootsiesBot

log = logging.getLogger(__name__)


class Ignore(commands.Cog):
    def __init__(self, bot: TootsiesBot) -> None:
        self.bot = bot

    @app_commands.command(name="ignore", description="mod-only: pick who toots ignores.")
    @track_command("ignore")
    async def ignore(self, interaction: discord.Interaction) -> None:
        if not await self._mod_gate(interaction):
            return
        assert interaction.guild is not None
        guild = interaction.guild
        rows = await self.bot.db.list_silenced(guild.id)
        current_ids = [user_id for user_id, _v, _at in rows]
        defaults = [
            discord.SelectDefaultValue(id=uid, type=discord.SelectDefaultValueType.user)
            for uid in current_ids
        ]
        view = _IgnoreView(self.bot, guild.id, current_ids, defaults)
        await interaction.response.send_message(
            _render_summary(guild, rows),
            view=view,
            ephemeral=True,
        )

    async def _mod_gate(self, interaction: discord.Interaction) -> bool:
        if not await require_configured(interaction, self.bot.db):
            return False
        member = interaction.user
        if not isinstance(member, discord.Member):
            await interaction.response.send_message(voice.pick(voice.PERMISSION_DENIED), ephemeral=True)
            return False
        if not await is_mod(self.bot.db, member):
            await interaction.response.send_message(voice.pick(voice.PERMISSION_DENIED), ephemeral=True)
            return False
        return True


def _render_summary(
    guild: discord.Guild, rows: list[tuple[int, int, object]],
) -> str:
    if not rows:
        return "**ignore list** (empty)\n\npick members below to ignore."
    lines = []
    for user_id, _violations, _at in rows:
        member = guild.get_member(user_id)
        name = member.display_name if member is not None else f"<unknown {user_id}>"
        lines.append(f"  - {name}")
    body = "\n".join(lines)
    return (
        f"**ignore list** ({len(rows)})\n{body}\n\n"
        "edit the selection to add or remove."
    )


class _IgnoreView(discord.ui.View):
    def __init__(
        self,
        bot: TootsiesBot,
        guild_id: int,
        current_ids: list[int],
        defaults: list[discord.SelectDefaultValue],
    ) -> None:
        super().__init__(timeout=300)
        self.bot = bot
        self.guild_id = guild_id
        self.current_ids = set(current_ids)
        self.add_item(_IgnoreUserSelect(self, defaults))


class _IgnoreUserSelect(discord.ui.UserSelect):
    def __init__(
        self,
        parent: _IgnoreView,
        defaults: list[discord.SelectDefaultValue],
    ) -> None:
        super().__init__(
            placeholder="👁️ ignored members",
            min_values=0,
            max_values=25,
            default_values=defaults,
        )
        self.parent_view = parent

    async def callback(self, interaction: discord.Interaction) -> None:
        guild = interaction.guild
        assert guild is not None
        selected_ids = {u.id for u in self.values}
        added = selected_ids - self.parent_view.current_ids
        removed = self.parent_view.current_ids - selected_ids

        for uid in added:
            user = guild.get_member(uid)
            if user is not None and (user.bot or uid == interaction.user.id):
                # quietly skip self / bots; they'll be reflected as "not added"
                # in the next render but don't error out the whole save.
                continue
            await self.parent_view.bot.db.manually_silence_user(
                guild.id, uid, abuse_tracker.ABUSE_THRESHOLD,
            )
            await self.parent_view.bot.db.audit(
                guild.id, interaction.user.id, "ignore_add", target=str(uid),
            )
            emit(
                "abuse_silenced", guild_id=guild.id, user_id=uid,
                violations=abuse_tracker.ABUSE_THRESHOLD, manual=True,
            )

        for uid in removed:
            was_silenced = await self.parent_view.bot.db.lift_silence(
                guild.id, uid, lifted_by=interaction.user.id,
            )
            if was_silenced:
                await self.parent_view.bot.db.audit(
                    guild.id, interaction.user.id, "ignore_lift", target=str(uid),
                )
                emit("abuse_lifted", guild_id=guild.id, user_id=uid)

        # Refresh local state + re-render the summary text in place.
        rows = await self.parent_view.bot.db.list_silenced(guild.id)
        self.parent_view.current_ids = {uid for uid, _v, _at in rows}
        await interaction.response.edit_message(
            content=_render_summary(guild, rows),
            view=self.parent_view,
        )


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Ignore(cast("TootsiesBot", bot)))
