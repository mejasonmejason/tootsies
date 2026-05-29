"""Tests for utils.reactions (Toots adding reactions) + voice/permission helpers."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest

from utils.permissions import can_react
from utils.reactions import react


def _fake_perms(*, view: bool = True, history: bool = True, add: bool = True) -> object:
    return SimpleNamespace(
        view_channel=view, read_message_history=history, add_reactions=add,
    )


def _fake_message(
    *,
    perms: object | None = None,
    existing_reactions: list[object] | None = None,
    with_guild: bool = True,
) -> MagicMock:
    """A discord.Message whose channel grants the given perms and add_reaction is async."""
    msg = MagicMock(spec=discord.Message)
    msg.id = 555
    channel = MagicMock(spec=discord.TextChannel)
    channel.id = 99
    channel.permissions_for = MagicMock(return_value=perms or _fake_perms())
    msg.channel = channel
    if with_guild:
        guild = MagicMock(spec=discord.Guild)
        guild.id = 7
        guild.me = MagicMock(spec=discord.Member)
        msg.guild = guild
    else:
        msg.guild = None
    msg.reactions = existing_reactions or []
    msg.add_reaction = AsyncMock()
    return msg


# ---- can_react -------------------------------------------------------------------


def test_can_react_true_with_all_perms() -> None:
    channel = MagicMock(spec=discord.TextChannel)
    channel.permissions_for = MagicMock(return_value=_fake_perms())
    assert can_react(channel, MagicMock(spec=discord.Member)) is True


def test_can_react_false_without_add_reactions() -> None:
    channel = MagicMock(spec=discord.TextChannel)
    channel.permissions_for = MagicMock(return_value=_fake_perms(add=False))
    assert can_react(channel, MagicMock(spec=discord.Member)) is False


def test_can_react_false_without_read_history() -> None:
    channel = MagicMock(spec=discord.TextChannel)
    channel.permissions_for = MagicMock(return_value=_fake_perms(history=False))
    assert can_react(channel, MagicMock(spec=discord.Member)) is False


# ---- react -----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_react_adds_reaction_when_permitted() -> None:
    msg = _fake_message()
    ok = await react(msg, "🔥", source="chimein")
    assert ok is True
    msg.add_reaction.assert_awaited_once_with("🔥")


@pytest.mark.asyncio
async def test_react_skips_without_permission() -> None:
    msg = _fake_message(perms=_fake_perms(add=False))
    ok = await react(msg, "🔥", source="chimein")
    assert ok is False
    msg.add_reaction.assert_not_awaited()


@pytest.mark.asyncio
async def test_react_skips_without_guild() -> None:
    msg = _fake_message(with_guild=False)
    assert await react(msg, "🔥", source="chimein") is False
    msg.add_reaction.assert_not_awaited()


@pytest.mark.asyncio
async def test_react_idempotent_when_already_reacted_by_bot() -> None:
    already = SimpleNamespace(emoji="🔥", me=True)
    msg = _fake_message(existing_reactions=[already])
    ok = await react(msg, "🔥", source="chimein")
    assert ok is False
    msg.add_reaction.assert_not_awaited()


@pytest.mark.asyncio
async def test_react_skips_when_already_reacted_with_different_emoji() -> None:
    """One reaction per message: a prior bot reaction (any emoji) blocks another."""
    already = SimpleNamespace(emoji="👀", me=True)
    msg = _fake_message(existing_reactions=[already])
    ok = await react(msg, "🔥", source="chimein")
    assert ok is False
    msg.add_reaction.assert_not_awaited()


@pytest.mark.asyncio
async def test_react_still_adds_when_others_reacted_with_same_emoji() -> None:
    """Another user's 🔥 (r.me False) shouldn't block Toots from adding hers."""
    others = SimpleNamespace(emoji="🔥", me=False)
    msg = _fake_message(existing_reactions=[others])
    ok = await react(msg, "🔥", source="chimein")
    assert ok is True
    msg.add_reaction.assert_awaited_once_with("🔥")


@pytest.mark.asyncio
async def test_react_swallows_discord_errors() -> None:
    msg = _fake_message()
    msg.add_reaction = AsyncMock(side_effect=discord.HTTPException(MagicMock(), "boom"))
    assert await react(msg, "🔥", source="chimein") is False
