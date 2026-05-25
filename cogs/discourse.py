"""/discourse, the manual discussion-starter command, plus the quiet-detection poster.

  /discourse category:<pop|sports|cinema|hiphop|nba|custom>
      Manual post. Toots drops a discourse starter into the channel where
      the command was run. Pulls from configured feed channels + current
      channel's last hour + web. Counts against the per-server daily limit.

Schedule control (chill / yaps / off) lives in /menu, not here. The scheduler
tick runs every minute and has two triggers:
  1. Lunchtime post (noon ET): one guaranteed daily conversation starter.
  2. Quiet-detection: when the conversation has gone stale (last human
     message >15min ago AND fewer than 3 humans in the threshold window),
     Toots drops a new topic. Threshold is mood-tuned (60min yaps / 120min
     chill). Active hours only (9am-2am ET).
"""

from __future__ import annotations

import logging
from datetime import datetime, time, timedelta
from typing import TYPE_CHECKING, cast
from zoneinfo import ZoneInfo

import discord
from discord import app_commands
from discord.ext import commands, tasks

from models import MoodMode
from utils import bot_logs, voice
from utils.events import emit, emit_error
from utils.feeds import (
    format_for_prompt,
    hot_urls,
    recent_image_urls,
    recent_messages,
)
from utils.gates import require_configured
from utils.link_enrich import enrich_batch
from utils.metrics import track_command
from utils.permissions import can_send_in
from utils.rate_limits import check_server_limit, consume_server

if TYPE_CHECKING:
    from bot import TootsiesBot

log = logging.getLogger(__name__)

CATEGORIES = ["pop", "sports", "cinema", "hiphop", "nba"]

ET = ZoneInfo("America/New_York")

# Lunchtime post: one guaranteed daily conversation starter.
LUNCH_HOUR = time(12, 0)

# Quiet-detection: post when the conversation has gone stale.
# "Stale" = last human message >15min ago AND fewer than 3 human messages
# in the threshold window. This avoids interrupting slow-but-alive threads.
QUIET_THRESHOLD = {
    MoodMode.CHILL: timedelta(minutes=120),
    MoodMode.YAPS: timedelta(minutes=60),
}
STALE_FLOOR = timedelta(minutes=15)
QUIET_MSG_CAP = 3

# Daily caps per mood (lunch + quiet-detection combined).
DAILY_CAP = {
    MoodMode.CHILL: 3,
    MoodMode.YAPS: 5,
}

# Active hours (ET): 9am through 2am next day. Outside this window, no posts.
ACTIVE_START = time(9, 0)
ACTIVE_END = time(2, 0)

# Minimum cooldown between post attempts in the same channel (prevents
# hammering Claude every minute while a channel stays quiet).
ATTEMPT_COOLDOWN = timedelta(minutes=45)


def _in_active_hours(t: time) -> bool:
    """True if `t` is between 9am and 2am ET (wraps midnight)."""
    return t >= ACTIVE_START or t <= ACTIVE_END


async def _channel_is_stale(
    channel: discord.TextChannel, threshold: timedelta,
) -> bool:
    """True if the conversation has gone stale.

    Stale means BOTH:
      1. The most recent human message is older than STALE_FLOOR (15min).
      2. Fewer than QUIET_MSG_CAP (3) human messages in the threshold window.

    This avoids posting into a slow-but-alive conversation (two people
    chatting every 20 minutes) while still catching genuinely dead channels.
    """
    now = discord.utils.utcnow()
    cutoff = now - threshold
    human_count = 0
    newest_human: datetime | None = None
    async for msg in channel.history(limit=30, after=cutoff):
        if msg.author.bot:
            continue
        human_count += 1
        if newest_human is None or msg.created_at > newest_human:
            newest_human = msg.created_at
        if human_count >= QUIET_MSG_CAP:
            return False
    return newest_human is None or (now - newest_human) >= STALE_FLOOR


class Discourse(commands.Cog):
    def __init__(self, bot: TootsiesBot) -> None:
        self.bot = bot
        self._last_attempt: dict[tuple[int, int], datetime] = {}
        self.scheduler_tick.start()

    async def cog_unload(self) -> None:
        self.scheduler_tick.cancel()

    # ---- /discourse -------------------------------------------------------------

    @app_commands.command(
        name="discourse",
        description="drop a discussion starter into this channel.",
    )
    @app_commands.describe(
        category="optional filter. omit to let toots read the room.",
    )
    @app_commands.choices(
        category=[app_commands.Choice(name=c, value=c) for c in CATEGORIES],
    )
    @track_command("discourse")
    async def discourse(
        self,
        interaction: discord.Interaction,
        category: app_commands.Choice[str] | None = None,
    ) -> None:
        """Post one discourse starter now in the invoked channel.

        Schedule control (chill/yaps/off) is in `/menu`, not here, keeps the
        slash command focused on the one thing everyone needs.
        """
        if not await require_configured(interaction, self.bot.db):
            return
        await self._handle_manual_post(
            interaction, category.value if category else None,
        )

    # ---- manual post handler ----------------------------------------------------

    async def _handle_manual_post(
        self, interaction: discord.Interaction, category: str | None
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

        guild = interaction.guild
        assert guild is not None
        channel = interaction.channel
        if not isinstance(channel, discord.TextChannel | discord.Thread):
            await interaction.followup.send(voice.pick(voice.DB_ERROR))
            return

        try:
            line = await self._compose(
                guild, channel, category=category, must_post=True,
                user_id=interaction.user.id,
            )
        except Exception as exc:
            log.exception("discourse compose failed")
            emit_error(
                source="discourse", exc=exc, recoverable=False,
                guild_id=guild_id, user_id=interaction.user.id, category=category,
            )
            await bot_logs.maybe_post_db_error(
                self.bot, self.bot.db, guild_id, exc,
                source="discourse", user_id=interaction.user.id,
                verbosity=self.bot.config.bot_logs_verbosity,
            )
            await bot_logs.maybe_post_prompt_error(
                self.bot, self.bot.db, guild_id, exc,
                source="discourse", user_id=interaction.user.id,
                verbosity=self.bot.config.bot_logs_verbosity,
            )
            await interaction.followup.send(voice.pick(voice.DB_ERROR))
            return

        try:
            await consume_server(self.bot.db, guild_id, "discourse")
            await self.bot.db.add_discourse(guild_id, category or "open", line[:200])
        except Exception:
            log.exception("post-discourse bookkeeping failed")

        await interaction.followup.send(line)

    # ---- shared compose pipeline ------------------------------------------------

    async def _compose(
        self,
        guild: discord.Guild,
        channel: discord.TextChannel | discord.Thread,
        *,
        category: str | None = None,
        must_post: bool = True,
        user_id: int | None = None,
    ) -> str:
        """Build a discourse post from feed channels + channel context + web.

        Used by both the manual /discourse command and the scheduler.
        category=None means "read the room": all feeds are pulled and
        Claude infers the topic from the channel context. An explicit
        category filters to matching feed channels only.
        """
        me = guild.me
        assert me is not None

        sources: list[str] = []
        all_feed_msgs: list[discord.Message] = []

        feed_cat = category
        feeds = await self.bot.db.get_feed_channels(guild.id, feed_cat)
        for feed_channel_id, _cat in feeds[:5]:
            ch = guild.get_channel(feed_channel_id)
            if isinstance(ch, discord.TextChannel):
                msgs = await recent_messages(
                    ch, me, limit=10, within=timedelta(hours=24), include_bots=True,
                )
                if msgs:
                    sources.append(
                        f"--- #{ch.name} (feed) ---\n"
                        f"{format_for_prompt(msgs, include_reactions=True)}"
                    )
                    all_feed_msgs.extend(msgs)

        local = await recent_messages(
            channel, me, limit=200, within=timedelta(hours=1),
        )
        if local:
            sources.append(
                f"--- #{channel.name} (recent) ---\n"
                f"{format_for_prompt(local, include_reactions=True)}"
            )
            all_feed_msgs.extend(local)

        image_urls = recent_image_urls(all_feed_msgs, limit=8)
        feed_hot_urls = hot_urls(all_feed_msgs, limit=8)
        enriched_map = await enrich_batch([u for u, _, _, _ in feed_hot_urls])
        enriched = [v for v in enriched_map.values() if v is not None]

        recent_all = await self.bot.db.recent_discourse_all(guild.id, limit=20)
        recent_count = len(recent_all)
        recent_blob = "\n".join(
            f"- [{ts.isoformat(timespec='minutes')}] ({cat}) {topic}"
            for cat, topic, ts in recent_all
        )

        sources_blob = "\n\n".join(sources) if sources else "(no local sources, use web search)"
        line = await self.bot.claude.discourse(
            category, sources_blob, recent_with_timestamps=recent_blob,
            channel_name=channel.name, must_post=must_post,
            image_urls=image_urls, hot_urls=feed_hot_urls,
            enriched_links=enriched,
        )

        if not line or line.strip().upper() == "EMPTY":
            emit(
                "discourse_fallback",
                guild_id=guild.id, user_id=user_id, category=category,
                source_count=len(sources), local_source_chars=len(sources_blob),
                recent_topic_count=recent_count,
                reason="claude_returned_empty" if line else "claude_returned_blank",
            )
            await bot_logs.post(
                self.bot, self.bot.db, guild.id,
                f"💬 discourse fell back to a quip in <#{channel.id}>: "
                f"category=`{category}`, sources={len(sources)}, "
                f"recent_topics={recent_count}, reason=`empty_claude_response`.",
                level="full", verbosity=self.bot.config.bot_logs_verbosity,
            )
            if must_post:
                return voice.pick(voice.DISCOURSE_FALLBACK)
            return ""
        return line

    # ---- scheduler (quiet-detection) ----------------------------------------------

    @tasks.loop(minutes=1)
    async def scheduler_tick(self) -> None:
        try:
            now_et = datetime.now(ET)
            for guild_id in await self.bot.db.all_configured_guilds():
                await self._maybe_scheduled_post(guild_id, now_et)
        except Exception:
            log.exception("discourse scheduler tick failed")

    @scheduler_tick.before_loop
    async def before_tick(self) -> None:
        await self.bot.wait_until_ready()

    async def _maybe_scheduled_post(self, guild_id: int, now_et: datetime) -> None:
        state = await self.bot.db.get_schedule(guild_id)
        if state.mood == MoodMode.OFF:
            return
        if not _in_active_hours(now_et.time()):
            return

        guild = self.bot.get_guild(guild_id)
        if guild is None:
            return

        channel_ids = await self.bot.db.get_discourse_channels(guild_id)
        if not channel_ids:
            return

        for channel_id in channel_ids:
            await self._maybe_post_to_channel(
                guild, channel_id, state.mood, now_et,
            )

    async def _maybe_post_to_channel(
        self,
        guild: discord.Guild,
        channel_id: int,
        mood: MoodMode,
        now_et: datetime,
    ) -> None:
        today = now_et.date()
        posts_today, last_post_at, posts_day = await self.bot.db.get_channel_slot(
            guild.id, channel_id,
        )
        if posts_day != today:
            posts_today = 0

        if posts_today >= DAILY_CAP[mood]:
            return

        # In-memory cooldown: don't re-attempt within ATTEMPT_COOLDOWN.
        key = (guild.id, channel_id)
        last_attempt = self._last_attempt.get(key)
        if last_attempt is not None and (now_et - last_attempt) < ATTEMPT_COOLDOWN:
            return

        channel = guild.get_channel(channel_id)
        if not isinstance(channel, discord.TextChannel):
            return
        me = guild.me
        if me is None or not can_send_in(channel, me):
            return

        # Two triggers: lunchtime post OR quiet-detection.
        lunch = posts_today == 0 and now_et.time() >= LUNCH_HOUR
        stale = await _channel_is_stale(channel, QUIET_THRESHOLD[mood])

        if not lunch and not stale:
            return

        self._last_attempt[key] = now_et

        try:
            line = await self._compose(
                guild, channel, must_post=lunch,
            )
        except Exception:
            log.exception("scheduled post compose failed for channel %s", channel_id)
            line = ""

        if not line or line.strip().upper() == "EMPTY":
            log.info(
                "quiet-detection skipped for guild %d channel %d, nothing fresh",
                guild.id, channel_id,
            )
            return

        await self.bot.db.record_channel_slot(guild.id, channel_id, today)
        await self.bot.db.record_schedule_post(guild.id, today)

        try:
            await channel.send(line)
            await self.bot.db.add_discourse(guild.id, "open", line[:200])
        except discord.DiscordException:
            log.exception("scheduled post send failed")


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Discourse(cast("TootsiesBot", bot)))
