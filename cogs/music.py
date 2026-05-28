"""Music-lounge cog: scheduled track drops to links-only channels.

Toots posts to configured music channels on a schedule. Every post
is a track recommendation with a take + Apple Music/Spotify link
(links-only channels, posts without links get deleted).

Sources (same pipeline as discourse, music-focused):
  - Music channel itself (what are people sharing/vibing with)
  - Feed channels (Twitter/social feeds for music news, hot takes, drops)
  - Perplexity search (current music news, new releases, trending topics)
  - Claude web_search (finding tracks + music platform links at call time)

Schedule rides on the existing mood system (chill/yaps/off) with its own
slot tracking. Posts fewer than discourse (1/day chill, 2/day yaps).
Supports multiple channels (each gets independent slot tracking).

Setup: `/music setup` (mod-only multi-channel picker view).
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
from utils import voice
from utils.dedup import is_duplicate_of_recent
from utils.events import emit, emit_error
from utils.feeds import format_for_prompt, hot_urls, recent_messages
from utils.gates import require_configured
from utils.link_enrich import enrich_batch
from utils.metrics import track_command
from utils.permissions import can_send_in, is_mod
from utils.perplexity import build_search_query

if TYPE_CHECKING:
    from bot import TootsiesBot

log = logging.getLogger(__name__)

ET = ZoneInfo("America/New_York")
CHILL_TIMES = [time(14, 0)]
YAPS_TIMES = [time(11, 0), time(20, 0)]

_SCHEDULED_CHANNEL_GAP_SECONDS = 15
_RATE_LIMIT_MAX_RETRY_WAIT_SECONDS = 65.0
MUSIC_SCORE_THRESHOLD = 0.6

_MUSIC_LINK_HOSTS = ("music.apple.com", "open.spotify.com", "spotify.link")

# Rotate Perplexity search category so she doesn't default to hip-hop every time.
_MUSIC_GENRES = ["hiphop", "rnb", "pop", "afrobeats", "neo-soul"]


def _has_music_link(text: str) -> bool:
    """Check if the text contains a valid Apple Music or Spotify link."""
    lowered = text.lower()
    return any(host in lowered for host in _MUSIC_LINK_HOSTS)


def _parse_retry_after_seconds(exc: anthropic.RateLimitError) -> float | None:
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


class Music(commands.Cog):
    def __init__(self, bot: TootsiesBot) -> None:
        self.bot = bot
        self.scheduler_tick.start()

    async def cog_unload(self) -> None:
        self.scheduler_tick.cancel()

    # ---- /music ----------------------------------------------------------------

    music_group = app_commands.Group(
        name="music", description="music-lounge features",
    )

    @music_group.command(
        name="setup",
        description="pick the music channels (mods only).",
    )
    @track_command("music_setup")
    async def music_setup(self, interaction: discord.Interaction) -> None:
        if not await require_configured(interaction, self.bot.db):
            return
        member = interaction.user
        guild = interaction.guild
        if guild is None or not isinstance(member, discord.Member):
            await interaction.response.send_message(
                voice.pick(voice.PERMISSION_DENIED), ephemeral=True,
            )
            return
        if not await is_mod(self.bot.db, member):
            await interaction.response.send_message(
                voice.pick(voice.PERMISSION_DENIED), ephemeral=True,
            )
            return

        current_ids = await self.bot.db.get_music_channels(guild.id)
        defaults: list[discord.SelectDefaultValue] = []
        for cid in current_ids:
            ch = guild.get_channel(cid)
            if isinstance(ch, discord.TextChannel):
                defaults.append(discord.SelectDefaultValue(
                    id=ch.id, type=discord.SelectDefaultValueType.channel,
                ))
        view = _MusicSetupView(self.bot, guild, member.id, defaults)
        current_label = (
            ", ".join(f"<#{cid}>" for cid in current_ids) if current_ids else "none"
        )
        embed = discord.Embed(
            title="music-lounge setup",
            description=(
                "pick the channels where i'll drop tracks and hot takes. "
                "saves when you pick."
                f"\n\ncurrently: {current_label}"
            ),
            color=0x9b59b6,
        )
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)

    @music_group.command(
        name="drop",
        description="drop a track recommendation right now.",
    )
    @track_command("music_drop")
    async def music_drop(self, interaction: discord.Interaction) -> None:
        if not await require_configured(interaction, self.bot.db):
            return
        assert interaction.guild_id is not None
        guild = interaction.guild
        assert guild is not None

        target = interaction.channel
        if not isinstance(target, discord.TextChannel | discord.Thread):
            await interaction.response.send_message(
                voice.pick(voice.DB_ERROR), ephemeral=True,
            )
            return

        await interaction.response.defer(thinking=True)

        try:
            line = await self._compose(guild, target, must_post=True)
        except Exception as exc:
            log.exception("music compose failed")
            emit_error(
                source="music_drop", exc=exc, recoverable=False,
                guild_id=interaction.guild_id, user_id=interaction.user.id,
            )
            await interaction.followup.send(voice.pick(voice.DB_ERROR))
            return

        try:
            await self.bot.db.add_music_history(interaction.guild_id, line[:200])
        except Exception:
            log.exception("music bookkeeping failed")

        await interaction.followup.send(line)

    # ---- compose pipeline -------------------------------------------------------

    async def _compose(
        self,
        guild: discord.Guild,
        channel: discord.TextChannel | discord.Thread,
        *,
        must_post: bool = True,
    ) -> str:
        me = guild.me
        assert me is not None

        sources: list[str] = []
        all_feed_msgs: list[discord.Message] = []

        # Pull from feed channels tagged with music-related categories
        feeds = await self.bot.db.get_feed_channels(guild.id)
        for feed_channel_id, _cat in feeds[:5]:
            ch = guild.get_channel(feed_channel_id)
            if isinstance(ch, discord.TextChannel):
                msgs = await recent_messages(
                    ch, me, limit=30, within=timedelta(hours=24), include_bots=True,
                )
                if msgs:
                    sources.append(
                        f"--- #{ch.name} (feed) ---\n"
                        f"{format_for_prompt(msgs, include_reactions=True)}"
                    )
                    all_feed_msgs.extend(msgs)

        # Recent music channel messages
        local = await recent_messages(
            channel, me, limit=50, within=timedelta(hours=24), include_bots=True,
        )
        if local:
            sources.append(
                f"--- #{channel.name} (recent, last 24h) ---\n"
                f"{format_for_prompt(local, include_reactions=True)}"
            )
            all_feed_msgs.extend(local)

        feed_hot_urls = hot_urls(all_feed_msgs, limit=8)

        genre = "music" if local else random.choice(_MUSIC_GENRES)

        # Run enrichment, Perplexity, and DB history in parallel.
        coros: list[Any] = [enrich_batch([u for u, _, _, _ in feed_hot_urls])]

        pplx_idx = -1
        pplx = self.bot.perplexity
        if pplx:
            pplx_idx = len(coros)
            coros.append(pplx.search(
                build_search_query(
                    "", surface="discourse",
                    category=genre, channel_name=channel.name,
                ),
                purpose="music",
            ))

        db_idx = len(coros)
        coros.append(self.bot.db.recent_music_history(guild.id, limit=15))

        raw = await asyncio.gather(*coros, return_exceptions=True)
        enriched_map = raw[0] if not isinstance(raw[0], BaseException) else {}
        pplx_result: str | None = (
            raw[pplx_idx]  # type: ignore[assignment]
            if pplx_idx >= 0 and not isinstance(raw[pplx_idx], BaseException) else None
        )
        recent_all: list[str] = (
            raw[db_idx] if not isinstance(raw[db_idx], BaseException) else []  # type: ignore[assignment]
        )

        enriched = [v for v in enriched_map.values() if v is not None]
        sources_blob = "\n\n".join(sources) if sources else "(quiet channel)"
        recent_blob = "\n".join(f"- {topic}" for topic in recent_all)

        line = await self.bot.claude.music_post(
            sources_blob=sources_blob,
            recent_posts=recent_blob,
            channel_name=channel.name,
            must_post=must_post,
            hot_urls=feed_hot_urls,
            enriched_links=enriched,
            perplexity_context=pplx_result,
            genre_hint=genre,
        )

        if not line or line.strip().upper() == "EMPTY":
            emit(
                "music_fallback",
                guild_id=guild.id, reason="claude_returned_empty",
            )
            # Links-only channel: don't post a linkless fallback quip.
            # Skip the slot on scheduled posts; on manual /music drop,
            # tell the user nothing came up.
            if must_post:
                return "nothing's hitting right now. try again in a bit."
            return ""

        # Quality gate
        try:
            score, reason = await self.bot.claude.discourse_score(
                line, channel_name=channel.name, surface="music",
            )
        except Exception as exc:
            emit_error(
                source="music_score", exc=exc, recoverable=True,
                guild_id=guild.id, channel_id=channel.id,
            )
            score, reason = 1.0, "score_failed_pass_through"

        emit(
            "music_scored",
            guild_id=guild.id, channel_id=channel.id,
            score=score, reason=reason, must_post=must_post,
            post_preview=line[:120],
        )

        if score < MUSIC_SCORE_THRESHOLD and not must_post:
            log.info(
                "music scored %.2f (< %.2f) for guild %d channel %d, skipping",
                score, MUSIC_SCORE_THRESHOLD, guild.id, channel.id,
            )
            return ""

        # Links-only gate: post must contain an Apple Music or Spotify link.
        # Retry once if missing; skip the slot if still no link.
        if not _has_music_link(line):
            emit(
                "music_link_missing",
                guild_id=guild.id, channel_id=channel.id,
                must_post=must_post, attempt=1, post_preview=line[:120],
            )
            log.info("music post missing music link, retrying for guild %d channel %d", guild.id, channel.id)
            line2 = await self.bot.claude.music_post(
                sources_blob=sources_blob,
                recent_posts=recent_blob,
                channel_name=channel.name,
                must_post=must_post,
                hot_urls=feed_hot_urls,
                enriched_links=enriched,
                perplexity_context=pplx_result,
                genre_hint=genre,
            )
            if line2 and line2.strip().upper() != "EMPTY" and _has_music_link(line2):
                return line2
            emit(
                "music_link_missing",
                guild_id=guild.id, channel_id=channel.id,
                must_post=must_post, attempt=2, post_preview=(line2 or "")[:120],
            )
            if not must_post:
                log.info("music post still missing link after retry, skipping slot")
                return ""
            return line2 if line2 and line2.strip().upper() != "EMPTY" else line

        return line

    # ---- scheduler ---------------------------------------------------------------

    @tasks.loop(minutes=1)
    async def scheduler_tick(self) -> None:
        try:
            now_et = datetime.now(ET)
            for guild_id in await self.bot.db.all_configured_guilds():
                await self._maybe_scheduled_post(guild_id, now_et)
        except Exception:
            log.exception("music scheduler tick failed")

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

        channel_ids = await self.bot.db.get_music_channels(guild_id)
        if not channel_ids:
            return

        for i, channel_id in enumerate(channel_ids):
            if i > 0:
                await asyncio.sleep(_SCHEDULED_CHANNEL_GAP_SECONDS)
            await self._maybe_post_to_channel(guild, channel_id, expected, today_et)

    async def _compose_with_retry(
        self,
        guild: discord.Guild,
        channel: discord.TextChannel | discord.Thread,
        channel_id: int,
    ) -> str:
        try:
            return await self._compose(guild, channel, must_post=False)
        except anthropic.RateLimitError as exc:
            retry_after = _parse_retry_after_seconds(exc)
            if retry_after is None:
                log.info("music scheduled 429 for channel %s, no retry-after; skipping", channel_id)
                emit_error(
                    source="music_scheduled", exc=exc, recoverable=True,
                    guild_id=guild.id, channel_id=channel_id,
                )
                return ""
            wait = min(retry_after, _RATE_LIMIT_MAX_RETRY_WAIT_SECONDS)
            log.info("music scheduled 429 for channel %s, retrying in %.1fs", channel_id, wait)
            await asyncio.sleep(wait)
            try:
                return await self._compose(guild, channel, must_post=False)
            except anthropic.RateLimitError as exc2:
                log.info("music still 429 after %.1fs retry for channel %s", wait, channel_id)
                emit_error(
                    source="music_scheduled", exc=exc2, recoverable=True,
                    guild_id=guild.id, channel_id=channel_id,
                    retried=True, retry_after_seconds=wait,
                )
                return ""

    async def _maybe_post_to_channel(
        self,
        guild: discord.Guild,
        channel_id: int,
        expected: int,
        today: date,
    ) -> None:
        posts_today, last_post_at, posts_day = await self.bot.db.get_music_slot(
            guild.id, channel_id,
        )

        if last_post_at is None:
            for _ in range(expected):
                await self.bot.db.record_music_slot(guild.id, channel_id, today)
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
            log.exception("music scheduled compose failed for channel %s", channel_id)
            line = ""

        if not line or line.strip().upper() == "EMPTY":
            await self.bot.db.record_music_slot(guild.id, channel_id, today)
            log.info("music slot skipped for guild %d channel %d", guild.id, channel_id)
            return

        recent_all = await self.bot.db.recent_music_history(guild.id, limit=15)
        if is_duplicate_of_recent(line, recent_all):
            await self.bot.db.record_music_slot(guild.id, channel_id, today)
            emit(
                "music_dedup",
                guild_id=guild.id, channel_id=channel_id,
                decision="similarity_gate",
                post_preview=line[:120],
            )
            log.info("music post deduped for guild %d channel %d", guild.id, channel_id)
            return

        try:
            await channel.send(line)
            await self.bot.db.add_music_history(guild.id, line[:200])
            await self.bot.db.record_music_slot(guild.id, channel_id, today)
        except discord.DiscordException:
            log.exception("music scheduled send failed")


class _MusicSetupView(discord.ui.View):
    def __init__(
        self,
        bot: TootsiesBot,
        guild: discord.Guild,
        actor_id: int,
        defaults: list[discord.SelectDefaultValue],
    ) -> None:
        super().__init__(timeout=300)
        self.bot = bot
        self.guild = guild
        self.actor_id = actor_id
        self.add_item(_MusicChannelSelect(self, defaults))


class _MusicChannelSelect(discord.ui.ChannelSelect):
    def __init__(
        self, parent: _MusicSetupView, defaults: list[discord.SelectDefaultValue],
    ) -> None:
        super().__init__(
            placeholder="pick music channels",
            min_values=1, max_values=25, row=0,
            channel_types=[discord.ChannelType.text],
            default_values=defaults,
        )
        self.parent_view = parent

    async def callback(self, interaction: discord.Interaction) -> None:
        if interaction.user.id != self.parent_view.actor_id:
            await interaction.response.send_message("not your menu.", ephemeral=True)
            return
        channel_ids = [c.id for c in self.values]
        guild_id = self.parent_view.guild.id
        await self.parent_view.bot.db.set_music_channels(guild_id, channel_ids)
        await self.parent_view.bot.db.audit(
            guild_id, interaction.user.id, "music_channels_set",
            after={"channel_ids": channel_ids},
        )
        label = ", ".join(f"<#{cid}>" for cid in channel_ids)
        embed = discord.Embed(
            title="locked in.",
            description=f"i'll be dropping tracks in {label}.",
            color=0x2ecc71,
        )
        self.default_values = [
            discord.SelectDefaultValue(
                id=cid, type=discord.SelectDefaultValueType.channel,
            )
            for cid in channel_ids
        ]
        await interaction.response.edit_message(embed=embed, view=self.parent_view)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Music(cast("TootsiesBot", bot)))
