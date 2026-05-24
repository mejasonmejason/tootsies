"""Bot-logs channel writer + verbosity gating."""

from __future__ import annotations

import logging

import anthropic
import asyncpg
import discord

from db import DB
from utils.permissions import can_send_in

log = logging.getLogger(__name__)

# Verbosity ladder, events are emitted with a level; the server's BOT_LOGS_VERBOSITY decides
# whether we actually post.
LEVELS = {"errors": 0, "milestones": 1, "full": 2}


async def post(
    bot: discord.Client,
    db: DB,
    guild_id: int,
    message: str,
    *,
    level: str = "milestones",
    verbosity: str = "milestones",
) -> None:
    """Best-effort post into the configured #bot-logs channel."""
    if LEVELS.get(level, 1) > LEVELS.get(verbosity, 1):
        return
    channel_id = await db.get_setting(guild_id, "bot_logs_channel")
    if not channel_id:
        return
    guild = bot.get_guild(guild_id)
    if guild is None:
        return
    channel = guild.get_channel(int(channel_id))
    if channel is None or not isinstance(channel, discord.TextChannel):
        return
    me = guild.me
    if me is None or not can_send_in(channel, me):
        return
    try:
        await channel.send(message)
    except discord.DiscordException as exc:
        log.warning("bot_logs post failed: %s", exc)


def format_db_error(
    *,
    exc_class: str,
    source: str,
    guild_id: int | None = None,
    user_id: int | None = None,
    sql_op: str | None = None,
) -> str:
    """Build a single-line mod-log message for a DB-layer error.

    Intentionally compact + PII-light: exception class, source label, redacted
    user mention, and a coarse SQL op label only. No full query text, no
    bound params (those can carry user content).
    """
    user_part = f"<@{user_id}>" if user_id else "n/a"
    sql_part = f" op=`{sql_op}`" if sql_op else ""
    guild_part = f" guild=`{guild_id}`" if guild_id else ""
    return (
        f"db error: `{exc_class}` in `{source}`{sql_part}{guild_part} "
        f"(user={user_part})"
    )


async def post_db_error(
    bot: discord.Client,
    db: DB,
    guild_id: int,
    *,
    exc: BaseException,
    source: str,
    user_id: int | None = None,
    sql_op: str | None = None,
    verbosity: str = "milestones",
) -> None:
    """Post a structured DB-error notification to the guild's #bot-logs channel.

    Always sent at the 'errors' level so any verbosity setting (including the
    most reserved one) will pass it through. Mods need to see DB failures.
    """
    await post(
        bot,
        db,
        guild_id,
        format_db_error(
            exc_class=type(exc).__name__,
            source=source,
            guild_id=guild_id,
            user_id=user_id,
            sql_op=sql_op,
        ),
        level="errors",
        verbosity=verbosity,
    )


async def maybe_post_db_error(
    bot: discord.Client,
    db: DB,
    guild_id: int | None,
    exc: BaseException,
    *,
    source: str,
    user_id: int | None = None,
    verbosity: str = "milestones",
) -> None:
    """Post to #bot-logs only if `exc` is an asyncpg error. No-op otherwise.

    Use from cog `except Exception` handlers in user-impact paths (where the
    error caused a deflection, ignored request, or undelivered response).
    Don't wire into background-task error handlers; those should emit `error`
    EVENTs for the Railway dashboard but not surface in #bot-logs (see the
    sibling maybe_post_prompt_error for the full rationale).

    Best-effort: any failure inside the post itself is swallowed (we already
    lost the parent exception's stack to the cog's logger).
    """
    if guild_id is None:
        return
    if not isinstance(exc, asyncpg.PostgresError | asyncpg.InterfaceError):
        return
    try:
        await post_db_error(
            bot, db, guild_id,
            exc=exc, source=source, user_id=user_id, verbosity=verbosity,
        )
    except Exception:
        log.exception("maybe_post_db_error: bot-logs post failed for %s", source)


def format_prompt_error(
    *,
    exc_class: str,
    source: str,
    guild_id: int | None = None,
    user_id: int | None = None,
    detail: str | None = None,
) -> str:
    """Build a single-line mod-log message for a Claude prompt-layer error.

    Mirrors `format_db_error` but for Anthropic API failures (BadRequestError,
    RateLimitError, APITimeoutError, etc.). Includes a truncated detail string
    when available because prompt errors often carry actionable info in their
    message ("Unable to download the file", "rate limit exceeded", etc.) that
    just an exception class wouldn't convey.
    """
    user_part = f"<@{user_id}>" if user_id else "n/a"
    guild_part = f" guild=`{guild_id}`" if guild_id else ""
    detail_part = f" detail=`{detail[:160]}`" if detail else ""
    return (
        f"prompt error: `{exc_class}` in `{source}`{detail_part}{guild_part} "
        f"(user={user_part})"
    )


async def maybe_post_prompt_error(
    bot: discord.Client,
    db: DB,
    guild_id: int | None,
    exc: BaseException,
    *,
    source: str,
    user_id: int | None = None,
    verbosity: str = "milestones",
) -> None:
    """Post to #bot-logs only if `exc` is a Claude API error, AND only when the
    server's verbosity is set to 'full'.

    Filters OUT asyncpg errors (those route through maybe_post_db_error at
    'errors' level which always fires). Filters IN `anthropic.APIError` and
    its subclasses (BadRequestError on image-fetch failures, RateLimitError,
    APITimeoutError, etc.). Gated to 'full' verbosity because prompt errors
    fire more often than DB ones, are usually transient, and would spam
    milestones-mode mods.

    WHERE TO CALL THIS FROM: only user-impact paths. The intent is that mods
    see a log ONLY when the error caused a user-facing failure (deflection,
    request ignored, response not delivered). Specifically:

      DO call from: cog `except Exception` blocks that follow with a
        voice.DB_ERROR quip or otherwise abandon the user's request
        (cogs/ask.py, cogs/recap.py, cogs/discourse.py, cogs/order.py
        preflight, bot.py on_app_command_error).

      DO NOT call from: background tasks where a failure is silently
        retried or skipped (chime-in tick scoring/posting, scheduled
        discourse tick, the chime-in recent_messages fetch which
        fails-open). Those should keep emitting `error` EVENTs for
        the Railway dashboard, but they don't belong in #bot-logs.

    Best-effort: any failure inside the post itself is swallowed.
    """
    if guild_id is None:
        return
    if isinstance(exc, asyncpg.PostgresError | asyncpg.InterfaceError):
        return  # handled by maybe_post_db_error at the 'errors' level
    if not isinstance(exc, anthropic.APIError):
        return
    msg = format_prompt_error(
        exc_class=type(exc).__name__,
        source=source,
        guild_id=guild_id,
        user_id=user_id,
        detail=str(exc) if str(exc) else None,
    )
    try:
        await post(
            bot, db, guild_id, msg,
            level="full",
            verbosity=verbosity,
        )
    except Exception:
        log.exception("maybe_post_prompt_error: bot-logs post failed for %s", source)
