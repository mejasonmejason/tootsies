"""Tests for utils/bot_logs.py: the DB-error notifier helpers.

The thin discord-channel post is covered indirectly elsewhere (and excluded
from coverage in pyproject); these tests focus on the new pure logic:
format_db_error (label shape) and maybe_post_db_error (only fires on asyncpg
errors, no-op otherwise, PII never leaks into the message).
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import asyncpg
import asyncpg.exceptions
import pytest

from utils import bot_logs

# ---- format_db_error ---------------------------------------------------------


def test_format_db_error_minimal_fields() -> None:
    msg = bot_logs.format_db_error(exc_class="InterfaceError", source="ask")
    assert "InterfaceError" in msg
    assert "ask" in msg
    assert msg.startswith("db error:")


def test_format_db_error_redacts_user_via_mention() -> None:
    """User ID becomes a Discord mention, not the raw int in plaintext."""
    msg = bot_logs.format_db_error(
        exc_class="InvalidCachedStatementError",
        source="recap",
        guild_id=111,
        user_id=222,
        sql_op="SELECT FROM discourse_schedule",
    )
    assert "<@222>" in msg
    assert "InvalidCachedStatementError" in msg
    assert "SELECT FROM discourse_schedule" in msg
    assert "recap" in msg
    assert "111" in msg


def test_format_db_error_omits_optional_when_missing() -> None:
    msg = bot_logs.format_db_error(exc_class="X", source="y")
    # No <@...> mention block when user_id is absent.
    assert "<@" not in msg
    # No op= block when sql_op is absent.
    assert "op=" not in msg


def test_format_db_error_does_not_leak_sql_params() -> None:
    """The caller passes a sanitized op label (sql_op() in db.py drops $1, $2,
    bound values, etc.). The formatter shouldn't itself add anything PII-shaped."""
    msg = bot_logs.format_db_error(
        exc_class="X", source="y", user_id=123,
        sql_op="SELECT FROM rate_limits",
    )
    assert "$" not in msg  # no positional placeholders survived
    # User content (the actual values bound to $1/$2/...) is never accepted
    # as input; the message is built from labels only.


# ---- maybe_post_db_error -----------------------------------------------------


def _stub_bot_and_db() -> tuple[MagicMock, MagicMock]:
    bot = MagicMock()
    db = MagicMock()
    return bot, db


@pytest.mark.asyncio
async def test_maybe_post_db_error_fires_on_asyncpg_postgres_error() -> None:
    bot, db = _stub_bot_and_db()
    exc = asyncpg.exceptions.InvalidCachedStatementError("plan changed")
    with patch.object(bot_logs, "post_db_error", new=AsyncMock()) as post:
        await bot_logs.maybe_post_db_error(
            bot, db, guild_id=999, exc=exc, source="ask", user_id=42,
        )
    post.assert_awaited_once()
    assert post.await_args is not None
    kwargs = post.await_args.kwargs
    assert kwargs["source"] == "ask"
    assert kwargs["user_id"] == 42
    assert kwargs["exc"] is exc


@pytest.mark.asyncio
async def test_maybe_post_db_error_fires_on_asyncpg_interface_error() -> None:
    """InterfaceError (e.g. connection closed) should also fire."""
    bot, db = _stub_bot_and_db()
    exc = asyncpg.InterfaceError("pool closed")
    with patch.object(bot_logs, "post_db_error", new=AsyncMock()) as post:
        await bot_logs.maybe_post_db_error(
            bot, db, guild_id=999, exc=exc, source="recap",
        )
    post.assert_awaited_once()


@pytest.mark.asyncio
async def test_maybe_post_db_error_noop_on_non_asyncpg_exception() -> None:
    """A plain Exception (Claude API timeout, Discord 5xx, etc.) does NOT post.

    DB error logs are for asyncpg failures specifically; routing everything
    through #bot-logs would drown out the real signal.
    """
    bot, db = _stub_bot_and_db()
    exc = RuntimeError("anthropic api 503")
    with patch.object(bot_logs, "post_db_error", new=AsyncMock()) as post:
        await bot_logs.maybe_post_db_error(
            bot, db, guild_id=999, exc=exc, source="ask",
        )
    post.assert_not_awaited()


@pytest.mark.asyncio
async def test_maybe_post_db_error_noop_without_guild_id() -> None:
    """DMs / global handlers without a guild can't route a post anywhere."""
    bot, db = _stub_bot_and_db()
    exc = asyncpg.InterfaceError("x")
    with patch.object(bot_logs, "post_db_error", new=AsyncMock()) as post:
        await bot_logs.maybe_post_db_error(
            bot, db, guild_id=None, exc=exc, source="ask",
        )
    post.assert_not_awaited()


@pytest.mark.asyncio
async def test_maybe_post_db_error_swallows_post_failure() -> None:
    """Bot-logs is best-effort. If the post itself crashes (channel deleted,
    perms revoked, etc.) we must not re-raise into the cog's error path."""
    bot, db = _stub_bot_and_db()
    exc = asyncpg.exceptions.InvalidCachedStatementError("x")
    with patch.object(
        bot_logs, "post_db_error",
        new=AsyncMock(side_effect=RuntimeError("discord 403")),
    ):
        # Must NOT raise.
        await bot_logs.maybe_post_db_error(
            bot, db, guild_id=1, exc=exc, source="ask",
        )


@pytest.mark.asyncio
async def test_post_db_error_passes_errors_level_through_to_post() -> None:
    """post_db_error must always send at the 'errors' level so the most
    reserved verbosity setting still surfaces DB errors."""
    bot, db = _stub_bot_and_db()
    exc = asyncpg.InterfaceError("x")
    with patch.object(bot_logs, "post", new=AsyncMock()) as post:
        await bot_logs.post_db_error(
            bot, db, guild_id=1, exc=exc, source="ask",
            verbosity="milestones",
        )
    assert post.await_args is not None
    kwargs = post.await_args.kwargs
    assert kwargs["level"] == "errors"
    assert kwargs["verbosity"] == "milestones"


# ---- format_prompt_error -----------------------------------------------------


def test_format_prompt_error_minimal_fields() -> None:
    msg = bot_logs.format_prompt_error(exc_class="BadRequestError", source="ask")
    assert "BadRequestError" in msg
    assert "ask" in msg
    assert msg.startswith("prompt error:")


def test_format_prompt_error_includes_detail_truncated() -> None:
    """Detail string carries actionable info ('unable to download', 'rate limit'),
    truncated to keep mod-log messages compact."""
    msg = bot_logs.format_prompt_error(
        exc_class="BadRequestError", source="ask_mention",
        guild_id=111, user_id=222,
        detail="Unable to download the file. Please verify the URL.",
    )
    assert "<@222>" in msg
    assert "111" in msg
    assert "Unable to download the file" in msg
    assert "ask_mention" in msg


def test_format_prompt_error_truncates_long_detail() -> None:
    long = "x" * 1000
    msg = bot_logs.format_prompt_error(exc_class="X", source="y", detail=long)
    # detail capped at 160 chars in the formatter
    assert msg.count("x") <= 160


def test_format_prompt_error_omits_optional_when_missing() -> None:
    msg = bot_logs.format_prompt_error(exc_class="X", source="y")
    assert "<@" not in msg
    assert "detail=" not in msg
    assert "guild=" not in msg


# ---- maybe_post_prompt_error -------------------------------------------------


@pytest.mark.asyncio
async def test_maybe_post_prompt_error_fires_on_anthropic_api_error() -> None:
    """Anthropic API errors should fire AND go through post() at level='full'
    so only full-verbosity guilds see them."""
    import anthropic
    bot, db = _stub_bot_and_db()
    # Use the actual error class so the isinstance check matches what prod hits.
    exc = anthropic.APIError(
        message="boom", request=MagicMock(), body=None,
    )
    with patch.object(bot_logs, "post", new=AsyncMock()) as post:
        await bot_logs.maybe_post_prompt_error(
            bot, db, guild_id=999, exc=exc, source="ask", user_id=42,
            verbosity="full",
        )
    post.assert_awaited_once()
    assert post.await_args is not None
    kwargs = post.await_args.kwargs
    assert kwargs["level"] == "full"
    assert kwargs["verbosity"] == "full"


@pytest.mark.asyncio
async def test_maybe_post_prompt_error_noop_on_asyncpg_exception() -> None:
    """asyncpg errors should NOT route through prompt-error path; they have
    their own DB-error pipeline that always surfaces."""
    bot, db = _stub_bot_and_db()
    exc = asyncpg.InterfaceError("pool closed")
    with patch.object(bot_logs, "post", new=AsyncMock()) as post:
        await bot_logs.maybe_post_prompt_error(
            bot, db, guild_id=999, exc=exc, source="ask",
        )
    post.assert_not_awaited()


@pytest.mark.asyncio
async def test_maybe_post_prompt_error_noop_on_plain_exception() -> None:
    """Plain Exception (KeyError, ValueError, etc.) shouldn't surface to mods;
    those are real bugs the dev should chase in Railway logs."""
    bot, db = _stub_bot_and_db()
    exc = KeyError("missing key")
    with patch.object(bot_logs, "post", new=AsyncMock()) as post:
        await bot_logs.maybe_post_prompt_error(
            bot, db, guild_id=999, exc=exc, source="ask",
        )
    post.assert_not_awaited()


@pytest.mark.asyncio
async def test_maybe_post_prompt_error_noop_without_guild_id() -> None:
    import anthropic
    bot, db = _stub_bot_and_db()
    exc = anthropic.APIError(message="x", request=MagicMock(), body=None)
    with patch.object(bot_logs, "post", new=AsyncMock()) as post:
        await bot_logs.maybe_post_prompt_error(
            bot, db, guild_id=None, exc=exc, source="ask",
        )
    post.assert_not_awaited()


@pytest.mark.asyncio
async def test_maybe_post_prompt_error_swallows_post_failure() -> None:
    import anthropic
    bot, db = _stub_bot_and_db()
    exc = anthropic.APIError(message="x", request=MagicMock(), body=None)
    with patch.object(
        bot_logs, "post",
        new=AsyncMock(side_effect=RuntimeError("discord 403")),
    ):
        # Must NOT raise.
        await bot_logs.maybe_post_prompt_error(
            bot, db, guild_id=1, exc=exc, source="ask",
        )
