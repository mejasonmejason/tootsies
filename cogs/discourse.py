"""/discourse — manual posts AND scheduled-posting control.

Two modes on the same root command:

  /discourse category:<pop|sports|cinema|hiphop|nba|custom>
      Manual post. Toots drops a discourse starter into the channel where the command was run.
      Pulls from configured feed channels + current channel's last hour + web. Counts against
      the per-server daily limit.

  /discourse mood:<chill|yaps|off|status>
      Controls the scheduled poster (posts land in the configured discourse channel, not the
      invoked one). chill = 2/day, yaps = 4/day, off = silent. Unlimited.

The scheduler tick lives here too — when the cog is loaded, a tasks.loop runs every minute,
checks each configured guild's schedule, and posts (or skips cleanly) as appropriate.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, time, timedelta
from typing import TYPE_CHECKING, cast
from zoneinfo import ZoneInfo

import discord
from discord import app_commands
from discord.ext import commands, tasks

from models import MoodMode
from utils import voice
from utils.feeds import format_for_prompt, recent_messages
from utils.gates import require_configured
from utils.metrics import track_command
from utils.permissions import can_send_in
from utils.rate_limits import check_server_limit, consume_server

if TYPE_CHECKING:
    from bot import TootsiesBot

log = logging.getLogger(__name__)

CATEGORIES = ["pop", "sports", "cinema", "hiphop", "nba", "custom"]

PT = ZoneInfo("America/Los_Angeles")
CHILL_TIMES = [time(12, 0), time(19, 0)]
YAPS_TIMES = [time(10, 0), time(14, 0), time(18, 0), time(22, 0)]


class Discourse(commands.Cog):
    def __init__(self, bot: TootsiesBot) -> None:
        self.bot = bot
        self.scheduler_tick.start()

    async def cog_unload(self) -> None:
        self.scheduler_tick.cancel()

    # ---- /discourse -------------------------------------------------------------

    @app_commands.command(
        name="discourse",
        description="start a fight (manual) or set the schedule (mood).",
    )
    @app_commands.describe(
        category="post one starter now in this channel.",
        mood="control scheduled posting in the discourse channel.",
    )
    @app_commands.choices(
        category=[app_commands.Choice(name=c, value=c) for c in CATEGORIES],
        mood=[
            app_commands.Choice(name="chill (2/day)", value="chill"),
            app_commands.Choice(name="yaps (4/day)", value="yaps"),
            app_commands.Choice(name="off", value="off"),
            app_commands.Choice(name="status", value="status"),
        ],
    )
    @track_command("discourse")
    async def discourse(
        self,
        interaction: discord.Interaction,
        category: app_commands.Choice[str] | None = None,
        mood: app_commands.Choice[str] | None = None,
    ) -> None:
        if not await require_configured(interaction, self.bot.db):
            return
        if category and mood:
            await interaction.response.send_message(
                "one or the other, regular. `category:` to post or `mood:` to set the schedule.",
                ephemeral=True,
            )
            return
        if mood:
            await self._handle_mood(interaction, mood.value)
            return
        if category:
            await self._handle_manual_post(interaction, category.value)
            return
        await interaction.response.send_message(
            "what'll it be? `/discourse category:<c>` to post or `/discourse mood:<x>` to set the schedule.",
            ephemeral=True,
        )

    # ---- mood handler -----------------------------------------------------------

    async def _handle_mood(self, interaction: discord.Interaction, mode_value: str) -> None:
        assert interaction.guild_id is not None
        guild_id = interaction.guild_id

        if mode_value == "status":
            state = await self.bot.db.get_schedule(guild_id)
            await interaction.response.send_message(
                f"mood: **{state.mood.value}** · {state.posts_today} post(s) today",
                ephemeral=True,
            )
            return

        new_mode = MoodMode(mode_value)
        await self.bot.db.set_schedule(guild_id, new_mode, interaction.user.id)
        await self.bot.db.audit(
            guild_id, interaction.user.id, "schedule_set",
            after={"mode": new_mode.value},
        )
        await interaction.response.send_message(f"mood: **{new_mode.value}**.")

    # ---- manual post handler ----------------------------------------------------

    async def _handle_manual_post(
        self, interaction: discord.Interaction, category: str
    ) -> None:
        assert interaction.guild_id is not None
        guild_id = interaction.guild_id

        try:
            allowed, _, _ = await check_server_limit(self.bot.db, guild_id, "discourse")
        except Exception:
            log.exception("rate check failed; failing open")
            allowed = True
        if not allowed:
            await interaction.response.send_message(
                voice.pick(voice.RATE_LIMIT_HIT), ephemeral=True
            )
            return

        await interaction.response.defer(thinking=True)
        try:
            line = await self._compose(interaction, category)
        except Exception:
            log.exception("discourse compose failed")
            await interaction.followup.send(voice.pick(voice.DB_ERROR))
            return

        try:
            await consume_server(self.bot.db, guild_id, "discourse")
            await self.bot.db.add_discourse(guild_id, category, line[:200])
        except Exception:
            log.exception("post-discourse bookkeeping failed")

        await interaction.followup.send(line)

    async def _compose(self, interaction: discord.Interaction, category: str) -> str:
        guild = interaction.guild
        assert guild is not None
        me = guild.me
        assert me is not None

        sources: list[str] = []

        # 1. Configured feed channels (category-filtered, or all if 'custom').
        feed_cat = None if category == "custom" else category
        feeds = await self.bot.db.get_feed_channels(guild.id, feed_cat)
        for channel_id, _cat in feeds[:5]:
            ch = guild.get_channel(channel_id)
            if isinstance(ch, discord.TextChannel):
                # Feed channels are bot/webhook-posted; include_bots=True or we read nothing.
                msgs = await recent_messages(
                    ch, me, limit=10, within=timedelta(hours=24), include_bots=True
                )
                if msgs:
                    sources.append(f"--- #{ch.name} (feed) ---\n{format_for_prompt(msgs)}")

        # 2. Current channel's last hour.
        if isinstance(interaction.channel, discord.TextChannel | discord.Thread):
            local = await recent_messages(
                interaction.channel, me, limit=20, within=timedelta(hours=1)
            )
            if local:
                sources.append(
                    f"--- #{interaction.channel.name} (last hour) ---\n{format_for_prompt(local)}"
                )

        # State-aware dedup: timestamped recent topics for this category. Manual /discourse
        # must always post (the user explicitly asked), so must_post=True instructs Claude to
        # pick a different angle if the obvious topic is stale.
        recent = await self.bot.db.recent_discourse(guild.id, category, limit=10)
        recent_blob = (
            "\n".join(f"- [{ts.isoformat(timespec='minutes')}] {topic}" for topic, ts in recent)
            if recent
            else ""
        )

        sources_blob = "\n\n".join(sources) if sources else "(no local sources, use web search)"
        line = await self.bot.claude.discourse(
            category, sources_blob, recent_with_timestamps=recent_blob, must_post=True,
        )

        if not line or line.strip().upper() == "EMPTY":
            return voice.pick(voice.DISCOURSE_FALLBACK)
        return line

    # ---- scheduler --------------------------------------------------------------

    @tasks.loop(minutes=1)
    async def scheduler_tick(self) -> None:
        try:
            now_pt = datetime.now(PT)
            for guild_id in await self.bot.db.all_configured_guilds():
                await self._maybe_scheduled_post(guild_id, now_pt)
        except Exception:
            log.exception("discourse scheduler tick failed")

    @scheduler_tick.before_loop
    async def before_tick(self) -> None:
        await self.bot.wait_until_ready()

    async def _maybe_scheduled_post(self, guild_id: int, now_pt: datetime) -> None:
        state = await self.bot.db.get_schedule(guild_id)
        if state.mood == MoodMode.OFF:
            return
        schedule = CHILL_TIMES if state.mood == MoodMode.CHILL else YAPS_TIMES

        # Pick the most recent scheduled slot whose time has passed today.
        current = now_pt.time().replace(second=0, microsecond=0)
        due = [t for t in schedule if t <= current]
        if not due:
            return
        expected = len(due)

        # If we've already consumed today's elapsed slots (whether or not we actually posted),
        # skip until the next slot.
        today_utc = now_pt.astimezone(UTC).date()
        if state.last_post_at is not None:
            last_pt = state.last_post_at.astimezone(PT)
            if last_pt.date() == now_pt.date() and state.posts_today >= expected:
                return

        guild = self.bot.get_guild(guild_id)
        if guild is None:
            return
        channel_id = await self.bot.db.get_setting(guild_id, "discourse_channel")
        if not channel_id:
            return
        channel = guild.get_channel(int(channel_id))
        if not isinstance(channel, discord.TextChannel):
            return
        me = guild.me
        if me is None or not can_send_in(channel, me):
            return

        # Cross-category dedup — scheduled posts span all topics, so we look at the whole
        # 72h history and let Claude decide if anything fresh exists.
        recent = await self.bot.db.recent_discourse_all(guild_id, limit=20)
        recent_blob = "\n".join(
            f"- [{ts.isoformat(timespec='minutes')}] ({cat}) {topic}"
            for cat, topic, ts in recent
        )

        try:
            line = await self.bot.claude.mood_post(recent_with_timestamps=recent_blob)
        except Exception:
            log.exception("scheduled post claude call failed")
            line = voice.pick(voice.DISCOURSE_FALLBACK)

        # Consume the slot regardless of whether we post — otherwise an EMPTY at 12:00 would
        # keep retrying every minute. Skipping cleanly means trying again at 19:00 (chill) or
        # the next yap slot.
        await self.bot.db.record_schedule_post(guild_id, today_utc)

        if not line or line.strip().upper() == "EMPTY":
            log.info("scheduled slot skipped for guild %d — nothing fresh", guild_id)
            return

        try:
            await channel.send(line)
            await self.bot.db.add_discourse(guild_id, "scheduled", line[:200])
        except discord.DiscordException:
            log.exception("scheduled post send failed")


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Discourse(cast("TootsiesBot", bot)))
