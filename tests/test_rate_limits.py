"""Unit tests for utils.rate_limits, daily caps + cooldowns + on-hit event emission."""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest

from utils.rate_limits import (
    DEFAULT_PER_SERVER_DAILY,
    DEFAULT_PER_USER_DAILY,
    check_cooldown,
    check_server_limit,
    check_user_limit,
    consume_server,
    consume_user,
)


def _fake_db(
    *,
    user_setting: object = None,
    server_setting: object = None,
    user_rate: int = 0,
    server_rate: int = 0,
    cooldown_last_used: datetime | None = None,
) -> AsyncMock:
    """Build a DB stand-in that satisfies the methods rate_limits calls into."""
    db = AsyncMock()
    db.get_setting = AsyncMock(side_effect=lambda gid, key: {
        "per_user_daily_limit": user_setting,
        "per_server_daily_limit": server_setting,
    }.get(key))
    db.get_user_rate = AsyncMock(return_value=user_rate)
    db.get_server_rate = AsyncMock(return_value=server_rate)
    db.incr_user_rate = AsyncMock(return_value=user_rate + 1)
    db.incr_server_rate = AsyncMock(return_value=server_rate + 1)
    db.get_cooldown = AsyncMock(return_value=cooldown_last_used)
    return db


# ---- per-user limits ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_check_user_limit_allowed_under_default_cap() -> None:
    db = _fake_db(user_rate=5)
    allowed, current, cap = await check_user_limit(db, user_id=1, guild_id=2, command="ask")
    assert allowed is True
    assert current == 5
    assert cap == DEFAULT_PER_USER_DAILY


@pytest.mark.asyncio
async def test_check_user_limit_blocked_at_cap() -> None:
    db = _fake_db(user_rate=DEFAULT_PER_USER_DAILY)
    allowed, current, cap = await check_user_limit(db, user_id=1, guild_id=2, command="ask")
    assert allowed is False
    assert current == cap


@pytest.mark.asyncio
async def test_check_user_limit_uses_server_override_when_set() -> None:
    """A /menu setting overrides the default cap. Live read on every call."""
    db = _fake_db(user_setting=5, user_rate=4)
    allowed, _, cap = await check_user_limit(db, user_id=1, guild_id=2, command="ask")
    assert cap == 5
    assert allowed is True

    db = _fake_db(user_setting=5, user_rate=5)
    allowed, _, _ = await check_user_limit(db, user_id=1, guild_id=2, command="ask")
    assert allowed is False


@pytest.mark.asyncio
async def test_check_user_limit_emits_event_on_hit(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A rate_limit_hit event fires when the user trips the cap (used for dashboards)."""
    caplog.set_level(logging.INFO, logger="tootsies.events")
    db = _fake_db(user_rate=DEFAULT_PER_USER_DAILY)
    await check_user_limit(db, user_id=42, guild_id=99, command="ask")
    messages = [r.getMessage() for r in caplog.records if r.name == "tootsies.events"]
    assert any("rate_limit_hit" in m and "\"scope\":\"user\"" in m for m in messages)


@pytest.mark.asyncio
async def test_check_user_limit_no_event_when_not_hit(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger="tootsies.events")
    db = _fake_db(user_rate=1)
    await check_user_limit(db, user_id=1, guild_id=2, command="ask")
    messages = [r.getMessage() for r in caplog.records if r.name == "tootsies.events"]
    assert not any("rate_limit_hit" in m for m in messages)


@pytest.mark.asyncio
async def test_consume_user_increments_count() -> None:
    db = _fake_db(user_rate=3)
    new_count = await consume_user(db, user_id=1, guild_id=2, command="ask")
    assert new_count == 4
    db.incr_user_rate.assert_awaited_once()


# ---- per-server limits -------------------------------------------------------------


@pytest.mark.asyncio
async def test_check_server_limit_allowed_under_default_cap() -> None:
    db = _fake_db(server_rate=10)
    allowed, current, cap = await check_server_limit(db, guild_id=2, command="discourse")
    assert allowed is True
    assert current == 10
    assert cap == DEFAULT_PER_SERVER_DAILY


@pytest.mark.asyncio
async def test_check_server_limit_blocked_at_cap_emits_event(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger="tootsies.events")
    db = _fake_db(server_rate=DEFAULT_PER_SERVER_DAILY)
    allowed, _, _ = await check_server_limit(db, guild_id=99, command="discourse")
    assert allowed is False
    messages = [r.getMessage() for r in caplog.records if r.name == "tootsies.events"]
    assert any("rate_limit_hit" in m and "\"scope\":\"server\"" in m for m in messages)


@pytest.mark.asyncio
async def test_check_server_limit_respects_menu_override() -> None:
    db = _fake_db(server_setting=3, server_rate=3)
    allowed, _, cap = await check_server_limit(db, guild_id=2, command="discourse")
    assert cap == 3
    assert allowed is False


@pytest.mark.asyncio
async def test_consume_server_increments_count() -> None:
    db = _fake_db(server_rate=7)
    new_count = await consume_server(db, guild_id=2, command="discourse")
    assert new_count == 8


# ---- cooldown ---------------------------------------------------------------------


@pytest.mark.asyncio
async def test_check_cooldown_allowed_when_no_prior_use() -> None:
    db = _fake_db(cooldown_last_used=None)
    allowed, remaining = await check_cooldown(db, user_id=1, guild_id=2, command="order")
    assert allowed is True
    assert remaining == timedelta(0)


@pytest.mark.asyncio
async def test_check_cooldown_blocked_during_window() -> None:
    """15-min default window: a recent use should be blocked."""
    now = datetime.now(UTC)
    db = _fake_db(cooldown_last_used=now - timedelta(minutes=2))
    allowed, remaining = await check_cooldown(db, user_id=1, guild_id=2, command="order")
    assert allowed is False
    # ~13 minutes remaining; bound loosely to avoid clock-skew flakes.
    assert remaining > timedelta(minutes=12)
    assert remaining < timedelta(minutes=14)


@pytest.mark.asyncio
async def test_check_cooldown_allowed_after_window() -> None:
    now = datetime.now(UTC)
    db = _fake_db(cooldown_last_used=now - timedelta(hours=1))
    allowed, _ = await check_cooldown(db, user_id=1, guild_id=2, command="order")
    assert allowed is True


@pytest.mark.asyncio
async def test_check_cooldown_custom_window() -> None:
    now = datetime.now(UTC)
    db = _fake_db(cooldown_last_used=now - timedelta(seconds=30))
    # Custom 1-minute window: still in cooldown.
    allowed, _ = await check_cooldown(
        db, user_id=1, guild_id=2, command="order", window=timedelta(minutes=1),
    )
    assert allowed is False
