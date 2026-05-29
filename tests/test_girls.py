"""Tests for the "girls" role feature: db helpers, the role check, the warmth
cue in claude_client.ask, and the ask cog's room-scan builder.

No live Postgres / Discord; we stub the DB internals and the Anthropic call,
and use spec'd mocks for Discord members (matching tests/test_permissions.py).
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import discord
import pytest

from claude_client import ClaudeClient
from cogs.ask import Ask
from cogs.girls import GirlsView
from db import DB
from utils.permissions import member_has_role


def _fake_member(*, user_id: int, display_name: str, role_ids: list[int]) -> MagicMock:
    member = MagicMock(spec=discord.Member)
    member.id = user_id
    member.display_name = display_name
    member.roles = [SimpleNamespace(id=rid) for rid in role_ids]
    return member


# ---- db.get/set_girls_roles -------------------------------------------------


@pytest.mark.asyncio
async def test_get_girls_roles_reads_list_from_settings() -> None:
    db = DB("postgres://x")
    db._fetchrow = AsyncMock(return_value={"value": [10, 20]})  # type: ignore[method-assign]
    assert await db.get_girls_roles(42) == [10, 20]


@pytest.mark.asyncio
async def test_get_girls_roles_empty_when_unset() -> None:
    db = DB("postgres://x")
    db._fetchrow = AsyncMock(return_value=None)  # type: ignore[method-assign]
    assert await db.get_girls_roles(42) == []


@pytest.mark.asyncio
async def test_set_girls_roles_dedups_and_persists() -> None:
    db = DB("postgres://x")
    db.set_setting = AsyncMock()  # type: ignore[method-assign]
    await db.set_girls_roles(42, [10, 20, 10, 30], actor_id=7)
    db.set_setting.assert_awaited_once()
    assert db.set_setting.await_args is not None
    args = db.set_setting.await_args.args
    # (guild_id, key, value, actor_id)
    assert args[0] == 42
    assert args[1] == "girls_role_ids"
    assert args[2] == [10, 20, 30]  # 10 deduped, order preserved
    assert args[3] == 7


# ---- member_has_role --------------------------------------------------------


def test_member_has_role_true_on_match() -> None:
    member = _fake_member(user_id=1, display_name="mia", role_ids=[5, 20])
    assert member_has_role(member, {20, 99}) is True


def test_member_has_role_false_on_no_match() -> None:
    member = _fake_member(user_id=1, display_name="mia", role_ids=[5, 6])
    assert member_has_role(member, {20, 99}) is False


def test_member_has_role_false_on_empty_set() -> None:
    member = _fake_member(user_id=1, display_name="mia", role_ids=[5, 6])
    assert member_has_role(member, set()) is False


# ---- claude_client.ask girls_context warmth cue -----------------------------


@pytest.mark.asyncio
async def test_ask_injects_girls_warmth_when_girls_context_present() -> None:
    client = ClaudeClient(api_key="test")
    fake = AsyncMock(return_value=MagicMock(text="hey girl"))
    with patch.object(client, "_call", fake):
        await client.ask("q", girls_context="mia, jade")
    user_message = fake.call_args.kwargs["user_message"].lower()
    assert "mia, jade" in user_message
    assert "your girls" in user_message
    assert "feminine" in user_message or "warm" in user_message


@pytest.mark.asyncio
async def test_ask_omits_girls_block_when_no_girls_context() -> None:
    client = ClaudeClient(api_key="test")
    fake = AsyncMock(return_value=MagicMock(text="answer"))
    with patch.object(client, "_call", fake):
        await client.ask("q")
    user_message = fake.call_args.kwargs["user_message"].lower()
    assert "your girls" not in user_message


# ---- Ask._girls_context room scan -------------------------------------------


def _ask_cog(role_ids: list[int]) -> Ask:
    bot = MagicMock()
    bot.db = MagicMock()
    bot.db.get_girls_roles = AsyncMock(return_value=role_ids)
    return Ask(bot)


@pytest.mark.asyncio
async def test_girls_context_returns_none_when_no_roles_configured() -> None:
    cog = _ask_cog([])
    channel = SimpleNamespace(guild=SimpleNamespace(id=42))
    asker = _fake_member(user_id=1, display_name="mia", role_ids=[20])
    assert await cog._girls_context(channel, [], asker) is None


@pytest.mark.asyncio
async def test_girls_context_names_asker_and_recent_girls() -> None:
    cog = _ask_cog([20])
    channel = SimpleNamespace(guild=SimpleNamespace(id=42))
    asker = _fake_member(user_id=1, display_name="mia", role_ids=[20])
    girl_poster = _fake_member(user_id=2, display_name="jade", role_ids=[20])
    not_a_girl = _fake_member(user_id=3, display_name="rob", role_ids=[7])
    msgs = [
        SimpleNamespace(author=girl_poster),
        SimpleNamespace(author=not_a_girl),
        SimpleNamespace(author=asker),  # dup of asker, should not repeat
    ]
    result = await cog._girls_context(channel, msgs, asker)  # type: ignore[arg-type]
    assert result == "mia, jade"


@pytest.mark.asyncio
async def test_girls_context_failopen_on_db_error() -> None:
    cog = _ask_cog([20])
    cog.bot.db.get_girls_roles = AsyncMock(side_effect=RuntimeError("db down"))  # type: ignore[method-assign]
    channel = SimpleNamespace(guild=SimpleNamespace(id=42))
    asker = _fake_member(user_id=1, display_name="mia", role_ids=[20])
    assert await cog._girls_context(channel, [], asker) is None


@pytest.mark.asyncio
async def test_girls_context_none_outside_guild() -> None:
    cog = _ask_cog([20])
    channel = SimpleNamespace(guild=None)
    assert await cog._girls_context(channel, [], None) is None


# ---- GirlsView (the /girls autosave role select) ----------------------------


def _fake_guild(role_names: dict[int, str]) -> MagicMock:
    guild = MagicMock(spec=discord.Guild)
    guild.id = 42
    guild.get_role = MagicMock(
        side_effect=lambda rid: (
            SimpleNamespace(id=rid, mention=f"@{role_names[rid]}")
            if rid in role_names else None
        )
    )
    return guild


def _fake_bot() -> MagicMock:
    bot = MagicMock()
    bot.db = MagicMock()
    bot.db.set_girls_roles = AsyncMock()
    bot.db.audit = AsyncMock()
    return bot


@pytest.mark.asyncio  # GirlsView.__init__ needs a running loop (asyncio.Future)
async def test_girls_view_embed_lists_current_roles() -> None:
    guild = _fake_guild({20: "Habibtis"})
    view = GirlsView(_fake_bot(), guild, [20], actor_id=1)
    assert "@Habibtis" in (view.embed().description or "")


@pytest.mark.asyncio
async def test_girls_view_embed_when_empty() -> None:
    guild = _fake_guild({})
    view = GirlsView(_fake_bot(), guild, [], actor_id=1)
    assert "no girls picked yet" in (view.embed().description or "")


@pytest.mark.asyncio
async def test_girls_view_autosave_persists_and_rerenders() -> None:
    bot = _fake_bot()
    guild = _fake_guild({20: "Habibtis", 21: "VIP"})
    view = GirlsView(bot, guild, [20], actor_id=1)
    view.selected = [20, 21]  # what the select callback would set
    interaction = MagicMock()
    interaction.user.id = 1
    interaction.response.edit_message = AsyncMock()
    await view.autosave(interaction)
    bot.db.set_girls_roles.assert_awaited_once_with(42, [20, 21], actor_id=1)
    bot.db.audit.assert_awaited_once()
    interaction.response.edit_message.assert_awaited_once()


@pytest.mark.asyncio
async def test_girls_view_autosave_rejects_other_users() -> None:
    bot = _fake_bot()
    guild = _fake_guild({20: "Habibtis"})
    view = GirlsView(bot, guild, [20], actor_id=1)
    interaction = MagicMock()
    interaction.user.id = 999  # not the invoker
    interaction.response.send_message = AsyncMock()
    interaction.response.edit_message = AsyncMock()
    await view.autosave(interaction)
    bot.db.set_girls_roles.assert_not_awaited()
    interaction.response.send_message.assert_awaited_once()
    interaction.response.edit_message.assert_not_awaited()
