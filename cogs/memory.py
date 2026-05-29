"""Long-term memory: the hourly writer + daily + weekly rollups, plus /forget.

Toots keeps a distilled, attributed memory of what happens in a guild's
discourse channels so /ask and @mentions can do callbacks and she knows her
regulars. The writer runs hourly, summarizing the last hour of discourse-channel
activity (up to 200 msgs/channel, same fetch shape as /recap) into one `hourly`
note. Rollups compact the pyramid so the store stays bounded and recall stays
useful at every horizon (the decay pyramid):

    hourly notes ──(daily rollup: compact + delete)──▶ daily note
    daily notes  ──(weekly rollup: compact + delete)──▶ weekly note

A 200-msg/1h fetch keeps each hourly note honest (the window is fully covered,
not just the tail). /ask reads a mix back: the last few hourly notes (sharp
recent recall), a couple daily notes (this week), and the weekly note (the
long arc).

What gets recorded is fenced hard (see claude_client._MEMORY_FENCE): observed
public behavior only, never inferred private traits, no transcripts. That fence
is what keeps attributed "who did what" memory inside the constitution, so it is
NOT tunable via /order.

/forget is a self-service privacy right: any user can wipe themselves from
Toots's memory. No parameter, you can only forget yourself, never someone else.
"""

from __future__ import annotations

import asyncio
import logging
import random
from datetime import UTC, datetime, time, timedelta
from typing import TYPE_CHECKING, cast
from zoneinfo import ZoneInfo

import discord
from discord import app_commands
from discord.ext import commands, tasks

from utils import voice
from utils.events import emit, emit_error
from utils.feeds import format_for_prompt, recent_messages, resolve_reactors
from utils.metrics import track_command
from utils.permissions import can_read

if TYPE_CHECKING:
    from bot import TootsiesBot

log = logging.getLogger(__name__)

ET = ZoneInfo("America/New_York")

# The scheduler ticks this often; each tier's gate decides when it actually
# fires. 10 min keeps the hourly write landing close to the top of each hour.
_TICK_MINUTES = 10

# ---- hourly tier ------------------------------------------------------------
# Min elapsed since the last hourly write before another fires. Just under an
# hour so the ~10-min tick lands it ~hourly without drifting late.
_HOURLY_MIN_GAP = timedelta(minutes=55)
# Fetch shape, matches /recap: up to 200 msgs per channel over the window.
PER_CHANNEL_LIMIT = 200
# The window adapts to the actual gap since the last hourly note (so writes
# tile with no gap even if a tick was missed), floored at the nominal hour and
# capped so a long outage doesn't pull an unbounded history.
_HOURLY_WINDOW_DEFAULT = timedelta(hours=1)
_HOURLY_WINDOW_MAX = timedelta(hours=3)
# Below this many messages across all discourse channels in the hour, the hour
# is "dead", skip the write rather than store a note about nothing.
ACTIVITY_THRESHOLD = 5

# ---- daily tier -------------------------------------------------------------
# Daily rollup runs once a day at this ET time, compacting the day's hourly
# notes into one daily note.
DAILY_ROLLUP_TIME = time(5, 0)
_DAILY_MIN_GAP = timedelta(hours=20)
_DAILY_LOOKBACK = timedelta(hours=30)  # covers a missed run

# ---- weekly tier ------------------------------------------------------------
# Weekly rollup runs Sunday (Mon=0 .. Sun=6) at this ET time, after the daily.
WEEKLY_ROLLUP_WEEKDAY = 6
WEEKLY_ROLLUP_TIME = time(5, 30)
_WEEKLY_MIN_GAP = timedelta(days=6)
_WEEKLY_LOOKBACK = timedelta(days=9)  # covers a missed Sunday

# Stored-note length ceilings (defense in depth; the prompt already aims short).
_NOTE_MAX_CHARS = 2000
_ROLLUP_MAX_CHARS = 3000

# Spread per-guild API calls across this window so many guilds don't burst.
_API_JITTER_MAX_SECONDS = 20.0


def hourly_due(now: datetime, last_attempt: datetime | None) -> bool:
    """An hourly write is due once it's been at least _HOURLY_MIN_GAP since the
    last attempt. `now` and `last_attempt` are tz-aware; comparing aware
    datetimes across zones is correct."""
    if last_attempt is None:
        return True
    return (now - last_attempt) >= _HOURLY_MIN_GAP


def daily_due(now_et: datetime, last_attempt: datetime | None) -> bool:
    """A daily rollup is due at/after DAILY_ROLLUP_TIME, at most once per
    _DAILY_MIN_GAP."""
    if now_et.time() < DAILY_ROLLUP_TIME:
        return False
    if last_attempt is None:
        return True
    return (now_et - last_attempt) >= _DAILY_MIN_GAP


def weekly_due(now_et: datetime, last_attempt: datetime | None) -> bool:
    """A weekly rollup is due on the rollup weekday at/after WEEKLY_ROLLUP_TIME,
    at most once per _WEEKLY_MIN_GAP."""
    if now_et.weekday() != WEEKLY_ROLLUP_WEEKDAY:
        return False
    if now_et.time() < WEEKLY_ROLLUP_TIME:
        return False
    if last_attempt is None:
        return True
    return (now_et - last_attempt) >= _WEEKLY_MIN_GAP


def hourly_window(now: datetime, last_note_at: datetime | None) -> timedelta:
    """How far back the hourly write looks: the gap since the last hourly note
    (so windows tile without gaps), floored at the nominal hour and capped so a
    long outage can't pull an unbounded history."""
    if last_note_at is None:
        return _HOURLY_WINDOW_DEFAULT
    gap = now - last_note_at
    if gap < _HOURLY_WINDOW_DEFAULT:
        return _HOURLY_WINDOW_DEFAULT
    return min(gap, _HOURLY_WINDOW_MAX)


def _is_empty(text: str) -> bool:
    return not text or text.strip().upper() == "EMPTY"


class Memory(commands.Cog):
    def __init__(self, bot: TootsiesBot) -> None:
        self.bot = bot
        # In-process "last attempt" markers per tier so a skipped (low-activity /
        # empty) window still advances the cadence gate without writing a
        # sentinel row. Reset on restart, at most one extra attempt after a boot.
        self._last_hourly_attempt: dict[int, datetime] = {}
        self._last_daily_attempt: dict[int, datetime] = {}
        self._last_weekly_attempt: dict[int, datetime] = {}
        self.scheduler_tick.start()

    async def cog_unload(self) -> None:
        self.scheduler_tick.cancel()

    # ---- /forget ----------------------------------------------------------------

    @app_commands.command(
        name="forget",
        description="wipe yourself from my memory (you can only forget yourself)",
    )
    @track_command("forget")
    async def forget(self, interaction: discord.Interaction) -> None:
        if interaction.guild_id is None:
            await interaction.response.send_message(
                "we're in a dm, i'm not keeping notes here anyway.", ephemeral=True
            )
            return
        await interaction.response.defer(ephemeral=True)
        name = interaction.user.display_name
        try:
            deleted = await self.bot.db.forget_user(
                interaction.guild_id, interaction.user.id, name
            )
        except Exception as exc:
            log.exception("forget failed")
            emit_error(
                source="forget", exc=exc, recoverable=False,
                guild_id=interaction.guild_id, user_id=interaction.user.id,
            )
            await interaction.followup.send(voice.pick(voice.DB_ERROR), ephemeral=True)
            return
        emit(
            "memory_forget",
            guild_id=interaction.guild_id, user_id=interaction.user.id,
            notes_deleted=deleted,
        )
        await interaction.followup.send(
            "done. wiped you from my memory and i won't keep notes on you going "
            "forward. clean slate.",
            ephemeral=True,
        )

    # ---- scheduler --------------------------------------------------------------

    @tasks.loop(minutes=_TICK_MINUTES)
    async def scheduler_tick(self) -> None:
        try:
            now_utc = datetime.now(UTC)
            now_et = now_utc.astimezone(ET)
            for guild_id in await self.bot.db.all_configured_guilds():
                try:
                    await self._maybe_write_hourly(guild_id, now_utc)
                    await self._maybe_rollup(guild_id, now_et, "daily")
                    await self._maybe_rollup(guild_id, now_et, "weekly")
                except Exception:
                    log.exception("memory tick failed for guild %s", guild_id)
        except Exception:
            log.exception("memory scheduler tick failed")

    @scheduler_tick.before_loop
    async def before_tick(self) -> None:
        await self.bot.wait_until_ready()

    @staticmethod
    def _effective_last(
        note_at: datetime | None, attempt: datetime | None,
    ) -> datetime | None:
        """Later of the last written note's span_end and the in-process attempt."""
        if note_at is None:
            return attempt
        if attempt is None:
            return note_at
        return max(note_at, attempt)

    async def _maybe_write_hourly(self, guild_id: int, now_utc: datetime) -> None:
        note_at = await self.bot.db.last_memory_note_at(guild_id, "hourly")
        last = self._effective_last(note_at, self._last_hourly_attempt.get(guild_id))
        if not hourly_due(now_utc, last):
            return
        # Mark the attempt up front so a mid-run failure doesn't loop every tick.
        self._last_hourly_attempt[guild_id] = now_utc

        guild = self.bot.get_guild(guild_id)
        if guild is None or guild.me is None:
            return
        channel_ids = await self.bot.db.get_discourse_channels(guild_id)
        if not channel_ids:
            return

        window = hourly_window(now_utc, note_at)
        # Collect first (history reads only, needed to count), gate on activity,
        # THEN do the pricier reactor resolution + API call, so a dead hour costs
        # nothing past the history reads.
        collected = await self._collect(guild, channel_ids, guild.me, window)
        msg_count = sum(len(msgs) for _, msgs in collected)
        channel_count = len(collected)
        span_end = datetime.now(UTC)
        span_start = span_end - window

        if msg_count < ACTIVITY_THRESHOLD:
            emit(
                "memory_write", guild_id=guild_id, tier="hourly", ok=True,
                skipped="low_activity", message_count=msg_count,
                channel_count=channel_count,
            )
            return

        blob = await self._render(collected)
        forgotten = await self.bot.db.forgotten_names(guild_id)
        await asyncio.sleep(random.uniform(0, _API_JITTER_MAX_SECONDS))
        note = await self.bot.claude.memory_note(blob, forgotten_names=forgotten)
        if _is_empty(note):
            emit(
                "memory_write", guild_id=guild_id, tier="hourly", ok=True,
                skipped="empty", message_count=msg_count,
                channel_count=channel_count,
            )
            return

        note = note[:_NOTE_MAX_CHARS]
        await self.bot.db.add_memory_note(
            guild_id, "hourly", note, span_start, span_end
        )
        emit(
            "memory_write", guild_id=guild_id, tier="hourly", ok=True,
            chars=len(note), message_count=msg_count, channel_count=channel_count,
        )

    async def _maybe_rollup(self, guild_id: int, now_et: datetime, period: str) -> None:
        """Daily/weekly rollup: compact the tier below into one note and delete
        the rolled-up notes. period='daily' rolls hourly->daily;
        period='weekly' rolls daily->weekly."""
        if period == "daily":
            lower, due_fn, attempts, lookback = (
                "hourly", daily_due, self._last_daily_attempt, _DAILY_LOOKBACK,
            )
        else:
            lower, due_fn, attempts, lookback = (
                "daily", weekly_due, self._last_weekly_attempt, _WEEKLY_LOOKBACK,
            )

        note_at = await self.bot.db.last_memory_note_at(guild_id, period)
        last = self._effective_last(note_at, attempts.get(guild_id))
        if not due_fn(now_et, last):
            return
        attempts[guild_id] = now_et

        since = datetime.now(UTC) - lookback
        lowers = await self.bot.db.memory_notes_since(guild_id, lower, since)
        if not lowers:
            return

        notes_blob = "\n\n".join(summary for _, summary, _, _ in lowers)
        ids = [note_id for note_id, _, _, _ in lowers]
        span_start = lowers[0][2]
        span_end = lowers[-1][3]

        forgotten = await self.bot.db.forgotten_names(guild_id)
        await asyncio.sleep(random.uniform(0, _API_JITTER_MAX_SECONDS))
        rolled = await self.bot.claude.memory_rollup(
            notes_blob, period=period, forgotten_names=forgotten
        )
        if not _is_empty(rolled):
            await self.bot.db.add_memory_note(
                guild_id, period, rolled[:_ROLLUP_MAX_CHARS], span_start, span_end
            )
            emit(
                "memory_write", guild_id=guild_id, tier=period, ok=True,
                chars=len(rolled), rolled_up=len(ids),
            )
        else:
            emit(
                "memory_write", guild_id=guild_id, tier=period, ok=True,
                skipped="empty", rolled_up=len(ids),
            )
        # Delete the rolled-up notes either way: they've been considered, and
        # keeping them would double-count into the next rollup.
        await self.bot.db.delete_memory_notes(ids)

    async def _collect(
        self,
        guild: discord.Guild,
        channel_ids: list[int],
        me: discord.Member,
        within: timedelta,
    ) -> list[tuple[discord.TextChannel | discord.Thread, list[discord.Message]]]:
        """Read recent human activity per discourse channel (history reads only,
        no reaction resolution yet). include_bots=False: memory is about what
        PEOPLE did, not webhook posts. Channels with no messages are dropped.
        """
        out: list[tuple[discord.TextChannel | discord.Thread, list[discord.Message]]] = []
        for cid in channel_ids:
            channel = guild.get_channel(cid)
            if not isinstance(channel, discord.TextChannel | discord.Thread):
                continue
            if not can_read(channel, me):
                continue
            msgs = await recent_messages(
                channel, me, limit=PER_CHANNEL_LIMIT, within=within,
                include_bots=False,
            )
            if not msgs:
                continue
            out.append((channel, msgs))
        return out

    @staticmethod
    async def _render(
        collected: list[tuple[discord.TextChannel | discord.Thread, list[discord.Message]]],
    ) -> str:
        """Render collected messages into one prompt blob, resolving reactors
        (paginated API calls) here, only reached once the activity gate passes.
        Reactions signal what the room actually cared about, so they're worth it.
        """
        blocks: list[str] = []
        for channel, msgs in collected:
            reactors = await resolve_reactors(msgs)
            rendered = format_for_prompt(msgs, include_reactions=True, reactors=reactors)
            blocks.append(f"#{channel.name}:\n{rendered}")
        return "\n\n".join(blocks)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Memory(cast("TootsiesBot", bot)))
