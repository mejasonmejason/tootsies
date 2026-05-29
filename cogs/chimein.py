"""Chime-in: Toots leans into the conversation when she has something to say.

No commands of its own. Wires into existing settings:

  - Listen channel: the guild's `discourse_channel` (set via /menu).
  - On/off + cadence: the mood schedule. mood=off silences, chill is reserved
    (5/day, 40min cooldown, 0.8 threshold), yaps is chatty (10/day, 20min
    cooldown, 0.6 threshold). Mirrors the 2:4 ratio scheduled discourse
    already uses.

Algorithm (also documented in docs/ALGORITHMS.md):
  - on_message in the discourse channel appends to a per-channel deque (max 50).
  - tasks.loop(seconds=60) walks each guild with buffered activity:
      * mood != off
      * Buffer has >= BUFFER_MIN_FOR_SCORE new messages since last evaluation
      * Outside cooldown (mood-tuned: 40min chill / 20min yaps)
      * Within hours window (9am to 2am ET)
      * Under daily cap (mood-tuned: 5 chill / 10 yaps)
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

import asyncio
import logging
from collections import defaultdict, deque
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, cast
from zoneinfo import ZoneInfo

import discord
from discord.ext import commands, tasks

from models import MoodMode
from utils.dedup import is_duplicate_of_recent
from utils.events import emit, emit_error
from utils.feeds import format_for_prompt, hot_urls, recent_image_urls
from utils.link_enrich import enrich_batch
from utils.markets import MarketSnapshot
from utils.permissions import can_send_in
from utils.perplexity import build_search_query
from utils.reactions import react
from utils.url_guardrail import extract_urls

if TYPE_CHECKING:
    from bot import TootsiesBot

log = logging.getLogger(__name__)


# ---- tunables (also listed in docs/ALGORITHMS.md) ---------------------------

ET = ZoneInfo("America/New_York")

# Buffer must have at least this many messages before we even score it. Below
# this, we don't have enough signal to know what the room's talking about.
BUFFER_MIN_FOR_SCORE = 5
CHIMEIN_QUALITY_THRESHOLD = 0.6

# Near-miss reaction band: when Toots can't (or won't) post but the buffer scores
# at or above this floor (and the vibe isn't a skip), she drops a single reaction
# instead of staying fully silent, the "I'm here, I clocked that" move. Reactions
# never post or consume the chimein post cooldown / daily cap; they ride their own
# light cooldown + a mood-tuned daily cap so she doesn't pepper the room. The
# reaction decision sits AFTER scoring, alongside the post decision, so it still
# fires during the post-cooldown / post-cap silent gaps it's meant to fill. The
# per-day reaction allowance is the mood's react_cap (chill 15 / yaps 30), set well
# above the post cap because a reaction is free (no API call, no clutter, no ping).
REACT_THRESHOLD = 0.45
REACT_COOLDOWN = timedelta(minutes=10)
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

    __slots__ = ("threshold", "daily_cap", "cooldown", "react_cap")

    def __init__(
        self, *, threshold: float, daily_cap: int, cooldown: timedelta, react_cap: int,
    ) -> None:
        self.threshold = threshold
        self.daily_cap = daily_cap
        self.cooldown = cooldown
        # Reactions are free (no API call, no channel clutter, no ping), so they
        # get a much higher daily allowance than posts, ~3x. Still mood-aware so
        # a reserved mood reacts less than a chatty one.
        self.react_cap = react_cap


# Mirrors the discourse scheduler's 2:4 chill:yaps post ratio. Chill is the
# reserved bartender, yaps is the one leaning across the bar.
MOOD_TUNING: dict[MoodMode, _MoodTuning] = {
    MoodMode.CHILL: _MoodTuning(
        threshold=0.8, daily_cap=5, cooldown=timedelta(minutes=40), react_cap=15,
    ),
    MoodMode.YAPS: _MoodTuning(
        threshold=0.6, daily_cap=10, cooldown=timedelta(minutes=20), react_cap=30,
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
        # (guild_id, channel_id) -> last reaction ATTEMPT time. In-memory and
        # purely a transient anti-hammer for the failure path (e.g. perms
        # revoked): durable cooldown + daily-cap pacing for SUCCESSFUL reactions
        # lives in the DB (chimein_reactions), so losing this on redeploy is
        # harmless (at most one extra attempt).
        self._last_react_attempt: dict[tuple[int, int], datetime] = {}
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

        # Post cooldown + daily cap are FLAGS, not early returns: a blocked post
        # shouldn't also block a reaction, which exists to fill exactly those
        # silent gaps. We only skip the (Haiku) scoring call when BOTH a post and
        # a reaction are off the table.
        key = (guild_id, channel_id)
        last_at = await self.bot.db.last_chimein_at(guild_id, channel_id)
        post_on_cooldown = (
            last_at is not None and datetime.now(UTC) - last_at < tuning.cooldown
        )
        post_capped = False
        count_today = 0
        if not post_on_cooldown:
            count_today = await self.bot.db.chimein_count_today(guild_id, channel_id)
            post_capped = count_today >= tuning.daily_cap
        post_blocked = post_on_cooldown or post_capped

        # Reaction eligibility is DB-backed (survives redeploys, like the post
        # cooldown). Check it lazily: only when posting is blocked (for the
        # cost-saving short-circuit) or when we later fall into the react branch,
        # never on the common about-to-post path.
        react_ok: bool | None = None
        if post_blocked:
            react_ok = await self._react_eligible(guild_id, channel_id, tuning.react_cap)
            if not react_ok:
                emit(
                    "chimein_evaluated", guild_id=guild_id, channel_id=channel_id,
                    decision="cooldown_gate" if post_on_cooldown else "daily_cap_gate",
                    count_today=count_today, mood=str(schedule.mood),
                )
                return

        # ---- Score the buffer --------------------------------------------------
        msgs = list(self._buffers[key])
        buffer_blob = format_for_prompt(msgs, include_reactions=True)
        # Numbered variant for the scorer only, so it can name a [#N] reaction
        # target. The post path keeps the un-numbered blob (no index noise).
        scored_blob = format_for_prompt(msgs, include_reactions=True, numbered=True)

        recent_all = await self.bot.db.recent_discourse_all(guild_id, limit=10)
        recent_posts = "\n".join(
            f"- [{ts.isoformat(timespec='minutes')}] ({cat}) {topic}"
            for cat, topic, ts in recent_all
        )

        try:
            score, vibe, hook, reaction, target = await self.bot.claude.chimein_score(
                scored_blob, recent_self_posts=recent_posts,
            )
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
        if post_blocked or score < tuning.threshold:
            # Can't post (cooldown/cap) or it's not post-worthy: react if it's a
            # near-miss-or-better, otherwise go dark. The reaction is the lighter
            # acknowledgement that fills the gap a post would have left.
            if react_ok is None:
                react_ok = await self._react_eligible(guild_id, channel_id, tuning.react_cap)
            reacted = await self._maybe_react(
                key, msgs, score, reaction, target, eligible=react_ok,
            )
            if reacted:
                decision = "reacted"
            elif post_on_cooldown:
                decision = "cooldown_gate"
            elif post_capped:
                decision = "daily_cap_gate"
            else:
                decision = "threshold_gate"
            emit(
                "chimein_evaluated", guild_id=guild_id, channel_id=channel_id,
                decision=decision, vibe=vibe, score=score, mood=str(schedule.mood),
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
        chime_hot_urls = hot_urls(msgs, limit=5)

        # Run link enrichment, Perplexity search, and markets fetch in parallel.
        # return_exceptions=True so one fetcher's outage can't cancel the others.
        coros: list[Any] = [enrich_batch(
            [u for u, _, _, _ in chime_hot_urls], concurrency=5,
        )]
        pplx_idx = -1
        pplx = self.bot.perplexity
        if pplx:
            pplx_idx = len(coros)
            coros.append(pplx.search(
                build_search_query(hook, surface="chimein"), purpose="chimein",
            ))
        # The Haiku classifier reads the hook string to decide whether the room
        # is talking about something market-grounded (game/parlay/election).
        markets_idx = len(coros)
        coros.append(self.bot.markets.get_context(hook))
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

        enriched = [v for v in enriched_map.values() if v is not None]

        recently_seen_urls = [
            u for msg in msgs for u in extract_urls(msg.content)
        ] if msgs else None

        try:
            line = await self.bot.claude.chimein_post(
                buffer_blob, hook=hook, image_urls=image_urls,
                enriched_links=enriched, recent_posts=recent_posts,
                perplexity_context=pplx_result,
                markets_context=markets_result,
                recently_seen_urls=recently_seen_urls,
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

        recent_summaries = [topic for _, topic, _ in recent_all]
        if is_duplicate_of_recent(line, recent_summaries):
            emit(
                "chimein_evaluated", guild_id=guild_id, channel_id=channel_id,
                decision="dedup_gate", vibe=vibe, score=score,
            )
            return

        try:
            quality_score, quality_reason = await self.bot.claude.discourse_score(
                line, channel_name=channel.name, surface="chimein",
            )
        except Exception as exc:
            emit_error(
                source="chimein_quality_score", exc=exc, recoverable=True,
                guild_id=guild_id, channel_id=channel_id,
            )
            quality_score, quality_reason = 1.0, "score_failed_pass_through"

        if quality_score < CHIMEIN_QUALITY_THRESHOLD:
            emit(
                "chimein_evaluated", guild_id=guild_id, channel_id=channel_id,
                decision="quality_gate", vibe=vibe, score=score,
                quality_score=quality_score, quality_reason=quality_reason,
                post_preview=line[:120],
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
        await self.bot.db.add_discourse(guild_id, "open", line[:200])
        emit(
            "chimein_posted",
            guild_id=guild_id, channel_id=channel_id,
            score=score, vibe=vibe, hook=hook[:200],
            mood=str(schedule.mood),
            quality_score=quality_score, quality_reason=quality_reason,
        )


    async def _react_eligible(
        self, guild_id: int, channel_id: int, daily_cap: int,
    ) -> bool:
        """DB-backed gate: react cooldown elapsed AND daily cap not hit.

        Durable across redeploys (unlike a purely in-memory timer), mirroring the
        post cooldown/cap. `daily_cap` is the mood's cap (chill 5 / yaps 10), so
        reactions pace with the same mood cadence as posts. Counts only
        successful reactions (rows in chimein_reactions).
        """
        last = await self.bot.db.last_reaction_at(guild_id, channel_id)
        if last is not None and datetime.now(UTC) - last < REACT_COOLDOWN:
            return False
        return await self.bot.db.reaction_count_today(guild_id, channel_id) < daily_cap

    @staticmethod
    def _pick_react_emoji(
        message: discord.Message, suggested: str,
    ) -> str | discord.Emoji | discord.PartialEmoji:
        """The emoji to react with, deterministically (no random fallback).

          1. Co-sign the room: if the message already carries reactions, pile
             onto the most-used one (highest count; ties resolve to the
             first-added, since Discord returns reactions in insertion order and
             max() keeps the first on ties).
          2. Otherwise the scorer's `suggested` stance emoji (🔥 cosign vs 🧢 cap
             aren't interchangeable).

        Returns "" when the message is bare AND the scorer named no emoji, so the
        caller stays silent rather than inventing a reaction.
        """
        if message.reactions:
            return max(message.reactions, key=lambda r: r.count).emoji
        return suggested

    async def _maybe_react(
        self,
        key: tuple[int, int],
        msgs: list[discord.Message],
        score: float,
        suggested: str,
        target_index: int | None,
        *,
        eligible: bool,
    ) -> bool:
        """React to the specific message the scorer picked, on a near-miss-or-better score.

        Cheap path: no Claude call, no post, no chimein post-cooldown/cap
        consumption. `eligible` is the DB-backed cooldown/daily-cap result.
        `msgs` is the same snapshot the scorer saw (same order as the numbered
        buffer it read), so `target_index` maps straight back to the message the
        scorer aimed its reaction at.

        Fully scorer-driven, no randomness: if the scorer named neither a target
        message nor an emoji, Toots stays silent. A named emoji with a missing /
        out-of-range index falls back to the most recent message.
        """
        if score < REACT_THRESHOLD or not eligible or not msgs:
            return False
        # Transient anti-hammer for the FAILURE path: don't re-attempt within
        # REACT_COOLDOWN even if the last attempt failed (e.g. perms revoked).
        # Successful reactions are paced durably by the DB cooldown above; this
        # in-memory guard only matters when nothing got recorded, so losing it on
        # redeploy is harmless.
        last_attempt = self._last_react_attempt.get(key)
        if last_attempt is not None and datetime.now(UTC) - last_attempt < REACT_COOLDOWN:
            return False
        if target_index is not None and 0 <= target_index < len(msgs):
            target = msgs[target_index]
        elif suggested:
            target = msgs[-1]  # emoji chosen but no usable index: land on the freshest line
        else:
            return False  # scorer chose nothing to react to: stay silent
        emoji = self._pick_react_emoji(target, suggested)
        if not emoji:
            return False
        self._last_react_attempt[key] = datetime.now(UTC)
        if not await react(target, emoji, source="chimein"):
            return False
        await self.bot.db.record_reaction(key[0], key[1], target.id, str(emoji))
        return True


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(ChimeIn(cast("TootsiesBot", bot)))
