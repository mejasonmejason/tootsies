"""/ask <question> + summon handler (@Toots mentions and direct replies to her).

A direct reply to one of Toots' own messages summons her with no @-mention
needed; an explicit mention works too. Both share the /ask counter so heavy
users can't escape the 20/day cap by swapping interfaces.
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import TYPE_CHECKING, Any, cast

import discord
from discord import app_commands
from discord.ext import commands

from utils import abuse_tracker, bot_logs, voice
from utils.events import emit, emit_error
from utils.feeds import _strip_html, format_for_prompt, recent_image_urls, recent_messages
from utils.gates import require_configured
from utils.link_enrich import enrich_batch
from utils.markets import MarketSnapshot
from utils.metrics import track_command
from utils.perplexity import build_search_query
from utils.rate_limits import check_user_limit, consume_user
from utils.url_guardrail import extract_urls

if TYPE_CHECKING:
    from bot import TootsiesBot

log = logging.getLogger(__name__)


def _format_memory_hits(
    hits: list[tuple[str, str, datetime, datetime]],
) -> str:
    """Render search_memory hits for the model: tier + date range + the note.
    Empty result reads as a plain 'nothing recalled' so the model answers
    naturally instead of narrating a failed search."""
    if not hits:
        return "no memories match that, nothing specific comes to mind."
    lines = []
    for tier, summary, span_start, span_end in hits:
        when = (
            f"{span_start:%b %d}"
            if span_start.date() == span_end.date()
            else f"{span_start:%b %d}-{span_end:%b %d}"
        )
        lines.append(f"[{tier} | {when}] {summary}")
    return "\n".join(lines)


def _reply_quote(message: discord.Message, me_id: int) -> str | None:
    """If `message` is a direct reply to one of *our own* messages, return that
    message's text (possibly empty). Otherwise None.

    A reply to Toots is treated as a "talking to you" signal, no explicit
    @-mention required, and an even clearer one than a ping buried mid-sentence.
    We rely on the gateway-resolved referenced message (Discord includes it in
    the reply payload) rather than fetching, to avoid an API round-trip on every
    reply in the server. Replies whose target is deleted or uncached resolve to
    None and fall through to the normal mention gate.
    """
    ref = message.reference
    if ref is None:
        return None
    resolved = getattr(ref, "resolved", None)
    # A live Message has .author; a DeletedReferencedMessage does not, so this
    # also filters out replies to since-deleted messages.
    author = getattr(resolved, "author", None)
    if author is None or getattr(author, "id", None) != me_id:
        return None
    return getattr(resolved, "content", "") or ""


class Ask(commands.Cog):
    def __init__(self, bot: TootsiesBot) -> None:
        self.bot = bot

    @app_commands.command(name="ask", description="ask toots something.")
    @app_commands.describe(question="what do you want to know?")
    @track_command("ask")
    async def ask(self, interaction: discord.Interaction, question: str) -> None:
        if not await require_configured(interaction, self.bot.db):
            return

        guild_id = interaction.guild_id
        user_id = interaction.user.id
        assert guild_id is not None

        # Silenced users get a short ephemeral brush-off; no Claude call, no rate-limit hit.
        if await abuse_tracker.is_silenced(self.bot.db, guild_id, user_id):
            await interaction.response.send_message(voice.pick(voice.ABUSE_SILENCED), ephemeral=True)
            return

        # Rate limit check first, fail fast before doing work.
        try:
            allowed, _, _ = await check_user_limit(self.bot.db, user_id, guild_id, "ask")
        except Exception as exc:  # fail-open per plan: better to answer than to go silent
            log.exception("rate limit check failed, failing open: %s", exc)
            allowed = True
        if not allowed:
            await interaction.response.send_message(voice.pick(voice.RATE_LIMIT_HIT), ephemeral=True)
            return

        # Abuse detection before deferring so we can still use send_message (not followup).
        # Haiku classifier, calibrated conservatively, fail-open on errors.
        if await self.bot.claude.classify_abuse(question):
            count = await abuse_tracker.record_violation(self.bot.db, guild_id, user_id)
            if count >= abuse_tracker.ABUSE_THRESHOLD:
                await interaction.response.send_message(voice.pick(voice.ABUSE_SILENCED), ephemeral=True)
                return
            if count >= abuse_tracker.WARN_AT:
                await interaction.response.send_message(voice.pick(voice.ABUSE_WARNING), ephemeral=True)
                return
            # First violation: let Claude handle it via the constitution; fall through.

        await interaction.response.defer(thinking=True)
        me = interaction.guild.me if interaction.guild else None
        try:
            answer = await self._answer(interaction.channel, me, question)
        except Exception as exc:
            log.exception("ask failed")
            emit_error(
                source="ask", exc=exc, recoverable=False,
                guild_id=guild_id, user_id=user_id,
            )
            await bot_logs.maybe_post_db_error(
                self.bot, self.bot.db, guild_id, exc,
                source="ask", user_id=user_id,
                verbosity=self.bot.config.bot_logs_verbosity,
            )
            await bot_logs.maybe_post_prompt_error(
                self.bot, self.bot.db, guild_id, exc,
                source="ask", user_id=user_id,
                verbosity=self.bot.config.bot_logs_verbosity,
            )
            await interaction.followup.send(voice.pick(voice.DB_ERROR))
            return

        try:
            await consume_user(self.bot.db, user_id, guild_id, "ask")
        except Exception:
            log.exception("rate consume failed")  # don't block the response

        await interaction.followup.send(answer)

    async def _answer(
        self,
        channel: object,
        me: discord.Member | None,
        question: str,
    ) -> str:
        context = ""
        image_urls: list[str] = []
        memory_context = await self._memory_context(channel)
        if (
            isinstance(channel, discord.TextChannel | discord.Thread)
            and me is not None
        ):
            msgs = await recent_messages(channel, me, limit=30)
            context = format_for_prompt(msgs)
            # Pull image URLs so Toots can actually see what the room is reacting to.
            # Bumped from 3 to 8, lean toward accuracy / "she sees what we see" since
            # cost is still bounded by the hard cap inside _call().
            image_urls = recent_image_urls(msgs, limit=8)

        # Pre-enrich any social URLs that appear in the user's question OR in
        # the recent channel chatter we just pulled. This is the same pattern
        # /recap, /discourse, and chime-in use: fetch the tweet/TikTok/Reddit
        # content directly via the platform's free endpoint and pass it to
        # Claude as structured data, so the model doesn't have to round-trip
        # through web_search on each URL (faster + no "i need to look that
        # up" narration risk). Cap to 10 URLs total per ask to bound latency.
        text_for_urls = f"{question}\n{context}"
        raw_urls = extract_urls(text_for_urls)[:10]

        # Run link enrichment, Perplexity search, and markets fetch in parallel.
        # return_exceptions=True so one fetcher's outage can't cancel the others.
        coros: list[Any] = []
        enrich_idx = pplx_idx = markets_idx = -1
        if raw_urls:
            enrich_idx = len(coros)
            coros.append(enrich_batch(raw_urls))
        pplx = self.bot.perplexity
        if pplx:
            pplx_idx = len(coros)
            coros.append(pplx.search(build_search_query(question, surface="ask"), purpose="ask"))
        markets_idx = len(coros)
        coros.append(self.bot.markets.get_context(question))

        raw = await asyncio.gather(*coros, return_exceptions=True) if coros else []
        enriched_map: dict[str, Any] = (
            raw[enrich_idx]  # type: ignore[assignment]
            if enrich_idx >= 0 and not isinstance(raw[enrich_idx], BaseException) else {}
        )
        pplx_result: str | None = (
            raw[pplx_idx]  # type: ignore[assignment]
            if pplx_idx >= 0 and not isinstance(raw[pplx_idx], BaseException) else None
        )
        markets_raw = raw[markets_idx] if markets_idx >= 0 else None
        markets_result: list[MarketSnapshot] | None = (
            markets_raw if isinstance(markets_raw, list) else None
        )

        enriched = [e for e in enriched_map.values() if e is not None]

        return await self.bot.claude.ask(
            question, channel_context=context, use_web=True,
            image_urls=image_urls,
            enriched_links=enriched if enriched else None,
            perplexity_context=pplx_result,
            recently_seen_urls=raw_urls if raw_urls else None,
            markets_context=markets_result,
            memory_context=memory_context,
            memory_search=self._make_memory_search(channel),
        )

    def _make_memory_search(
        self, channel: object,
    ) -> Callable[[str], Awaitable[str]] | None:
        """Build the `search_memory` tool handler for claude.ask: an async
        (query) -> str bound to this guild's notes, for on-demand deep recall
        past the fixed memory block. None outside a guild. Fail-open: a search
        error returns a soft string, never raises into the model's tool loop."""
        guild = getattr(channel, "guild", None)
        if guild is None:
            return None
        guild_id = guild.id

        async def search(query: str) -> str:
            try:
                hits = await self.bot.db.search_memory_notes(guild_id, query)
            except Exception:
                log.exception("search_memory failed")
                return "(couldn't reach memory just now)"
            emit("memory_search", guild_id=guild_id, query=query[:120], hits=len(hits))
            return _format_memory_hits(hits)

        return search

    async def _memory_context(self, channel: object) -> str | None:
        """Toots's distilled long-term memory of this server (from the memory
        cog's notes), formatted for the /ask prompt. Mixes the three tiers:
        the weekly arc first (coarse, oldest), then this week's daily notes,
        then the last few hourly notes last (recency bias favors the freshest).
        Fail-open: a memory-fetch error must never block an answer.
        """
        guild = getattr(channel, "guild", None)
        if guild is None:
            return None
        try:
            weekly = await self.bot.db.get_memory_notes(guild.id, "weekly", limit=1)
            daily = await self.bot.db.get_memory_notes(guild.id, "daily", limit=2)
            hourly = await self.bot.db.get_memory_notes(guild.id, "hourly", limit=4)
        except Exception:
            log.exception("memory context fetch failed")
            return None
        parts = [f"[the bigger picture] {summary}" for _, summary, _, _ in weekly]
        parts += [f"[this week] {summary}" for _, summary, _, _ in daily]
        parts += [f"[recently] {summary}" for _, summary, _, _ in hourly]
        return "\n".join(parts) if parts else None

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        """Summon handler. Shares the /ask rate limit.

        Two ways to get Toots to answer:
          1. Reply directly to one of her own messages (no @ needed).
          2. Explicitly @-mention her in the body.
        """
        if message.author.bot:
            return
        if message.guild is None:
            return  # no DMs
        if message.mention_everyone:
            return
        me = message.guild.me
        if me is None:
            return

        # Path 1: a direct reply to one of Toots' own messages counts as
        # addressing her, even with no inline mention.
        reply_quote = _reply_quote(message, me.id)
        replying_to_toots = reply_quote is not None

        # Path 2: an explicit @-mention. Only enforce the mention gate when this
        # isn't already a reply to her.
        if not replying_to_toots:
            if not message.mentions or me not in message.mentions:
                return
            # Must be a real mention in the body (not just the auto-reply ping).
            if f"<@{me.id}>" not in message.content and f"<@!{me.id}>" not in message.content:
                return
            if message.reference is not None:
                # Reply to someone *else* that auto-pings us: ignore unless there's
                # an explicit mention beyond the prefix Discord injects.
                stripped = re.sub(rf"^<@!?{me.id}>\s*", "", message.content, count=1)
                if (
                    me.mention not in stripped
                    and f"<@{me.id}>" not in stripped
                    and f"<@!{me.id}>" not in stripped
                ):
                    return

        if not await self.bot.db.is_configured(message.guild.id):
            return  # silent before setup

        # Strip our mention; replace other user mentions with display names.
        question = re.sub(rf"<@!?{me.id}>", "", message.content)
        for u in message.mentions:
            if u.id != me.id:
                question = question.replace(f"<@{u.id}>", u.display_name)
                question = question.replace(f"<@!{u.id}>", u.display_name)
        question = question.strip()
        if not question:
            return

        # When she's been replied to, prepend what *she* said so "that"/"why"
        # resolves, since her own messages aren't in the channel context we pull
        # (recent_messages skips bots). Abuse + empty checks still run on the
        # user's raw text only.
        prompt_question = question
        if replying_to_toots and reply_quote:
            # _strip_html drops cite/html tags AND unescapes entities, matching
            # how channel context is cleaned elsewhere.
            quoted = re.sub(r"\s+", " ", _strip_html(reply_quote)).strip()[:300]
            if quoted:
                prompt_question = f'[replying to your earlier message: "{quoted}"] {question}'

        # Silenced users: completely silent treatment (no reply).
        if await abuse_tracker.is_silenced(self.bot.db, message.guild.id, message.author.id):
            return

        try:
            allowed, _, _ = await check_user_limit(
                self.bot.db, message.author.id, message.guild.id, "ask"
            )
        except Exception:
            log.exception("rate check failed in mention; failing open")
            allowed = True
        if not allowed:
            await message.reply(voice.pick(voice.RATE_LIMIT_HIT), mention_author=False)
            return

        # Abuse detection: Haiku classifier, fail-open. Mention warnings are
        # public replies (mods + room see the call-out); silenced users get
        # complete silence (no reply) at the on_message entry above.
        if await self.bot.claude.classify_abuse(question):
            count = await abuse_tracker.record_violation(
                self.bot.db, message.guild.id, message.author.id,
            )
            if count >= abuse_tracker.ABUSE_THRESHOLD:
                await message.reply(voice.pick(voice.ABUSE_SILENCED), mention_author=False)
                return
            if count >= abuse_tracker.WARN_AT:
                await message.reply(voice.pick(voice.ABUSE_WARNING), mention_author=False)
                return
            # First violation: let Claude handle via the constitution; fall through.

        async with message.channel.typing():
            try:
                answer = await self._answer(message.channel, me, prompt_question)
            except Exception as exc:
                log.exception("mention answer failed")
                emit_error(
                source="ask_mention", exc=exc, recoverable=False,
                guild_id=message.guild.id, user_id=message.author.id,
            )
                await bot_logs.maybe_post_db_error(
                    self.bot, self.bot.db, message.guild.id, exc,
                    source="ask_mention", user_id=message.author.id,
                    verbosity=self.bot.config.bot_logs_verbosity,
                )
                await bot_logs.maybe_post_prompt_error(
                    self.bot, self.bot.db, message.guild.id, exc,
                    source="ask_mention", user_id=message.author.id,
                    verbosity=self.bot.config.bot_logs_verbosity,
                )
                await message.reply(voice.pick(voice.DB_ERROR), mention_author=False)
                return

        try:
            await consume_user(self.bot.db, message.author.id, message.guild.id, "ask")
        except Exception:
            log.exception("consume failed (mention)")
        await message.reply(answer, mention_author=False)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Ask(cast("TootsiesBot", bot)))
