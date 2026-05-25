"""Tests for db.py: the cached-plan retry wrapper and the sql_op label helper.

We don't spin up Postgres here; the wrapper logic is pure (asyncpg.Pool +
exception type), so we stub the pool with AsyncMock and assert the
wrapper's behavior on the happy path, the single-retry path, the
no-double-retry contract, and the pass-through for unrelated errors.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import asyncpg
import asyncpg.exceptions
import pytest

from db import DB, sql_op

# ---- sql_op label helper -----------------------------------------------------


def test_sql_op_select_from_table() -> None:
    assert sql_op("SELECT * FROM discourse_schedule WHERE guild_id = $1") == (
        "SELECT FROM discourse_schedule"
    )


def test_sql_op_insert_into_table() -> None:
    assert sql_op("INSERT INTO servers (guild_id) VALUES ($1)") == "INSERT INTO servers"


def test_sql_op_update_table() -> None:
    assert sql_op("UPDATE orders SET status = $1 WHERE id = $2") == "UPDATE orders"


def test_sql_op_delete_from_table() -> None:
    assert sql_op("DELETE FROM audit_log WHERE timestamp < NOW()") == "DELETE FROM audit_log"


def test_sql_op_strips_whitespace_and_truncates() -> None:
    # Multi-line + indented (the common style in db.py) collapses cleanly.
    label = sql_op("""
        SELECT count
        FROM rate_limits
        WHERE user_id=$1
    """)
    assert label == "SELECT FROM rate_limits"


def test_sql_op_no_params_in_label() -> None:
    """We never want $1, $2, ... or quoted strings to leak into the label."""
    label = sql_op("SELECT * FROM settings WHERE guild_id = $1 AND key = $2")
    assert "$1" not in label
    assert "$2" not in label
    assert "settings" in label.lower()


def test_sql_op_unknown_dml_returns_truncated_prefix() -> None:
    """Schema/DDL statements aren't shaped like a SELECT/INSERT; we still get
    SOMETHING short and safe (not the full schema dump)."""
    assert len(sql_op("CREATE TABLE foo (id INT)")) <= 50


# ---- DB._run cached-plan retry ----------------------------------------------


def _make_db_with_pool(pool: MagicMock) -> DB:
    """Build a DB instance bypassing connect(); inject the stub pool directly."""
    db = DB(dsn="postgres://stub")
    db.pool = pool
    return db


@pytest.mark.asyncio
async def test_run_happy_path_calls_pool_method_once() -> None:
    pool = MagicMock()
    pool.fetchrow = AsyncMock(return_value={"value": 1})
    db = _make_db_with_pool(pool)
    out = await db._fetchrow("SELECT value FROM settings WHERE key = $1", "k")
    assert out == {"value": 1}
    pool.fetchrow.assert_called_once_with(
        "SELECT value FROM settings WHERE key = $1", "k"
    )


@pytest.mark.asyncio
async def test_run_retries_once_on_invalid_cached_statement() -> None:
    """First call raises InvalidCachedStatementError, retry succeeds with same args."""
    pool = MagicMock()
    pool.fetch = AsyncMock(
        side_effect=[
            asyncpg.exceptions.InvalidCachedStatementError(
                "cached plan must not change result type"
            ),
            [{"id": 7}],
        ]
    )
    db = _make_db_with_pool(pool)
    out = await db._fetch("SELECT * FROM discourse_schedule WHERE guild_id = $1", 42)
    assert out == [{"id": 7}]
    assert pool.fetch.call_count == 2
    # Both calls used identical args (asyncpg evicts the bad cache between them).
    first_args = pool.fetch.call_args_list[0]
    second_args = pool.fetch.call_args_list[1]
    assert first_args == second_args


@pytest.mark.asyncio
async def test_run_does_not_retry_twice_on_persistent_invalid_cached() -> None:
    """If the retry also raises InvalidCachedStatementError, we bubble it; we
    don't loop. This bounds error-path latency to at most one extra round-trip."""
    pool = MagicMock()
    pool.execute = AsyncMock(
        side_effect=asyncpg.exceptions.InvalidCachedStatementError("still bad")
    )
    db = _make_db_with_pool(pool)
    with pytest.raises(asyncpg.exceptions.InvalidCachedStatementError):
        await db._execute("UPDATE orders SET status = $1 WHERE id = $2", "burnt", 1)
    assert pool.execute.call_count == 2  # one initial + one retry, no third


@pytest.mark.asyncio
async def test_run_does_not_swallow_unrelated_asyncpg_errors() -> None:
    """A plain PostgresError (e.g. unique constraint) should NOT be retried."""
    pool = MagicMock()
    pool.execute = AsyncMock(
        side_effect=asyncpg.exceptions.UniqueViolationError("dup key")
    )
    db = _make_db_with_pool(pool)
    with pytest.raises(asyncpg.exceptions.UniqueViolationError):
        await db._execute("INSERT INTO servers (guild_id) VALUES ($1)", 1)
    assert pool.execute.call_count == 1  # NOT retried


@pytest.mark.asyncio
async def test_run_does_not_swallow_unrelated_exceptions() -> None:
    pool = MagicMock()
    pool.fetchval = AsyncMock(side_effect=RuntimeError("network"))
    db = _make_db_with_pool(pool)
    with pytest.raises(RuntimeError, match="network"):
        await db._fetchval("SELECT 1")
    assert pool.fetchval.call_count == 1


# ---- discourse channels CRUD -------------------------------------------------


@pytest.mark.asyncio
async def test_get_discourse_channels_empty() -> None:
    pool = MagicMock()
    pool.fetch = AsyncMock(return_value=[])
    db = _make_db_with_pool(pool)
    result = await db.get_discourse_channels(1)
    assert result == []


@pytest.mark.asyncio
async def test_get_discourse_channels_returns_ids() -> None:
    pool = MagicMock()
    pool.fetch = AsyncMock(return_value=[{"channel_id": 100}, {"channel_id": 200}])
    db = _make_db_with_pool(pool)
    result = await db.get_discourse_channels(1)
    assert result == [100, 200]


@pytest.mark.asyncio
async def test_set_discourse_channels_uses_transaction() -> None:
    conn = AsyncMock()
    conn.__aenter__ = AsyncMock(return_value=conn)
    conn.__aexit__ = AsyncMock(return_value=False)
    txn = AsyncMock()
    txn.__aenter__ = AsyncMock(return_value=txn)
    txn.__aexit__ = AsyncMock(return_value=False)
    conn.transaction = MagicMock(return_value=txn)

    pool = MagicMock()
    pool.acquire = MagicMock(return_value=conn)
    db = _make_db_with_pool(pool)
    await db.set_discourse_channels(1, [100, 200])
    conn.execute.assert_called_once()
    conn.executemany.assert_called_once()


@pytest.mark.asyncio
async def test_get_channel_slot_default() -> None:
    pool = MagicMock()
    pool.fetchrow = AsyncMock(return_value=None)
    db = _make_db_with_pool(pool)
    result = await db.get_channel_slot(1, 100)
    assert result == (0, None, None)


@pytest.mark.asyncio
async def test_get_channel_slot_with_data() -> None:
    from datetime import date, datetime

    pool = MagicMock()
    pool.fetchrow = AsyncMock(return_value={
        "posts_today": 2, "last_post_at": datetime(2026, 5, 25), "posts_day": date(2026, 5, 25),
    })
    db = _make_db_with_pool(pool)
    posts, last_at, day = await db.get_channel_slot(1, 100)
    assert posts == 2
    assert last_at is not None
    assert day == date(2026, 5, 25)


# ---- all four wrappers ------------------------------------------------------


@pytest.mark.asyncio
async def test_all_four_wrappers_exist_and_route_to_correct_method() -> None:
    """Spot-check every wrapper routes to the matching pool method name."""
    pool = MagicMock()
    pool.execute = AsyncMock(return_value="EXECUTE 1")
    pool.fetch = AsyncMock(return_value=[])
    pool.fetchrow = AsyncMock(return_value=None)
    pool.fetchval = AsyncMock(return_value=0)
    db = _make_db_with_pool(pool)

    await db._execute("UPDATE foo SET x = 1")
    await db._fetch("SELECT * FROM foo")
    await db._fetchrow("SELECT * FROM foo LIMIT 1")
    await db._fetchval("SELECT COUNT(*) FROM foo")

    pool.execute.assert_called_once()
    pool.fetch.assert_called_once()
    pool.fetchrow.assert_called_once()
    pool.fetchval.assert_called_once()
