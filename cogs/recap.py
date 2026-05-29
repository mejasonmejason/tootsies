"""/recap period:[1h|today], channel-level summary in Toots voice."""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, time, timedelta
from typing import TYPE_CHECKING, Any, cast

import discord
from discord import app_commands
from discord.ext import commands

from utils import bot_logs, voice
from utils.events import emit, emit_error
from utils.feeds import (
    channel_dead_diagnostic,
    format_for_prompt,
    hot_urls,
    is_channel_dead,
    recent_image_urls,
    recent_messages,
    resolve_reactors,
)
from utils.gates import require_configured
from utils.link_enrich import enrich_batch
from utils.metrics import track_command
from utils.perplexity import build_search_query
from utils.rate_limits import check_user_limit, consume_user

if TYPE_CHECKING:
    from bot import TootsiesBot

log = logging.getLogger(__name__)


class Recap(commands.Cog):
    def __init__(self, bot: TootsiesBot) -> None:
        self.bot = bot

    @app_commands.command(name="recap", description="what'd i miss in this channel?")
    @app_commands.describe(period="how far back?")
    @app_commands.choices(
        period=[
            app_commands.Choice(name="last hour", value="1h"),
            app_commands.Choice(name="last 24h", value="1d"),
            app_commands.Choice(name="today (since midnight)", value="today"),
        ]
    )
    @track_command("recap")
    async def recap(
        self,
        interaction: discord.Interaction,
        period: app_commands.Choice[str],
    ) -> None:
        if not await require_configured(interaction, self.bot.db):
            return
        assert interaction.guild_id is not None
        user_id = interaction.user.id
        guild_id = interaction.guild_id

        try:
            allowed, _, _ = await check_user_limit(self.bot.db, user_id, guild_id, "recap")
        except Exception:
            log.exception("rate check failed; failing open")
            allowed = True
        if not allowed:
            await interaction.response.send_message(
                voice.pick(voice.RATE_LIMIT_HIT), ephemeral=True
            )
            return

        channel = interaction.channel
        if not isinstance(channel, discord.TextChannel | discord.Thread):
            await interaction.response.send_message(
                voice.pick(voice.PERMISSION_DENIED), ephemeral=True
            )
            return
        me = interaction.guild.me if interaction.guild else None
        if me is None:
            await interaction.response.send_message(
                voice.pick(voice.PERMISSION_DENIED), ephemeral=True
            )
            return

        within = _period_to_window(period.value)
        # Acknowledge the interaction BEFORE the history fetch. Discord expires
        # unacknowledged interactions after 3s, and recent_messages() can pull up
        # to 200 messages over the Discord API; in a busy channel that alone can
        # blow the 3s window, leaving defer() to fail with NotFound (10062). The
        # fast checks above (rate limit, channel/perms) stay ahead of the defer
        # and still answer via interaction.response; everything after the defer
        # uses interaction.followup, which has a 15-minute window.
        await interaction.response.defer(thinking=True)
        # /recap looks at more history than /ask, up to 200 over the period.
        # include_bots=True: a /recap should summarize EVERYTHING (webhook posts,
        # feed bots, the works), not just human chatter.
        msgs = await recent_messages(channel, me, limit=200, within=within, include_bots=True)

        try:
            if is_channel_dead(msgs):
                # Distinguish "quip vs. no info", emit a structured diagnostic AND post
                # to #bot-logs at full verbosity so mods can tell whether Toots is being
                # cute or whether something's actually wrong (perms, filtering, etc.).
                diag = channel_dead_diagnostic(channel, me, msgs)
                emit(
                    "recap_deflected",
                    guild_id=guild_id, user_id=user_id,
                    period=period.value, **diag,
                )
                await bot_logs.post(
                    self.bot, self.bot.db, guild_id,
                    f"👀 `/recap` deflected in <#{channel.id}> (period={period.value}): "
                    f"reason=`{diag['reason']}`, total={diag['total_messages']}, "
                    f"can_read_history={diag['can_read_history']}.",
                    level="full", verbosity=self.bot.config.bot_logs_verbosity,
                )
                line = voice.pick(voice.CHANNEL_DEAD)
            else:
                # Name WHO reacted with WHICH emoji on the top-reacted messages
                # (bounded API lookup), so Toots can say "the room 🔥'd jordan's
                # take" instead of a faceless reaction count.
                reactors = await resolve_reactors(msgs)
                blob = format_for_prompt(msgs, include_reactions=True, reactors=reactors)
                # Surface recent images to recap too so Toots can name the meme that
                # got the reactions. Now reaction-ranked, not strictly chronological.
                image_urls = recent_image_urls(msgs, limit=8)
                # Surface popular URLs separately so Toots is explicitly nudged to
                # OPEN them (fixes the "can't peep what's at the link" failure mode).
                url_list = hot_urls(msgs, limit=8)
                # Pre-fetch enriched content for known social platforms (X, TikTok,
                # YouTube, Reddit, Bluesky) so Claude reads tweet text / captions /
                # comments directly instead of bouncing off login walls via
                # web_search. Failures fall through silently per URL.
                # Run link enrichment and Perplexity search in parallel.
                # return_exceptions=True so a Perplexity outage can't cancel enrich_batch.
                coros: list[Any] = [enrich_batch([u for u, _, _, _ in url_list])]
                pplx = self.bot.perplexity
                pplx_idx = -1
                if pplx:
                    pplx_idx = len(coros)
                    coros.append(pplx.search(
                        build_search_query(blob[:300], surface="recap"),
                        purpose="recap",
                    ))
                raw = await asyncio.gather(*coros, return_exceptions=True)
                enriched_map = raw[0] if not isinstance(raw[0], BaseException) else {}
                pplx_result: str | None = (
                    raw[pplx_idx]  # type: ignore[assignment]
                    if pplx_idx >= 0 and not isinstance(raw[pplx_idx], BaseException) else None
                )

                enriched = [v for v in enriched_map.values() if v is not None]
                line = await self.bot.claude.recap(
                    channel.name, blob, image_urls=image_urls, hot_urls=url_list,
                    enriched_links=enriched,
                    perplexity_context=pplx_result,
                )
        except Exception as exc:
            log.exception("recap failed")
            emit_error(
                source="recap", exc=exc, recoverable=False,
                guild_id=guild_id, user_id=user_id,
            )
            await bot_logs.maybe_post_db_error(
                self.bot, self.bot.db, guild_id, exc,
                source="recap", user_id=user_id,
                verbosity=self.bot.config.bot_logs_verbosity,
            )
            await bot_logs.maybe_post_prompt_error(
                self.bot, self.bot.db, guild_id, exc,
                source="recap", user_id=user_id,
                verbosity=self.bot.config.bot_logs_verbosity,
            )
            await interaction.followup.send(voice.pick(voice.DB_ERROR))
            return

        try:
            await consume_user(self.bot.db, user_id, guild_id, "recap")
        except Exception:
            log.exception("consume failed")

        await interaction.followup.send(line)


def _period_to_window(period: str) -> timedelta:
    if period == "1h":
        return timedelta(hours=1)
    if period == "1d":
        return timedelta(hours=24)
    # "today" = since midnight UTC. Close enough, exact TZ doesn't matter for vibes.
    now = datetime.now(UTC)
    today_start = datetime.combine(now.date(), time.min, tzinfo=UTC)
    return now - today_start


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Recap(cast("TootsiesBot", bot)))
