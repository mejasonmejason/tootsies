"""Chip-in: Toots leans into the conversation when she has something to say.

No commands of its own. Wires into existing settings:

  - Listen channel: the guild's `discourse_channel` (set via /menu).
  - On/off control: the mood schedule (mood=off => no chip-in).

Algorithm (also documented in docs/ALGORITHMS.md):
  - on_message in the discourse channel appends to a per-channel deque (max 50).
  - tasks.loop(seconds=60) walks each guild with buffered activity:
      * mood != off
      * Buffer has >= BUFFER_MIN_FOR_SCORE new messages since last evaluation
      * Outside cooldown (default 30 min since last chip-in this channel)
      * Within hours window (9am to 2am ET)
      * Under daily cap (5 chip-ins per channel per 24h)
    -> call Haiku to score (score, vibe, hook)
  - Then gate by:
      * vibe not in {vulnerable, catchup, other}
      * score >= THRESHOLD (default 0.7)
    -> call Sonnet to generate the actual post + send + record.

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
from utils.events import emit
from utils.feeds import format_for_prompt, recent_image_urls
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

# Don't chip in more often than this in a single channel.
COOLDOWN = timedelta(minutes=30)
# Hard cap per channel per rolling 24h. Stops runaway behavior on busy days.
DAILY_CAP = 5

# Hours window in ET. (9, 26) = 9am through 2am next day. Outside this we
# treat the room as "sleeping" and don't fire chip-ins.
HOURS_START_ET = 9
HOURS_END_ET_NEXT_DAY = 26  # 24 + 2 = 2am next day

# Score threshold. Higher = more reserved Toots, lower = chattier.
THRESHOLD = 0.7

# Vibes that never get a chip-in regardless of score. Bartender doesn't
# interrupt vulnerable shares or casual catch-ups.
SKIP_VIBES = {"vulnerable", "catchup", "other"}

# Tick frequency. Cheap; the scoring pass only fires when buffer has activity.
TICK_SECONDS = 60


class ChipIn(commands.Cog):
    """Background chip-in listener + scorer. No slash commands of its own."""

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
        # guild_id -> discourse_channel_id. Refreshed each tick so /menu edits
        # take effect within a tick instead of needing a restart.
        self._listen_channels: dict[int, int] = {}
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
            # Skip bot/webhook posts: chip-in should react to humans, not feed bots.
            return
        # Pure-attachment messages get added too (vision picks them up).
        if not message.content.strip() and not message.attachments and not message.embeds:
            return
        # Only buffer if this is the guild's configured discourse channel.
        listen_channel = self._listen_channels.get(message.guild.id)
        if listen_channel is None or listen_channel != message.channel.id:
            return
        key = (message.guild.id, message.channel.id)
        self._buffers[key].append(message)
        self._new_since_eval[key] += 1

    # ---- background tick --------------------------------------------------------

    @tasks.loop(seconds=TICK_SECONDS)
    async def tick(self) -> None:
        try:
            await self._maybe_chip_in_all()
        except Exception:
            log.exception("chip-in tick failed")

    @tick.before_loop
    async def before_tick(self) -> None:
        await self.bot.wait_until_ready()

    async def _refresh_listen_channels(self) -> None:
        """Pull the current discourse_channel setting for every guild we're in."""
        fresh: dict[int, int] = {}
        for guild in self.bot.guilds:
            raw = await self.bot.db.get_setting(guild.id, "discourse_channel")
            if not raw:
                continue
            try:
                fresh[guild.id] = int(raw)
            except (TypeError, ValueError):
                continue
        self._listen_channels = fresh

    async def _maybe_chip_in_all(self) -> None:
        """Walk every (guild, channel) with buffered activity and try to chip in."""
        await self._refresh_listen_channels()
        for key in list(self._buffers.keys()):
            guild_id, channel_id = key
            if self._listen_channels.get(guild_id) != channel_id:
                # discourse_channel changed (or was cleared); drop the stale buffer.
                self._buffers.pop(key, None)
                self._new_since_eval.pop(key, None)
                continue
            if self._new_since_eval[key] < BUFFER_MIN_FOR_SCORE:
                continue
            try:
                await self._maybe_chip_in_one(guild_id, channel_id)
            except Exception:
                log.exception(
                    "chip-in one-channel evaluation failed: guild=%s channel=%s",
                    guild_id, channel_id,
                )
            finally:
                # Reset the counter regardless of outcome: even if we declined to
                # post, the buffer's been considered. Next eval needs fresh signal.
                self._new_since_eval[key] = 0

    async def _maybe_chip_in_one(self, guild_id: int, channel_id: int) -> None:
        # ---- Pre-flight gates --------------------------------------------------
        # Mood gate: piggyback on the discourse mood setting. mood=off => silent.
        schedule = await self.bot.db.get_schedule(guild_id)
        if schedule.mood == MoodMode.OFF:
            emit(
                "chipin_evaluated", guild_id=guild_id, channel_id=channel_id,
                decision="mood_off_gate",
            )
            return

        # Hours window
        now_et = datetime.now(ET)
        et_hour_of_day = now_et.hour
        # Translate "9am to 2am next day" into a single test
        if not (et_hour_of_day >= HOURS_START_ET or et_hour_of_day < (HOURS_END_ET_NEXT_DAY - 24)):
            emit(
                "chipin_evaluated", guild_id=guild_id, channel_id=channel_id,
                decision="hours_gate", local_hour_et=et_hour_of_day,
            )
            return

        # Cooldown
        last_at = await self.bot.db.last_chipin_at(guild_id, channel_id)
        if last_at is not None and datetime.now(UTC) - last_at < COOLDOWN:
            emit(
                "chipin_evaluated", guild_id=guild_id, channel_id=channel_id,
                decision="cooldown_gate",
            )
            return

        # Daily cap
        count_today = await self.bot.db.chipin_count_today(guild_id, channel_id)
        if count_today >= DAILY_CAP:
            emit(
                "chipin_evaluated", guild_id=guild_id, channel_id=channel_id,
                decision="daily_cap_gate", count_today=count_today,
            )
            return

        # ---- Score the buffer --------------------------------------------------
        key = (guild_id, channel_id)
        msgs = list(self._buffers[key])
        buffer_blob = format_for_prompt(msgs)
        try:
            score, vibe, hook = await self.bot.claude.chipin_score(buffer_blob)
        except Exception as exc:
            emit(
                "error", source="chipin_score", error=type(exc).__name__,
                guild_id=guild_id, channel_id=channel_id,
            )
            return

        if vibe in SKIP_VIBES:
            emit(
                "chipin_evaluated", guild_id=guild_id, channel_id=channel_id,
                decision="vibe_gate", vibe=vibe, score=score,
            )
            return
        if score < THRESHOLD:
            emit(
                "chipin_evaluated", guild_id=guild_id, channel_id=channel_id,
                decision="threshold_gate", vibe=vibe, score=score,
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
        try:
            line = await self.bot.claude.chipin_post(
                buffer_blob, hook=hook, image_urls=image_urls,
            )
        except Exception as exc:
            emit(
                "error", source="chipin_post", error=type(exc).__name__,
                guild_id=guild_id, channel_id=channel_id,
            )
            return

        if not line.strip():
            emit(
                "chipin_evaluated", guild_id=guild_id, channel_id=channel_id,
                decision="empty_generation", vibe=vibe, score=score,
            )
            return

        try:
            await channel.send(line)
        except discord.DiscordException:
            log.exception("chip-in send failed for guild=%s channel=%s", guild_id, channel_id)
            return

        await self.bot.db.record_chipin(
            guild_id, channel_id, score=score, vibe=vibe, hook=hook,
        )
        emit(
            "chipin_posted",
            guild_id=guild_id, channel_id=channel_id,
            score=score, vibe=vibe, hook=hook[:200],
        )


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(ChipIn(cast("TootsiesBot", bot)))
