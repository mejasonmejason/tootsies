"""Bot-logs channel writer + verbosity gating."""

from __future__ import annotations

import logging

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

    Use from cog `except Exception` handlers, lets you keep the broad catch
    while still surfacing DB-class failures with detail. Best-effort: any
    failure inside the post itself is swallowed (we already lost the parent
    exception's stack to the cog's logger).
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
