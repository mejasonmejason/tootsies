"""/chipin: Toots leans into the conversation when she has something to say.

Three mod commands:
  /chipin enable   turn chip-in on for the channel where it's run
  /chipin disable  turn it off for the channel where it's run
  /chipin status   show every channel where chip-in is on + today's stats

Algorithm (also documented in docs/ALGORITHMS.md):
  - on_message in any enabled channel appends to a per-channel deque (max 50).
  - tasks.loop(seconds=60) walks each enabled channel:
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
from discord import app_commands
from discord.ext import commands, tasks

from utils import voice
from utils.events import emit
from utils.feeds import format_for_prompt, recent_image_urls
from utils.metrics import track_command
from utils.permissions import can_send_in, is_mod

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


# ---- cog --------------------------------------------------------------------


class ChipIn(commands.GroupCog, name="chipin"):
    """/chipin enable | disable | status, plus the background listener + scorer."""

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
        super().__init__()
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
            # (Discourse already pulls feed content via its own path.)
            return
        # Pure-attachment messages get added too (vision picks them up).
        if not message.content.strip() and not message.attachments and not message.embeds:
            return
        # Cheap path: only buffer if the channel is in our listen set. Hitting
        # the DB on every message would be wasteful, so we cache the listen set
        # on the cog and refresh it via on_chipin_change (called by the /chipin
        # commands below) instead of polling.
        if (message.guild.id, message.channel.id) not in self._listen_set_cache():
            return
        key = (message.guild.id, message.channel.id)
        self._buffers[key].append(message)
        self._new_since_eval[key] += 1

    # ---- /chipin commands -------------------------------------------------------

    @app_commands.command(
        name="enable",
        description="turn chip-in on for this channel. mods only.",
    )
    @track_command("chipin enable")
    async def enable(self, interaction: discord.Interaction) -> None:
        if not await self._mod_gate(interaction):
            return
        assert interaction.guild is not None
        if not isinstance(interaction.channel, discord.TextChannel | discord.Thread):
            await interaction.response.send_message(
                "can't do this in here. text channel only.", ephemeral=True,
            )
            return
        await self.bot.db.enable_chipin(
            interaction.guild.id, interaction.channel.id, interaction.user.id,
        )
        await self.bot.db.audit(
            interaction.guild.id, interaction.user.id, "chipin_enable",
            after={"channel_id": interaction.channel.id},
        )
        self._invalidate_listen_cache()
        await interaction.response.send_message(
            f"chip-in on for <#{interaction.channel.id}>. i'll lean in when "
            "i've got something. you can `/chipin disable` anytime.",
        )

    @app_commands.command(
        name="disable",
        description="turn chip-in off for this channel. mods only.",
    )
    @track_command("chipin disable")
    async def disable(self, interaction: discord.Interaction) -> None:
        if not await self._mod_gate(interaction):
            return
        assert interaction.guild is not None
        if not isinstance(interaction.channel, discord.TextChannel | discord.Thread):
            await interaction.response.send_message(
                "can't do this in here.", ephemeral=True,
            )
            return
        await self.bot.db.disable_chipin(
            interaction.guild.id, interaction.channel.id,
        )
        await self.bot.db.audit(
            interaction.guild.id, interaction.user.id, "chipin_disable",
            after={"channel_id": interaction.channel.id},
        )
        # Drop the buffer for this channel; saves memory + means we don't fire
        # on backlog if re-enabled later.
        self._buffers.pop((interaction.guild.id, interaction.channel.id), None)
        self._new_since_eval.pop((interaction.guild.id, interaction.channel.id), None)
        self._invalidate_listen_cache()
        await interaction.response.send_message(
            f"chip-in off for <#{interaction.channel.id}>. silent here.",
        )

    @app_commands.command(
        name="status",
        description="see which channels have chip-in on + today's chip-in count.",
    )
    @track_command("chipin status")
    async def status(self, interaction: discord.Interaction) -> None:
        if not await self._mod_gate(interaction):
            return
        assert interaction.guild is not None
        channel_ids = await self.bot.db.chipin_channels(interaction.guild.id)
        if not channel_ids:
            await interaction.response.send_message(
                "chip-in's off everywhere. run `/chipin enable` in a channel to turn it on.",
                ephemeral=True,
            )
            return
        lines = []
        for cid in channel_ids:
            count_today = await self.bot.db.chipin_count_today(interaction.guild.id, cid)
            lines.append(
                f"<#{cid}>: **{count_today}/{DAILY_CAP}** chip-ins today"
            )
        await interaction.response.send_message(
            "chip-in on:\n" + "\n".join(lines), ephemeral=True,
        )

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

    async def _maybe_chip_in_all(self) -> None:
        """Walk every (guild, channel) with buffered activity and try to chip in."""
        # Refresh listen set so newly enabled channels start being checked.
        listen_pairs = await self.bot.db.all_chipin_channels()
        listen_set = set(listen_pairs)
        self._cached_listen_set = listen_set

        for key in list(self._buffers.keys()):
            if key not in listen_set:
                continue  # listen was disabled since the buffer accumulated
            if self._new_since_eval[key] < BUFFER_MIN_FOR_SCORE:
                continue
            guild_id, channel_id = key
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

    # ---- helpers ----------------------------------------------------------------

    _cached_listen_set: set[tuple[int, int]] | None = None

    def _listen_set_cache(self) -> set[tuple[int, int]]:
        """In-process cache of (guild, channel) pairs where chip-in is enabled.

        Refreshed by the tick loop each minute. Lets on_message skip the DB on
        every message: at any reasonable message rate this matters.
        """
        return self._cached_listen_set or set()

    def _invalidate_listen_cache(self) -> None:
        """Drop the cache so the next tick reloads from DB. Called by enable/disable
        commands so toggles take effect within a tick instead of at next minute."""
        self._cached_listen_set = None

    async def _mod_gate(self, interaction: discord.Interaction) -> bool:
        member = interaction.user
        if not isinstance(member, discord.Member):
            await interaction.response.send_message(
                voice.pick(voice.PERMISSION_DENIED), ephemeral=True,
            )
            return False
        if not await is_mod(self.bot.db, member):
            await interaction.response.send_message(
                voice.pick(voice.PERMISSION_DENIED), ephemeral=True,
            )
            return False
        return True


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(ChipIn(cast("TootsiesBot", bot)))
