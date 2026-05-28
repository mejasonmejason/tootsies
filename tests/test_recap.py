"""Regression tests for /recap interaction handling.

Covers the NotFound 10062 bug (issue #98): the channel-history fetch must not run
before the interaction is acknowledged, or Discord expires the interaction (3s
window) and defer() raises NotFound. We assert defer() happens before the fetch.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import cast
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest
from discord import app_commands


class _StopAfterFetch(Exception):
    """Sentinel raised from the patched fetch to short-circuit the downstream path."""


@pytest.mark.asyncio
async def test_recap_defers_before_history_fetch(monkeypatch) -> None:
    """defer() must be awaited before recent_messages() (see issue #98).

    recent_messages() can pull up to 200 messages over the Discord API; in a busy
    channel that alone can exceed the 3s acknowledgment window. If the fetch runs
    first, defer() fails with NotFound (10062) and /recap dies silently for the user.
    """
    import cogs.recap as recap_mod

    order: list[str] = []

    async def fake_recent_messages(*_args, **_kwargs):
        order.append("fetch")
        raise _StopAfterFetch

    monkeypatch.setattr(recap_mod, "recent_messages", fake_recent_messages)
    monkeypatch.setattr(
        recap_mod, "check_user_limit", AsyncMock(return_value=(True, 0, 0))
    )

    bot = MagicMock()
    bot.db.is_configured = AsyncMock(return_value=True)
    bot.db.record_command = AsyncMock()

    cog = recap_mod.Recap(bot)

    interaction = MagicMock()
    interaction.guild = MagicMock()
    interaction.guild.id = 1
    interaction.guild.me = MagicMock()
    interaction.guild_id = 1
    interaction.user.id = 2
    interaction.channel = MagicMock(spec=discord.TextChannel)

    async def fake_defer(**_kwargs):
        order.append("defer")

    interaction.response = MagicMock()
    interaction.response.defer = AsyncMock(side_effect=fake_defer)
    interaction.response.send_message = AsyncMock()
    interaction.followup = MagicMock()
    interaction.followup.send = AsyncMock()

    period = app_commands.Choice(name="last hour", value="1h")

    # discord.py types Command.callback without the cog `self` param, but at
    # runtime it's the unbound function and needs the cog passed explicitly.
    # Cast to a plain callable so mypy accepts the (cog, interaction, period) call.
    callback = cast(Callable[..., Awaitable[None]], type(cog).recap.callback)
    with pytest.raises(_StopAfterFetch):
        await callback(cog, interaction, period)

    assert order == ["defer", "fetch"], (
        "interaction must be acknowledged (defer) before the history fetch; "
        f"got order={order}"
    )
    interaction.response.defer.assert_awaited_once()
