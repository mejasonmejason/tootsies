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
import random
from datetime import date, datetime, time, timedelta
from typing import TYPE_CHECKING, Any, cast
from zoneinfo import ZoneInfo

import anthropic
import discord
from discord import app_commands
from discord.ext import commands, tasks

from models import MoodMode
from utils import bot_logs, voice
from utils.dedup import is_duplicate_of_recent
from utils.events import emit, emit_error
from utils.feeds import (
    format_for_prompt,
    hot_urls,
    recent_image_urls,
    recent_messages,
)
from utils.gates import require_configured
from utils.link_enrich import enrich_batch
from utils.markets import MarketSnapshot
from utils.metrics import track_command
from utils.permissions import can_send_in
from utils.perplexity import build_search_query
from utils.rate_limits import check_server_limit, consume_server
from utils.url_guardrail import extract_urls

if TYPE_CHECKING:
    from bot import TootsiesBot

log = logging.getLogger(__name__)

CATEGORIES = ["pop", "sports", "cinema", "hiphop", "nba"]

ET = ZoneInfo("America/New_York")
CHILL_TIMES = [time(12, 0), time(19, 0)]
YAPS_TIMES = [time(9, 0), time(12, 0), time(15, 0), time(18, 0), time(22, 0)]

# Gap between per-channel compose calls inside a single scheduler tick.
# Each compose burns ~10K input tokens on sonnet-4-6 and the org TPM ceiling
# is 30K/min, so bursting >3 channels in <60s reliably trips 429.
_SCHEDULED_CHANNEL_GAP_SECONDS = 15

# Random delay before a guild's scheduled dispatch starts. The music cog rides
# the same mood schedule and fires on the same top-of-hour tick; without jitter,
# discourse + music hit the shared Sonnet TPM ceiling at the same instant and
# trip a 429 (issue #123). Scheduled posts have no user waiting, so spreading
# the burst across a 30s window is free.
_SCHEDULED_JITTER_MAX_SECONDS = 30.0

# Upper bound on how long we'll honor a retry-after on a scheduled compose
# 429. TPM rolling windows max out at 60s; 65 gives a small cushion without
# letting a misbehaving header pin a tick for minutes.
_RATE_LIMIT_MAX_RETRY_WAIT_SECONDS = 65.0

# Post-generation quality gate: Haiku scores the generated post before it ships.
# Below this threshold, scheduled slots are skipped and manual posts retry once.
DISCOURSE_SCORE_THRESHOLD = 0.6


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

        # Run link enrichment, Perplexity search, markets fetch, and DB history
        # in parallel. return_exceptions=True so one fetcher's outage can't
        # cancel the others.
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
        # The "query" for markets in /discourse is the category + channel name,
        # which feeds the Haiku classifier and gets routed to SGO (if sports) or
        # Polymarket/Kalshi (if culture/elections/etc.).
        markets_query = f"{category or ''} {channel.name}".strip() or channel.name
        markets_idx = len(coros)
        coros.append(self.bot.markets.get_context(markets_query))
        db_idx = len(coros)
        coros.append(self.bot.db.recent_discourse_all(guild.id, limit=20))

        raw = await asyncio.gather(*coros, return_exceptions=True)
        enriched_map = raw[0] if not isinstance(raw[0], BaseException) else {}
        pplx_result: str | None = (
            raw[pplx_idx]  # type: ignore[assignment]
            if pplx_idx >= 0 and not isinstance(raw[pplx_idx], BaseException) else None
        )
        markets_raw = raw[markets_idx]
        markets_result: list[MarketSnapshot] | None = (
            markets_raw if isinstance(markets_raw, list) else None
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

        # URLs already visible in the destination channel's last hour. These
        # get deduped from Toots's output: if the room just saw the link, a
        # re-paste is redundant (Discord shows two embeds of the same thing).
        recently_seen_urls = [
            u for msg in local for u in extract_urls(msg.content)
        ] if local else None

        compose_kwargs: dict[str, Any] = dict(
            category=category, sources_blob=sources_blob,
            recent_with_timestamps=recent_blob, channel_name=channel.name,
            image_urls=image_urls, hot_urls=feed_hot_urls,
            enriched_links=enriched, perplexity_context=pplx_result,
            recently_seen_urls=recently_seen_urls,
            markets_context=markets_result,
        )

        line = await self.bot.claude.discourse(
            compose_kwargs["category"], compose_kwargs["sources_blob"],
            recent_with_timestamps=compose_kwargs["recent_with_timestamps"],
            channel_name=compose_kwargs["channel_name"], must_post=must_post,
            image_urls=compose_kwargs["image_urls"],
            hot_urls=compose_kwargs["hot_urls"],
            enriched_links=compose_kwargs["enriched_links"],
            perplexity_context=compose_kwargs["perplexity_context"],
            recently_seen_urls=compose_kwargs["recently_seen_urls"],
            markets_context=compose_kwargs["markets_context"],
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

        # Post-generation quality gate: Haiku scores engagement potential.
        try:
            score, reason = await self.bot.claude.discourse_score(
                line, channel_name=channel.name,
            )
        except Exception as exc:
            emit_error(
                source="discourse_score", exc=exc, recoverable=True,
                guild_id=guild.id, channel_id=channel.id,
            )
            score, reason = 1.0, "score_failed_pass_through"

        emit(
            "discourse_scored",
            guild_id=guild.id, channel_id=channel.id, user_id=user_id,
            score=score, reason=reason, must_post=must_post,
            category=category, post_preview=line[:120],
        )

        if score >= DISCOURSE_SCORE_THRESHOLD:
            return line

        # Below threshold: scheduled posts skip, manual posts retry once.
        if not must_post:
            log.info(
                "discourse scored %.2f (< %.2f) for guild %d channel %d, skipping slot",
                score, DISCOURSE_SCORE_THRESHOLD, guild.id, channel.id,
            )
            return ""

        # Manual: retry with a nudge to pick a different angle.
        line2 = await self.bot.claude.discourse(
            compose_kwargs["category"], compose_kwargs["sources_blob"],
            recent_with_timestamps=compose_kwargs["recent_with_timestamps"],
            channel_name=compose_kwargs["channel_name"], must_post=True,
            image_urls=compose_kwargs["image_urls"],
            hot_urls=compose_kwargs["hot_urls"],
            enriched_links=compose_kwargs["enriched_links"],
            perplexity_context=compose_kwargs["perplexity_context"],
            recently_seen_urls=compose_kwargs["recently_seen_urls"],
            markets_context=compose_kwargs["markets_context"],
        )
        if not line2 or line2.strip().upper() == "EMPTY":
            return line  # retry produced nothing, send the original
        return line2

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

        # Decorrelate this guild's dispatch from the music cog's same-tick post
        # so they don't collide on the shared Sonnet TPM ceiling (issue #123).
        await asyncio.sleep(random.uniform(0, _SCHEDULED_JITTER_MAX_SECONDS))

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
    ) -> tuple[str, str | None]:
        """Compose, honoring a single retry-after on RateLimitError.

        Scheduled posts have no user waiting, so when Anthropic tells us
        "wait N seconds", it's strictly better to wait and post late than
        to skip the slot entirely. One retry only, capped at 65s.

        Returns (line, skip_reason). skip_reason is None on success and
        "rate_limited" when the slot is dropped after a persistent 429; the
        caller emits a structured `discourse_skipped` event off it.
        """
        try:
            return await self._compose(guild, channel, must_post=False), None
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
                return "", "rate_limited"
            wait = min(retry_after, _RATE_LIMIT_MAX_RETRY_WAIT_SECONDS)
            log.info(
                "scheduled compose 429 for channel %s, retrying in %.1fs (anthropic retry-after)",
                channel_id, wait,
            )
            await asyncio.sleep(wait)
            try:
                return await self._compose(guild, channel, must_post=False), None
            except anthropic.RateLimitError as exc2:
                log.info(
                    "scheduled compose still 429 after %.1fs retry for channel %s",
                    wait, channel_id,
                )
                emit_error(
                    source="discourse_scheduled", exc=exc2, recoverable=True,
                    guild_id=guild.id, channel_id=channel_id,
                    retried=True, retry_after_seconds=wait,
                )
                return "", "rate_limited"

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
            line, skip_reason = await self._compose_with_retry(guild, channel, channel_id)
        except Exception as exc:
            log.exception("scheduled post compose failed for channel %s", channel_id)
            emit_error(
                source="discourse_scheduled", exc=exc, recoverable=False,
                guild_id=guild.id, channel_id=channel_id,
            )
            line, skip_reason = "", "compose_error"

        await self.bot.db.record_channel_slot(guild.id, channel_id, today)
        await self.bot.db.record_schedule_post(guild.id, today)

        if not line or line.strip().upper() == "EMPTY":
            # Every dropped scheduled slot emits one structured event so the
            # log-monitor routine can see it (a 429 / compose crash / empty
            # generation used to vanish with only a plain log line). reason:
            # rate_limited | compose_error | empty.
            reason = skip_reason or "empty"
            emit(
                "discourse_skipped",
                guild_id=guild.id, channel_id=channel_id, reason=reason,
            )
            log.info(
                "scheduled slot skipped for guild %d channel %d (reason=%s)",
                guild.id, channel_id, reason,
            )
            return

        recent_all = await self.bot.db.recent_discourse_all(guild.id, limit=20)
        recent_summaries = [topic for _, topic, _ in recent_all]
        if is_duplicate_of_recent(line, recent_summaries):
            emit(
                "discourse_dedup",
                guild_id=guild.id, channel_id=channel_id,
                decision="similarity_gate",
                post_preview=line[:120],
            )
            log.info(
                "scheduled post deduped for guild %d channel %d, too similar to recent post",
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
