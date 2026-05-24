"""Tests for utils.metrics.track_command, signature preservation, error capture."""

from __future__ import annotations

import inspect
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from utils.metrics import track_command


class _FakeCog:
    """Stand-in for a cog with a bot.db that record_command can poke at."""

    def __init__(self) -> None:
        self.bot = MagicMock()
        self.bot.db = MagicMock()
        self.bot.db.record_command = AsyncMock()


def _fake_interaction(guild_id: int = 1, user_id: int = 2) -> MagicMock:
    interaction = MagicMock()
    interaction.guild_id = guild_id
    interaction.user.id = user_id
    interaction.command = MagicMock()
    interaction.command.qualified_name = "test_cmd"
    return interaction


def test_track_command_preserves_wrapped_signature() -> None:
    """discord.py introspects the callback's signature to build slash command params.
    The decorator must expose the original signature, not its (self, interaction,
    *args, **kwargs) wrapper signature, or command registration breaks.
    """

    @track_command("foo")
    async def cmd(self: Any, interaction: Any, question: str) -> None:
        pass

    sig = inspect.signature(cmd)
    params = list(sig.parameters.keys())
    assert params == ["self", "interaction", "question"]


@pytest.mark.asyncio
async def test_track_command_writes_metrics_on_success() -> None:
    cog = _FakeCog()

    @track_command("ask")
    async def cmd(self: Any, interaction: Any) -> str:
        return "ok"

    interaction = _fake_interaction()
    result = await cmd(cog, interaction)
    assert result == "ok"
    cog.bot.db.record_command.assert_awaited_once()
    call_kwargs = cog.bot.db.record_command.call_args.kwargs
    assert call_kwargs["command"] == "ask"
    assert call_kwargs["ok"] is True
    assert call_kwargs["error_class"] is None
    assert call_kwargs["duration_ms"] >= 0


@pytest.mark.asyncio
async def test_track_command_records_error_and_reraises() -> None:
    """A failed command must still be recorded; the exception bubbles up unchanged."""
    cog = _FakeCog()

    @track_command("ask")
    async def cmd(self: Any, interaction: Any) -> None:
        raise ValueError("boom")

    with pytest.raises(ValueError, match="boom"):
        await cmd(cog, _fake_interaction())

    cog.bot.db.record_command.assert_awaited_once()
    kwargs = cog.bot.db.record_command.call_args.kwargs
    assert kwargs["ok"] is False
    assert kwargs["error_class"] == "ValueError"


@pytest.mark.asyncio
async def test_track_command_metrics_failure_does_not_break_user_response() -> None:
    """If the metrics DB write itself errors, the user's command still completes."""
    cog = _FakeCog()
    cog.bot.db.record_command.side_effect = RuntimeError("db down")

    @track_command("ask")
    async def cmd(self: Any, interaction: Any) -> str:
        return "ok"

    result = await cmd(cog, _fake_interaction())
    assert result == "ok"


@pytest.mark.asyncio
async def test_track_command_falls_back_to_interaction_when_name_omitted() -> None:
    cog = _FakeCog()

    @track_command()  # no explicit name
    async def cmd(self: Any, interaction: Any) -> None:
        pass

    interaction = _fake_interaction()
    interaction.command.qualified_name = "order new"
    await cmd(cog, interaction)
    assert cog.bot.db.record_command.call_args.kwargs["command"] == "order new"
