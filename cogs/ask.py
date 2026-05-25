"""/ask <question> + @Toots mention handler.

Mentions and /ask share a counter so heavy mention users can't escape the 20/day cap by
swapping interfaces.
"""

from __future__ import annotations

import asyncio
import logging
import re
from typing import TYPE_CHECKING, Any, cast

import discord
from discord import app_commands
from discord.ext import commands

from utils import bot_logs, voice
from utils.events import emit_error
from utils.feeds import format_for_prompt, recent_image_urls, recent_messages
from utils.gates import require_configured
from utils.link_enrich import enrich_batch
from utils.metrics import track_command
from utils.perplexity import build_search_query
from utils.rate_limits import check_user_limit, consume_user
from utils.url_guardrail import extract_urls

if TYPE_CHECKING:
    from bot import TootsiesBot

log = logging.getLogger(__name__)


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

        # Rate limit check first, fail fast before doing work.
        try:
            allowed, _, _ = await check_user_limit(self.bot.db, user_id, guild_id, "ask")
        except Exception as exc:  # fail-open per plan: better to answer than to go silent
            log.exception("rate limit check failed, failing open: %s", exc)
            allowed = True
        if not allowed:
            await interaction.response.send_message(voice.pick(voice.RATE_LIMIT_HIT), ephemeral=True)
            return

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

        # Run link enrichment and Perplexity search in parallel.
        # return_exceptions=True so a Perplexity outage can't cancel enrich_batch.
        coros: list[Any] = []
        enrich_idx = pplx_idx = -1
        if raw_urls:
            enrich_idx = len(coros)
            coros.append(enrich_batch(raw_urls))
        pplx = self.bot.perplexity
        if pplx:
            pplx_idx = len(coros)
            coros.append(pplx.search(build_search_query(question, surface="ask"), purpose="ask"))

        raw = await asyncio.gather(*coros, return_exceptions=True) if coros else []
        enriched_map: dict[str, Any] = (
            raw[enrich_idx]  # type: ignore[assignment]
            if enrich_idx >= 0 and not isinstance(raw[enrich_idx], BaseException) else {}
        )
        pplx_result: str | None = (
            raw[pplx_idx]  # type: ignore[assignment]
            if pplx_idx >= 0 and not isinstance(raw[pplx_idx], BaseException) else None
        )

        enriched = [e for e in enriched_map.values() if e is not None]

        return await self.bot.claude.ask(
            question, channel_context=context, use_web=True,
            image_urls=image_urls,
            enriched_links=enriched if enriched else None,
            perplexity_context=pplx_result,
            recently_seen_urls=raw_urls if raw_urls else None,
        )

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        """@Toots mention handler. Shares the /ask rate limit."""
        if message.author.bot:
            return
        if message.guild is None:
            return  # no DMs
        if message.mention_everyone:
            return
        me = message.guild.me
        if me is None or not message.mentions:
            return
        # Must mention only us, and must be a real mention (not the auto-reply ping).
        if me not in message.mentions:
            return
        if any(u.id != me.id for u in message.mentions):
            return
        # If this is a reply, Discord auto-mentions the original author. We need an *explicit*
        # mention beyond that, so check the raw content for our id.
        if f"<@{me.id}>" not in message.content and f"<@!{me.id}>" not in message.content:
            return
        if message.reference is not None:
            # Strip the auto-added mention prefix Discord injects on replies, then check again.
            stripped = re.sub(rf"^<@!?{me.id}>\s*", "", message.content, count=1)
            if me.mention not in stripped and f"<@{me.id}>" not in stripped and f"<@!{me.id}>" not in stripped:
                # The only mention was the auto-reply prefix, ignore.
                return

        if not await self.bot.db.is_configured(message.guild.id):
            return  # silent before setup

        # Strip the mention itself to get the actual question.
        question = re.sub(rf"<@!?{me.id}>", "", message.content).strip()
        if not question:
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

        async with message.channel.typing():
            try:
                answer = await self._answer(message.channel, me, question)
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
