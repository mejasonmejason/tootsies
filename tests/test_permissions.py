"""Tests for utils.permissions — mod role gating and channel access checks."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest

from utils.permissions import can_read, can_send_in, is_mod


def _fake_member(
    *,
    user_id: int = 1,
    guild_owner_id: int = 999,
    manage_guild: bool = False,
    role_ids: list[int] | None = None,
) -> MagicMock:
    member = MagicMock(spec=discord.Member)
    member.id = user_id
    member.guild = SimpleNamespace(owner_id=guild_owner_id, id=42)
    member.guild_permissions = SimpleNamespace(manage_guild=manage_guild)
    member.roles = [SimpleNamespace(id=rid) for rid in (role_ids or [])]
    return member


# ---- is_mod ---------------------------------------------------------------------


@pytest.mark.asyncio
async def test_is_mod_true_for_guild_owner() -> None:
    """The guild owner is always treated as a mod, even with no mod_roles configured.
    This is the escape hatch for the first /menu run on a fresh install."""
    db = MagicMock()
    db.get_mod_roles = AsyncMock(return_value=[])
    member = _fake_member(user_id=100, guild_owner_id=100)
    assert await is_mod(db, member) is True


@pytest.mark.asyncio
async def test_is_mod_true_for_manage_guild_permission() -> None:
    """Anyone with manage_guild is a mod even if no roles match — server admins."""
    db = MagicMock()
    db.get_mod_roles = AsyncMock(return_value=[])
    member = _fake_member(user_id=1, guild_owner_id=999, manage_guild=True)
    assert await is_mod(db, member) is True


@pytest.mark.asyncio
async def test_is_mod_false_when_no_mod_roles_configured_and_no_perms() -> None:
    """Without any mod roles set up, regular members are not mods."""
    db = MagicMock()
    db.get_mod_roles = AsyncMock(return_value=[])
    member = _fake_member(user_id=1, guild_owner_id=999, manage_guild=False)
    assert await is_mod(db, member) is False


@pytest.mark.asyncio
async def test_is_mod_true_when_member_has_configured_role() -> None:
    db = MagicMock()
    db.get_mod_roles = AsyncMock(return_value=[10, 20, 30])
    member = _fake_member(user_id=1, role_ids=[5, 20])  # 20 matches
    assert await is_mod(db, member) is True


@pytest.mark.asyncio
async def test_is_mod_false_when_member_roles_dont_match() -> None:
    db = MagicMock()
    db.get_mod_roles = AsyncMock(return_value=[10, 20])
    member = _fake_member(user_id=1, role_ids=[5, 7])  # no overlap
    assert await is_mod(db, member) is False


# ---- can_send_in / can_read -----------------------------------------------------


def _fake_channel_with_perms(
    *, send: bool, view: bool, history: bool,
) -> MagicMock:
    channel = MagicMock(spec=discord.TextChannel)
    perms = SimpleNamespace(
        send_messages=send, view_channel=view, read_message_history=history,
    )
    channel.permissions_for = MagicMock(return_value=perms)
    return channel


def test_can_send_in_true_when_send_and_view_granted() -> None:
    me = MagicMock(spec=discord.Member)
    channel = _fake_channel_with_perms(send=True, view=True, history=True)
    assert can_send_in(channel, me) is True


def test_can_send_in_false_when_send_missing() -> None:
    me = MagicMock(spec=discord.Member)
    channel = _fake_channel_with_perms(send=False, view=True, history=True)
    assert can_send_in(channel, me) is False


def test_can_send_in_false_when_view_missing() -> None:
    me = MagicMock(spec=discord.Member)
    channel = _fake_channel_with_perms(send=True, view=False, history=True)
    assert can_send_in(channel, me) is False


def test_can_send_in_false_for_non_text_channel() -> None:
    """Voice channels, categories, etc. all return False (no isinstance match)."""
    me = MagicMock(spec=discord.Member)
    channel = MagicMock(spec=discord.VoiceChannel)
    assert can_send_in(channel, me) is False


def test_can_read_true_when_view_and_history_granted() -> None:
    me = MagicMock(spec=discord.Member)
    channel = _fake_channel_with_perms(send=False, view=True, history=True)
    assert can_read(channel, me) is True


def test_can_read_false_when_history_denied() -> None:
    """Visible-but-locked channels: bot sees the channel but can't read past messages."""
    me = MagicMock(spec=discord.Member)
    channel = _fake_channel_with_perms(send=True, view=True, history=False)
    assert can_read(channel, me) is False


def test_can_read_false_when_view_denied() -> None:
    me = MagicMock(spec=discord.Member)
    channel = _fake_channel_with_perms(send=False, view=False, history=True)
    assert can_read(channel, me) is False
