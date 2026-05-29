"""Rate limit + cooldown helpers.

Per the plan: 100 is the default daily cap. Per-user (ask, recap) and per-server (discourse, order)
are separate counters. /order also has a 15-min per-user cooldown.

Settings can override defaults via /menu; we read those at call time so changes apply live.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

from db import DB
from utils.events import emit

DEFAULT_PER_USER_DAILY = 100
DEFAULT_PER_SERVER_DAILY = 20
ORDER_COOLDOWN = timedelta(minutes=15)


def _today_utc() -> date:
    return datetime.now(UTC).date()


async def _user_cap(db: DB, guild_id: int) -> int:
    val = await db.get_setting(guild_id, "per_user_daily_limit")
    return int(val) if isinstance(val, int) else DEFAULT_PER_USER_DAILY


async def _server_cap(db: DB, guild_id: int) -> int:
    val = await db.get_setting(guild_id, "per_server_daily_limit")
    return int(val) if isinstance(val, int) else DEFAULT_PER_SERVER_DAILY


async def check_user_limit(
    db: DB, user_id: int, guild_id: int, command: str
) -> tuple[bool, int, int]:
    """Returns (allowed, current_count, cap). Allowed = current_count < cap.

    Does NOT consume the slot. Call consume_user() to increment after a successful action.
    """
    cap = await _user_cap(db, guild_id)
    current = await db.get_user_rate(user_id, guild_id, command, _today_utc())
    allowed = current < cap
    if not allowed:
        emit(
            "rate_limit_hit", scope="user", command=command,
            user_id=user_id, guild_id=guild_id, count=current, cap=cap,
        )
    return allowed, current, cap


async def consume_user(db: DB, user_id: int, guild_id: int, command: str) -> int:
    return await db.incr_user_rate(user_id, guild_id, command, _today_utc())


async def check_server_limit(
    db: DB, guild_id: int, command: str
) -> tuple[bool, int, int]:
    cap = await _server_cap(db, guild_id)
    current = await db.get_server_rate(guild_id, command, _today_utc())
    allowed = current < cap
    if not allowed:
        emit(
            "rate_limit_hit", scope="server", command=command,
            user_id=None, guild_id=guild_id, count=current, cap=cap,
        )
    return allowed, current, cap


async def consume_server(db: DB, guild_id: int, command: str) -> int:
    return await db.incr_server_rate(guild_id, command, _today_utc())


async def check_cooldown(
    db: DB, user_id: int, guild_id: int, command: str, window: timedelta = ORDER_COOLDOWN
) -> tuple[bool, timedelta]:
    """Returns (allowed, time_left). time_left is zero if allowed."""
    last = await db.get_cooldown(user_id, guild_id, command)
    if last is None:
        return True, timedelta(0)
    elapsed = datetime.now(UTC) - last
    if elapsed >= window:
        return True, timedelta(0)
    return False, window - elapsed
