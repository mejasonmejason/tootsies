"""Chime-in: Toots leans into the conversation when she has something to say.

No commands of its own. Wires into existing settings:

  - Listen channel: the guild's `discourse_channel` (set via /menu).
  - On/off + cadence: the mood schedule. mood=off silences, chill is reserved
    (3/day, 60min cooldown, 0.8 threshold), yaps is chatty (6/day, 20min
    cooldown, 0.6 threshold). Mirrors the 2:4 ratio scheduled discourse
    already uses.

Algorithm (also documented in docs/ALGORITHMS.md):
  - on_message in the discourse channel appends to a per-channel deque (max 50).
  - tasks.loop(seconds=60) walks each guild with buffered activity:
      * mood != off
      * Buffer has >= BUFFER_MIN_FOR_SCORE new messages since last evaluation
      * Outside cooldown (mood-tuned: 60min chill / 20min yaps)
      * Within hours window (9am to 2am ET)
      * Under daily cap (mood-tuned: 3 chill / 6 yaps)
    -> call Haiku to score (score, vibe, hook)
  - Then gate by:
      * vibe not in {vulnerable, catchup, other}
      * score >= mood-tuned THRESHOLD (0.8 chill / 0.6 yaps)
    -> call Sonnet to generate the actual post + send + record.

The goal is to push the ROOM to keep talking to each other, not to start a
back-and-forth with Toots. The chimein_post prompt enforces this; the
on_message listener can stay dumb.

Failures in any step go to events + skip cleanly (no crash, no spam).
"""

from __future__ import annotations

import logging
from collections import defaultdict, deque
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, cast
from zoneinfo import ZoneInfo

import discord
from discord.ext import commands, tasks

from models import MoodMode
from utils.events import emit, emit_error
from utils.feeds import format_for_prompt, hot_urls, recent_image_urls
from utils.link_enrich import enrich_batch
from utils.permissions import can_send_in

if TYPE_CHECKING:
    from bot import TootsiesBot

log = logging.getLogger(__name__)


# ---- tunables (also listed in docs/ALGORITHMS.md) ---------------------------

ET = ZoneInfo("America/New_York")

# Buffer must have at least this many messages before we even score it. Below
# this, we don't have enough signal to know what the room's talking about.
BUFFER_MIN_FOR_SCORE = 5
# In-memory buffer cap per channel. We don't need infinite history; the cheap
# Haiku scoring pass works fine on the most recent 50 messages.
BUFFER_MAX = 50

# Hours window in ET. (9, 26) = 9am through 2am next day. Outside this we
# treat the room as "sleeping" and don't fire chime-ins regardless of mood.
HOURS_START_ET = 9
HOURS_END_ET_NEXT_DAY = 26  # 24 + 2 = 2am next day

# Vibes that never get a chime-in regardless of score. Bartender doesn't
# interrupt vulnerable shares or casual catch-ups.
SKIP_VIBES = {"vulnerable", "catchup", "other"}

# Tick frequency. Cheap; the scoring pass only fires when buffer has activity.
TICK_SECONDS = 60


class _MoodTuning:
    """Cadence knobs per mood. Higher threshold = more reserved Toots."""

    __slots__ = ("threshold", "daily_cap", "cooldown")

    def __init__(self, *, threshold: float, daily_cap: int, cooldown: timedelta) -> None:
        self.threshold = threshold
        self.daily_cap = daily_cap
        self.cooldown = cooldown


# Mirrors the discourse scheduler's 2:4 chill:yaps post ratio. Chill is the
# reserved bartender, yaps is the one leaning across the bar.
MOOD_TUNING: dict[MoodMode, _MoodTuning] = {
    MoodMode.CHILL: _MoodTuning(
        threshold=0.8, daily_cap=3, cooldown=timedelta(minutes=60),
    ),
    MoodMode.YAPS: _MoodTuning(
        threshold=0.6, daily_cap=6, cooldown=timedelta(minutes=20),
    ),
}


class ChimeIn(commands.Cog):
    """Background chime-in listener + scorer. No slash commands of its own."""

    def __init__(self, bot: TootsiesBot) -> None:
        self.bot = bot
        # (guild_id, channel_id) -> deque of messages
        self._buffers: defaultdict[tuple[int, int], deque[discord.Message]] = (
            defaultdict(lambda: deque(maxlen=BUFFER_MAX))
        )
        # (guild_id, channel_id) -> count of messages added since last evaluation.
        # We track this so we only score when there's *new* signal, not just on
        # the time interval.
        self._new_since_eval: defaultdict[tuple[int, int], int] = defaultdict(int)
        # guild_id -> set of discourse channel IDs. Refreshed each tick so /menu
        # edits take effect within a tick instead of needing a restart.
        self._listen_channels: dict[int, set[int]] = {}
        self.tick.start()

    async def cog_unload(self) -> None:
        self.tick.cancel()

    # ---- on_message listener ----------------------------------------------------

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        """Append qualifying messages to the per-channel buffer."""
        if message.guild is None:
            return
        if message.author.bot:
            # Skip bot/webhook posts: chime-in should react to humans, not feed bots.
            return
        # Pure-attachment messages get added too (vision picks them up).
        if not message.content.strip() and not message.attachments and not message.embeds:
            return
        listen_channels = self._listen_channels.get(message.guild.id)
        if not listen_channels or message.channel.id not in listen_channels:
            return
        key = (message.guild.id, message.channel.id)
        self._buffers[key].append(message)
        self._new_since_eval[key] += 1

    # ---- background tick --------------------------------------------------------

    @tasks.loop(seconds=TICK_SECONDS)
    async def tick(self) -> None:
        try:
            await self._maybe_chime_in_all()
        except Exception:
            log.exception("chime-in tick failed")

    @tick.before_loop
    async def before_tick(self) -> None:
        await self.bot.wait_until_ready()

    async def _refresh_listen_channels(self) -> None:
        """Pull discourse channels for every guild we're in."""
        fresh: dict[int, set[int]] = {}
        for guild in self.bot.guilds:
            channels = await self.bot.db.get_discourse_channels(guild.id)
            if channels:
                fresh[guild.id] = set(channels)
        self._listen_channels = fresh

    async def _maybe_chime_in_all(self) -> None:
        """Walk every (guild, channel) with buffered activity and try to chime in."""
        await self._refresh_listen_channels()
        for key in list(self._buffers.keys()):
            guild_id, channel_id = key
            guild_channels = self._listen_channels.get(guild_id)
            if not guild_channels or channel_id not in guild_channels:
                self._buffers.pop(key, None)
                self._new_since_eval.pop(key, None)
                continue
            if self._new_since_eval[key] < BUFFER_MIN_FOR_SCORE:
                continue
            try:
                await self._maybe_chime_in_one(guild_id, channel_id)
            except Exception:
                log.exception(
                    "chime-in one-channel evaluation failed: guild=%s channel=%s",
                    guild_id, channel_id,
                )
            finally:
                # Reset the counter regardless of outcome: even if we declined to
                # post, the buffer's been considered. Next eval needs fresh signal.
                self._new_since_eval[key] = 0

    async def _maybe_chime_in_one(self, guild_id: int, channel_id: int) -> None:
        # ---- Pre-flight gates --------------------------------------------------
        # Mood gate: piggyback on the discourse mood setting. mood=off => silent.
        # The mood also picks the cadence knobs (threshold/cap/cooldown).
        schedule = await self.bot.db.get_schedule(guild_id)
        if schedule.mood == MoodMode.OFF:
            emit(
                "chimein_evaluated", guild_id=guild_id, channel_id=channel_id,
                decision="mood_off_gate",
            )
            return
        tuning = MOOD_TUNING.get(schedule.mood)
        if tuning is None:
            # Defensive: if a new mood is added without tuning, skip rather than crash.
            emit(
                "chimein_evaluated", guild_id=guild_id, channel_id=channel_id,
                decision="mood_off_gate", mood=str(schedule.mood),
            )
            return

        # Hours window
        now_et = datetime.now(ET)
        et_hour_of_day = now_et.hour
        # Translate "9am to 2am next day" into a single test
        if not (et_hour_of_day >= HOURS_START_ET or et_hour_of_day < (HOURS_END_ET_NEXT_DAY - 24)):
            emit(
                "chimein_evaluated", guild_id=guild_id, channel_id=channel_id,
                decision="hours_gate", local_hour_et=et_hour_of_day,
            )
            return

        # Cooldown (mood-tuned)
        last_at = await self.bot.db.last_chimein_at(guild_id, channel_id)
        if last_at is not None and datetime.now(UTC) - last_at < tuning.cooldown:
            emit(
                "chimein_evaluated", guild_id=guild_id, channel_id=channel_id,
                decision="cooldown_gate", mood=str(schedule.mood),
            )
            return

        # Daily cap (mood-tuned)
        count_today = await self.bot.db.chimein_count_today(guild_id, channel_id)
        if count_today >= tuning.daily_cap:
            emit(
                "chimein_evaluated", guild_id=guild_id, channel_id=channel_id,
                decision="daily_cap_gate", count_today=count_today,
                mood=str(schedule.mood),
            )
            return

        # ---- Score the buffer --------------------------------------------------
        key = (guild_id, channel_id)
        msgs = list(self._buffers[key])
        buffer_blob = format_for_prompt(msgs, include_reactions=True)
        try:
            score, vibe, hook = await self.bot.claude.chimein_score(buffer_blob)
        except Exception as exc:
            emit_error(
                source="chimein_score", exc=exc, recoverable=True,
                guild_id=guild_id, channel_id=channel_id,
            )
            return

        if vibe in SKIP_VIBES:
            emit(
                "chimein_evaluated", guild_id=guild_id, channel_id=channel_id,
                decision="vibe_gate", vibe=vibe, score=score,
            )
            return
        if score < tuning.threshold:
            emit(
                "chimein_evaluated", guild_id=guild_id, channel_id=channel_id,
                decision="threshold_gate", vibe=vibe, score=score,
                mood=str(schedule.mood),
            )
            return

        # ---- Resolve channel + generate the post --------------------------------
        guild = self.bot.get_guild(guild_id)
        if guild is None:
            return
        channel = guild.get_channel(channel_id)
        if not isinstance(channel, discord.TextChannel):
            return
        me = guild.me
        if me is None or not can_send_in(channel, me):
            return

        image_urls = recent_image_urls(msgs, limit=5)
        # Pre-fetch enriched social-link content. Chime-in is latency-sensitive
        # (the tick blocks the next eval), so we cap concurrency at 5 and the
        # per-URL timeout in link_enrich is 2s, bounding the worst case.
        chime_hot_urls = hot_urls(msgs, limit=5)
        enriched_map = await enrich_batch(
            [u for u, _, _, _ in chime_hot_urls], concurrency=5,
        )
        enriched = [v for v in enriched_map.values() if v is not None]
        try:
            line = await self.bot.claude.chimein_post(
                buffer_blob, hook=hook, image_urls=image_urls,
                enriched_links=enriched,
            )
        except Exception as exc:
            emit_error(
                source="chimein_post", exc=exc, recoverable=True,
                guild_id=guild_id, channel_id=channel_id,
            )
            return

        if not line.strip():
            emit(
                "chimein_evaluated", guild_id=guild_id, channel_id=channel_id,
                decision="empty_generation", vibe=vibe, score=score,
            )
            return

        try:
            await channel.send(line)
        except discord.DiscordException:
            log.exception("chime-in send failed for guild=%s channel=%s", guild_id, channel_id)
            return

        await self.bot.db.record_chimein(
            guild_id, channel_id, score=score, vibe=vibe, hook=hook,
        )
        emit(
            "chimein_posted",
            guild_id=guild_id, channel_id=channel_id,
            score=score, vibe=vibe, hook=hook[:200],
            mood=str(schedule.mood),
        )


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(ChimeIn(cast("TootsiesBot", bot)))
