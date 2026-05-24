"""/recap period:[1h|today] — channel-level summary in Toots voice."""

from __future__ import annotations

import logging
from datetime import UTC, datetime, time, timedelta
from typing import TYPE_CHECKING, cast

import discord
from discord import app_commands
from discord.ext import commands

from utils import voice
from utils.feeds import format_for_prompt, is_channel_dead, recent_messages
from utils.gates import require_configured

if TYPE_CHECKING:
    from bot import TootsiesBot
from utils.rate_limits import check_user_limit, consume_user

log = logging.getLogger(__name__)


class Recap(commands.Cog):
    def __init__(self, bot: TootsiesBot) -> None:
        self.bot = bot

    @app_commands.command(name="recap", description="what'd i miss in this channel?")
    @app_commands.describe(period="how far back?")
    @app_commands.choices(
        period=[
            app_commands.Choice(name="last hour", value="1h"),
            app_commands.Choice(name="today", value="today"),
        ]
    )
    async def recap(
        self,
        interaction: discord.Interaction,
        period: app_commands.Choice[str],
    ) -> None:
        if not await require_configured(interaction, self.bot.db):
            return
        assert interaction.guild_id is not None
        user_id = interaction.user.id
        guild_id = interaction.guild_id

        try:
            allowed, _, _ = await check_user_limit(self.bot.db, user_id, guild_id, "recap")
        except Exception:
            log.exception("rate check failed; failing open")
            allowed = True
        if not allowed:
            await interaction.response.send_message(
                voice.pick(voice.RATE_LIMIT_HIT), ephemeral=True
            )
            return

        channel = interaction.channel
        if not isinstance(channel, discord.TextChannel | discord.Thread):
            await interaction.response.send_message(
                voice.pick(voice.PERMISSION_DENIED), ephemeral=True
            )
            return
        me = interaction.guild.me if interaction.guild else None
        if me is None:
            await interaction.response.send_message(
                voice.pick(voice.PERMISSION_DENIED), ephemeral=True
            )
            return

        within = _period_to_window(period.value)
        # /recap looks at more history than /ask — up to 200 over the period.
        msgs = await recent_messages(channel, me, limit=200, within=within)

        await interaction.response.defer(thinking=True)
        try:
            if is_channel_dead(msgs):
                line = voice.pick(voice.CHANNEL_DEAD)
            else:
                blob = format_for_prompt(msgs, include_reactions=True)
                line = await self.bot.claude.recap(channel.name, blob)
        except Exception:
            log.exception("recap failed")
            await interaction.followup.send(voice.pick(voice.DB_ERROR))
            return

        try:
            await consume_user(self.bot.db, user_id, guild_id, "recap")
        except Exception:
            log.exception("consume failed")

        await interaction.followup.send(line)


def _period_to_window(period: str) -> timedelta:
    if period == "1h":
        return timedelta(hours=1)
    # "today" = since midnight UTC. Close enough — exact TZ doesn't matter for vibes.
    now = datetime.now(UTC)
    today_start = datetime.combine(now.date(), time.min, tzinfo=UTC)
    return now - today_start


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Recap(cast("TootsiesBot", bot)))
