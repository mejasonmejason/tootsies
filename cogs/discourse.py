"""/discourse, the manual discussion-starter command, plus the scheduled poster.

  /discourse category:<pop|sports|cinema|hiphop|nba|custom>
      Manual post. Toots drops a discourse starter into the channel where
      the command was run. Pulls from configured feed channels + current
      channel's last hour + web. Counts against the per-server daily limit.

Schedule control (chill / yaps / off) lives in /menu, not here. The scheduler
tick runs every minute, checks each configured guild's mood, and posts (or
skips cleanly) according to the configured cadence in US Eastern time (Miami).
Each configured discourse channel gets its own independent slot tracking.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import date, datetime, time, timedelta
from typing import TYPE_CHECKING, Any, cast
from zoneinfo import ZoneInfo

import anthropic
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
from utils.perplexity import build_search_query
from utils.rate_limits import check_server_limit, consume_server

if TYPE_CHECKING:
    from bot import TootsiesBot

log = logging.getLogger(__name__)

CATEGORIES = ["pop", "sports", "cinema", "hiphop", "nba"]

ET = ZoneInfo("America/New_York")
CHILL_TIMES = [time(12, 0), time(19, 0)]
YAPS_TIMES = [time(10, 0), time(14, 0), time(18, 0), time(22, 0)]

# Gap between per-channel compose calls inside a single scheduler tick.
# Each compose burns ~10K input tokens on sonnet-4-6 and the org TPM ceiling
# is 30K/min, so bursting >3 channels in <60s reliably trips 429.
_SCHEDULED_CHANNEL_GAP_SECONDS = 15

# Upper bound on how long we'll honor a retry-after on a scheduled compose
# 429. TPM rolling windows max out at 60s; 65 gives a small cushion without
# letting a misbehaving header pin a tick for minutes.
_RATE_LIMIT_MAX_RETRY_WAIT_SECONDS = 65.0


def _parse_retry_after_seconds(exc: anthropic.RateLimitError) -> float | None:
    """Pull the retry-after header (seconds) off a RateLimitError, if present."""
    response = getattr(exc, "response", None)
    headers = getattr(response, "headers", None) if response is not None else None
    if headers is None:
        return None
    raw = headers.get("retry-after")
    if not raw:
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


class Discourse(commands.Cog):
    def __init__(self, bot: TootsiesBot) -> None:
        self.bot = bot
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
                    ch, me, limit=50, within=timedelta(hours=24), include_bots=True,
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

        # Run link enrichment, Perplexity search, and DB history fetch in parallel.
        # return_exceptions=True so a Perplexity outage can't cancel the others.
        coros: list[Any] = [enrich_batch([u for u, _, _, _ in feed_hot_urls])]
        pplx_idx = -1
        pplx = self.bot.perplexity
        if pplx:
            pplx_idx = len(coros)
            coros.append(pplx.search(
                build_search_query(
                    "", surface="discourse",
                    category=category, channel_name=channel.name,
                ),
                purpose="discourse",
            ))
        db_idx = len(coros)
        coros.append(self.bot.db.recent_discourse_all(guild.id, limit=20))

        raw = await asyncio.gather(*coros, return_exceptions=True)
        enriched_map = raw[0] if not isinstance(raw[0], BaseException) else {}
        pplx_result: str | None = (
            raw[pplx_idx]  # type: ignore[assignment]
            if pplx_idx >= 0 and not isinstance(raw[pplx_idx], BaseException) else None
        )
        recent_all: list[Any] = (
            raw[db_idx] if not isinstance(raw[db_idx], BaseException) else []  # type: ignore[assignment]
        )

        enriched = [v for v in enriched_map.values() if v is not None]
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
            perplexity_context=pplx_result,
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

    # ---- scheduler --------------------------------------------------------------

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
        schedule = CHILL_TIMES if state.mood == MoodMode.CHILL else YAPS_TIMES

        current = now_et.time().replace(second=0, microsecond=0)
        due = [t for t in schedule if t <= current]
        if not due:
            return
        expected = len(due)
        today_et = now_et.date()

        guild = self.bot.get_guild(guild_id)
        if guild is None:
            return

        channel_ids = await self.bot.db.get_discourse_channels(guild_id)
        if not channel_ids:
            return

        for i, channel_id in enumerate(channel_ids):
            if i > 0:
                await asyncio.sleep(_SCHEDULED_CHANNEL_GAP_SECONDS)
            await self._maybe_post_to_channel(
                guild, channel_id, expected, today_et,
            )

    async def _compose_with_retry(
        self,
        guild: discord.Guild,
        channel: discord.TextChannel | discord.Thread,
        channel_id: int,
    ) -> str:
        """Compose, honoring a single retry-after on RateLimitError.

        Scheduled posts have no user waiting, so when Anthropic tells us
        "wait N seconds", it's strictly better to wait and post late than
        to skip the slot entirely. One retry only, capped at 65s.
        """
        try:
            return await self._compose(guild, channel, must_post=False)
        except anthropic.RateLimitError as exc:
            retry_after = _parse_retry_after_seconds(exc)
            if retry_after is None:
                log.info(
                    "scheduled compose 429 for channel %s, no retry-after; skipping slot",
                    channel_id,
                )
                emit_error(
                    source="discourse_scheduled", exc=exc, recoverable=True,
                    guild_id=guild.id, channel_id=channel_id,
                )
                return ""
            wait = min(retry_after, _RATE_LIMIT_MAX_RETRY_WAIT_SECONDS)
            log.info(
                "scheduled compose 429 for channel %s, retrying in %.1fs (anthropic retry-after)",
                channel_id, wait,
            )
            await asyncio.sleep(wait)
            try:
                return await self._compose(guild, channel, must_post=False)
            except anthropic.RateLimitError as exc2:
                log.info(
                    "scheduled compose still 429 after %.1fs retry for channel %s",
                    wait, channel_id,
                )
                emit_error(
                    source="discourse_scheduled_retry", exc=exc2, recoverable=True,
                    guild_id=guild.id, channel_id=channel_id,
                    retry_after_seconds=wait,
                )
                return ""

    async def _maybe_post_to_channel(
        self,
        guild: discord.Guild,
        channel_id: int,
        expected: int,
        today: date,
    ) -> None:
        posts_today, last_post_at, posts_day = await self.bot.db.get_channel_slot(
            guild.id, channel_id,
        )

        # Fresh channel (deploy, newly added via /menu): consume all elapsed
        # slots as skipped. Don't post, just wait for the next natural slot.
        if last_post_at is None:
            for _ in range(expected):
                await self.bot.db.record_channel_slot(guild.id, channel_id, today)
            return

        now_et = datetime.now(ET)
        last_et = last_post_at.astimezone(ET)
        if last_et.date() == now_et.date() and posts_today >= expected:
            return

        channel = guild.get_channel(channel_id)
        if not isinstance(channel, discord.TextChannel):
            return
        me = guild.me
        if me is None or not can_send_in(channel, me):
            return

        try:
            line = await self._compose_with_retry(guild, channel, channel_id)
        except Exception:
            log.exception("scheduled post compose failed for channel %s", channel_id)
            line = ""

        await self.bot.db.record_channel_slot(guild.id, channel_id, today)
        await self.bot.db.record_schedule_post(guild.id, today)

        if not line or line.strip().upper() == "EMPTY":
            log.info(
                "scheduled slot skipped for guild %d channel %d, nothing fresh",
                guild.id, channel_id,
            )
            return

        try:
            await channel.send(line)
            await self.bot.db.add_discourse(guild.id, "open", line[:200])
        except discord.DiscordException:
            log.exception("scheduled post send failed")


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Discourse(cast("TootsiesBot", bot)))
