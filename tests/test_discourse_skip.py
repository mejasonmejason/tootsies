"""Regression tests for scheduled discourse slot visibility (issue #123).

A scheduled slot that gets dropped (persistent 429, compose crash, or an empty
generation) used to vanish with only a plain log line, invisible to the
log-monitor routine. Each drop must now emit one structured `discourse_skipped`
event with a reason the monitor can query on.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import discord
import pytest

import cogs.discourse as discourse_mod


def _make_cog() -> discourse_mod.Discourse:
    # Bypass __init__ so we don't start the scheduler task loop.
    cog = discourse_mod.Discourse.__new__(discourse_mod.Discourse)
    bot = MagicMock()
    bot.db.get_channel_slot = AsyncMock(return_value=(0, datetime.now(UTC), 0))
    bot.db.record_channel_slot = AsyncMock()
    bot.db.record_schedule_post = AsyncMock()
    cog.bot = bot
    return cog


def _make_guild_channel() -> tuple[MagicMock, MagicMock]:
    channel = MagicMock(spec=discord.TextChannel)
    channel.id = 222
    guild = MagicMock(spec=discord.Guild)
    guild.id = 111
    guild.me = MagicMock()
    guild.get_channel = MagicMock(return_value=channel)
    return guild, channel


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("retval", "exc", "expected_reason"),
    [
        (("", "rate_limited"), None, "rate_limited"),
        (("EMPTY", None), None, "empty"),
        (("", None), None, "empty"),
        (None, RuntimeError("boom"), "compose_error"),
    ],
)
async def test_dropped_slot_emits_discourse_skipped(
    retval, exc, expected_reason
) -> None:
    cog = _make_cog()
    guild, _channel = _make_guild_channel()

    compose = AsyncMock(side_effect=exc) if exc is not None else AsyncMock(return_value=retval)

    with (
        patch.object(discourse_mod, "can_send_in", return_value=True),
        patch.object(cog, "_compose_with_retry", compose),
        patch.object(discourse_mod, "emit") as emit_mock,
        patch.object(discourse_mod, "emit_error"),
    ):
        await cog._maybe_post_to_channel(guild, 222, expected=1, today=date.today())

    skipped = [c for c in emit_mock.call_args_list if c.args and c.args[0] == "discourse_skipped"]
    assert len(skipped) == 1, f"expected one discourse_skipped emit, got {emit_mock.call_args_list}"
    assert skipped[0].kwargs["reason"] == expected_reason
    assert skipped[0].kwargs["guild_id"] == 111
    assert skipped[0].kwargs["channel_id"] == 222
