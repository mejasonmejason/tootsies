"""Long-term memory: the twice-daily writer + weekly rollup, plus /forget.

Toots keeps a distilled, attributed memory of what happens in a guild's
discourse channels so /ask and @mentions can do callbacks and she knows her
regulars. The writer runs twice a day (ET), summarizing the last ~12h of
discourse-channel activity into one half-day note. A weekly rollup compacts the
week's half-day notes into a single weekly note and deletes the rolled-up
halfdays, so the store stays bounded (the decay pyramid):

    halfday notes  --(Sunday rollup: compact + delete)-->  weekly note

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

# Twice daily, in ET. Offset from the discourse posting slots (12:00 / 19:00) so
# the memory Haiku calls don't pile onto the discourse/music Sonnet burst.
MEMORY_TIMES = [time(4, 0), time(16, 0)]
# Min gap between half-day writes: just under 12h so a slot can't double-fire,
# but the second daily slot still clears it.
_HALFDAY_MIN_GAP = timedelta(hours=11)
# How far back each half-day note looks.
_HALFDAY_WINDOW = timedelta(hours=12)

# Weekly rollup: Sunday (Mon=0 .. Sun=6) at 05:00 ET, after the early slot.
MEMORY_WEEKLY_WEEKDAY = 6
MEMORY_WEEKLY_TIME = time(5, 0)
_WEEKLY_MIN_GAP = timedelta(days=6)
# Roll up half-day notes from a little over a week back (covers a missed Sunday).
_WEEKLY_LOOKBACK = timedelta(days=8)

# Below this many messages across all discourse channels in the window, the
# window is "dead", skip the write rather than store a note about nothing.
ACTIVITY_THRESHOLD = 5
# Per-channel history pull cap for the gather pass.
PER_CHANNEL_LIMIT = 60

# Stored-note length ceilings (defense in depth; the prompt already aims short).
_HALFDAY_MAX_CHARS = 2000
_WEEKLY_MAX_CHARS = 3000

# Spread per-guild work across this window so many guilds don't fire at once.
_TICK_JITTER_MAX_SECONDS = 20.0


def halfday_due(now_et: datetime, last_attempt: datetime | None) -> bool:
    """Is a half-day write due? True when we're past one of today's scheduled
    times AND it's been at least _HALFDAY_MIN_GAP since the last attempt.

    `last_attempt` is the later of the last written note's span_end and the last
    in-process attempt (so a skipped low-activity window still advances the
    gate and we don't re-gather every tick). Both args are tz-aware; comparing
    aware datetimes across zones is correct.
    """
    if not any(t <= now_et.time() for t in MEMORY_TIMES):
        return False
    if last_attempt is None:
        return True
    return (now_et - last_attempt) >= _HALFDAY_MIN_GAP


def weekly_due(now_et: datetime, last_attempt: datetime | None) -> bool:
    """Is a weekly rollup due? Only on the rollup weekday, at/after the rollup
    time, and at least _WEEKLY_MIN_GAP since the last attempt."""
    if now_et.weekday() != MEMORY_WEEKLY_WEEKDAY:
        return False
    if now_et.time() < MEMORY_WEEKLY_TIME:
        return False
    if last_attempt is None:
        return True
    return (now_et - last_attempt) >= _WEEKLY_MIN_GAP


def _is_empty(text: str) -> bool:
    return not text or text.strip().upper() == "EMPTY"


class Memory(commands.Cog):
    def __init__(self, bot: TootsiesBot) -> None:
        self.bot = bot
        # In-process "last attempt" markers so a skipped (low-activity / empty)
        # window still advances the cadence gate without writing a sentinel note
        # to the DB. Reset on restart, at most one extra attempt after a boot.
        self._last_halfday_attempt: dict[int, datetime] = {}
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

    @tasks.loop(minutes=20)
    async def scheduler_tick(self) -> None:
        try:
            now_et = datetime.now(ET)
            for guild_id in await self.bot.db.all_configured_guilds():
                # Small per-guild jitter so writes spread out instead of bursting.
                await asyncio.sleep(random.uniform(0, _TICK_JITTER_MAX_SECONDS))
                try:
                    await self._maybe_write_halfday(guild_id, now_et)
                    await self._maybe_rollup_weekly(guild_id, now_et)
                except Exception:
                    log.exception("memory tick failed for guild %s", guild_id)
        except Exception:
            log.exception("memory scheduler tick failed")

    @scheduler_tick.before_loop
    async def before_tick(self) -> None:
        await self.bot.wait_until_ready()

    def _effective_last(
        self, guild_id: int, note_at: datetime | None, attempts: dict[int, datetime],
    ) -> datetime | None:
        """Later of the last written note's span_end and the in-process attempt."""
        attempt = attempts.get(guild_id)
        if note_at is None:
            return attempt
        if attempt is None:
            return note_at
        return max(note_at, attempt)

    async def _maybe_write_halfday(self, guild_id: int, now_et: datetime) -> None:
        note_at = await self.bot.db.last_memory_note_at(guild_id, "halfday")
        last = self._effective_last(guild_id, note_at, self._last_halfday_attempt)
        if not halfday_due(now_et, last):
            return
        # Mark the attempt up front so a mid-run failure doesn't loop every tick.
        self._last_halfday_attempt[guild_id] = now_et

        guild = self.bot.get_guild(guild_id)
        if guild is None or guild.me is None:
            return
        channel_ids = await self.bot.db.get_discourse_channels(guild_id)
        if not channel_ids:
            return

        blob, msg_count, channel_count = await self._gather(
            guild, channel_ids, guild.me, _HALFDAY_WINDOW
        )
        span_end = datetime.now(UTC)
        span_start = span_end - _HALFDAY_WINDOW

        if msg_count < ACTIVITY_THRESHOLD:
            emit(
                "memory_write", guild_id=guild_id, tier="halfday", ok=True,
                skipped="low_activity", message_count=msg_count,
                channel_count=channel_count,
            )
            return

        forgotten = await self.bot.db.forgotten_names(guild_id)
        note = await self.bot.claude.memory_note(blob, forgotten_names=forgotten)
        if _is_empty(note):
            emit(
                "memory_write", guild_id=guild_id, tier="halfday", ok=True,
                skipped="empty", message_count=msg_count,
                channel_count=channel_count,
            )
            return

        note = note[:_HALFDAY_MAX_CHARS]
        await self.bot.db.add_memory_note(
            guild_id, "halfday", note, span_start, span_end
        )
        emit(
            "memory_write", guild_id=guild_id, tier="halfday", ok=True,
            chars=len(note), message_count=msg_count, channel_count=channel_count,
        )

    async def _maybe_rollup_weekly(self, guild_id: int, now_et: datetime) -> None:
        note_at = await self.bot.db.last_memory_note_at(guild_id, "weekly")
        last = self._effective_last(guild_id, note_at, self._last_weekly_attempt)
        if not weekly_due(now_et, last):
            return
        self._last_weekly_attempt[guild_id] = now_et

        since = datetime.now(UTC) - _WEEKLY_LOOKBACK
        halfdays = await self.bot.db.memory_notes_since(guild_id, "halfday", since)
        if not halfdays:
            return

        notes_blob = "\n\n".join(summary for _, summary, _, _ in halfdays)
        ids = [note_id for note_id, _, _, _ in halfdays]
        span_start = halfdays[0][2]
        span_end = halfdays[-1][3]

        forgotten = await self.bot.db.forgotten_names(guild_id)
        weekly = await self.bot.claude.memory_rollup(
            notes_blob, forgotten_names=forgotten
        )
        if not _is_empty(weekly):
            await self.bot.db.add_memory_note(
                guild_id, "weekly", weekly[:_WEEKLY_MAX_CHARS], span_start, span_end
            )
            emit(
                "memory_write", guild_id=guild_id, tier="weekly", ok=True,
                chars=len(weekly), rolled_up=len(ids),
            )
        else:
            emit(
                "memory_write", guild_id=guild_id, tier="weekly", ok=True,
                skipped="empty", rolled_up=len(ids),
            )
        # Delete the rolled-up halfdays either way: they've been considered, and
        # keeping them would double-count into next week's rollup.
        await self.bot.db.delete_memory_notes(ids)

    async def _gather(
        self,
        guild: discord.Guild,
        channel_ids: list[int],
        me: discord.Member,
        within: timedelta,
    ) -> tuple[str, int, int]:
        """Gather recent human activity across the discourse channels into one
        prompt blob. Returns (blob, total_message_count, channels_with_activity).
        include_bots=False: memory is about what PEOPLE did, not webhook posts.
        """
        blocks: list[str] = []
        total = 0
        used = 0
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
            reactors = await resolve_reactors(msgs)
            rendered = format_for_prompt(msgs, include_reactions=True, reactors=reactors)
            blocks.append(f"#{channel.name}:\n{rendered}")
            total += len(msgs)
            used += 1
        return "\n\n".join(blocks), total, used


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Memory(cast("TootsiesBot", bot)))
