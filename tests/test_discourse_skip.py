"""Regression tests for the scheduled discourse poster (issue #123).

Two behaviors:
  - dropped slots (persistent 429, compose crash, empty generation) used to
    vanish with only a plain log line; each drop must now emit one structured
    `discourse_skipped` event the log-monitor routine can query.
  - the per-guild dispatch is jittered up to 30s so discourse + the music cog
    (same mood schedule, same tick) don't collide on the shared Sonnet TPM
    ceiling and trip a 429.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock, patch
from zoneinfo import ZoneInfo

import discord
import pytest

import cogs.discourse as discourse_mod
from models import MoodMode


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


@pytest.mark.asyncio
async def test_scheduled_dispatch_is_jittered() -> None:
    """The dispatch waits a bounded random offset (<=30s) before posting, so it
    doesn't collide with the music cog on the same tick (issue #123)."""
    cog = _make_cog()
    guild, _channel = _make_guild_channel()
    bot = cast(Any, cog.bot)
    bot.get_guild = MagicMock(return_value=guild)
    bot.db.get_schedule = AsyncMock(return_value=MagicMock(mood=MoodMode.CHILL))
    bot.db.get_discourse_channels = AsyncMock(return_value=[222])

    now_et = datetime(2026, 5, 28, 19, 0, tzinfo=ZoneInfo("America/New_York"))
    sleeps: list[float] = []

    async def fake_sleep(secs: float) -> None:
        sleeps.append(secs)

    with (
        patch.object(discourse_mod.asyncio, "sleep", side_effect=fake_sleep),
        patch.object(cog, "_maybe_post_to_channel", AsyncMock()) as post_mock,
    ):
        await cog._maybe_scheduled_post(111, now_et)

    # One channel => the only sleep is the jitter (no inter-channel gap), and it
    # must be within [0, 30].
    assert len(sleeps) == 1
    assert 0.0 <= sleeps[0] <= discourse_mod._SCHEDULED_JITTER_MAX_SECONDS
    post_mock.assert_awaited_once()
