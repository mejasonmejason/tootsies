"""Postgres connection pool + schema bootstrap + typed CRUD helpers."""

from __future__ import annotations

import logging
from datetime import date, datetime
from typing import Any

import asyncpg

from models import MoodMode, Order, OrderStatus, ScheduleState

log = logging.getLogger(__name__)

# Schema is idempotent — re-runs on every startup. Add new tables here; never drop columns
# from existing tables without a migration plan.
SCHEMA = """
CREATE TABLE IF NOT EXISTS servers (
    guild_id BIGINT PRIMARY KEY,
    configured BOOLEAN NOT NULL DEFAULT FALSE,
    configured_at TIMESTAMPTZ,
    kitchen_open BOOLEAN NOT NULL DEFAULT TRUE
);

CREATE TABLE IF NOT EXISTS settings (
    guild_id BIGINT NOT NULL,
    key TEXT NOT NULL,
    value JSONB NOT NULL,
    updated_by BIGINT,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (guild_id, key)
);

CREATE TABLE IF NOT EXISTS mod_roles (
    guild_id BIGINT NOT NULL,
    role_id BIGINT NOT NULL,
    PRIMARY KEY (guild_id, role_id)
);

CREATE TABLE IF NOT EXISTS feed_channels (
    guild_id BIGINT NOT NULL,
    channel_id BIGINT NOT NULL,
    category TEXT,
    PRIMARY KEY (guild_id, channel_id)
);

CREATE TABLE IF NOT EXISTS orders (
    id BIGSERIAL PRIMARY KEY,
    guild_id BIGINT NOT NULL,
    issue_number INTEGER,
    pr_number INTEGER,
    requester_id BIGINT NOT NULL,
    request_text TEXT NOT NULL,
    summary TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    error_log TEXT
);
CREATE INDEX IF NOT EXISTS orders_guild_status_idx ON orders (guild_id, status);
CREATE INDEX IF NOT EXISTS orders_issue_idx ON orders (issue_number);

CREATE TABLE IF NOT EXISTS rate_limits (
    user_id BIGINT NOT NULL,
    guild_id BIGINT NOT NULL,
    command TEXT NOT NULL,
    day DATE NOT NULL,
    count INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (user_id, guild_id, command, day)
);
CREATE INDEX IF NOT EXISTS rate_limits_day_idx ON rate_limits (day);

CREATE TABLE IF NOT EXISTS server_rate_limits (
    guild_id BIGINT NOT NULL,
    command TEXT NOT NULL,
    day DATE NOT NULL,
    count INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (guild_id, command, day)
);

CREATE TABLE IF NOT EXISTS cooldowns (
    user_id BIGINT NOT NULL,
    guild_id BIGINT NOT NULL,
    command TEXT NOT NULL,
    last_used_at TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (user_id, guild_id, command)
);

CREATE TABLE IF NOT EXISTS discourse_history (
    id BIGSERIAL PRIMARY KEY,
    guild_id BIGINT NOT NULL,
    category TEXT NOT NULL,
    topic_summary TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS discourse_history_guild_cat_idx ON discourse_history (guild_id, category, created_at DESC);

CREATE TABLE IF NOT EXISTS audit_log (
    id BIGSERIAL PRIMARY KEY,
    guild_id BIGINT NOT NULL,
    actor_id BIGINT,
    action TEXT NOT NULL,
    target TEXT,
    before JSONB,
    after JSONB,
    timestamp TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS audit_log_guild_ts_idx ON audit_log (guild_id, timestamp DESC);

CREATE TABLE IF NOT EXISTS discourse_schedule (
    guild_id BIGINT PRIMARY KEY,
    mode TEXT NOT NULL DEFAULT 'chill',
    last_changed_by BIGINT,
    last_changed_at TIMESTAMPTZ,
    posts_today INTEGER NOT NULL DEFAULT 0,
    last_post_at TIMESTAMPTZ,
    posts_day DATE
);

-- Migrate legacy mood_state rows on first boot after rename. Safe to run repeatedly:
-- only copies if the legacy table exists AND the new table has no row for that guild.
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM information_schema.tables WHERE table_name = 'mood_state') THEN
        INSERT INTO discourse_schedule (
            guild_id, mode, last_changed_by, last_changed_at, posts_today, last_post_at, posts_day
        )
        SELECT guild_id, mode, last_changed_by, last_changed_at, posts_today, last_post_at, posts_day
        FROM mood_state
        ON CONFLICT (guild_id) DO NOTHING;
        DROP TABLE mood_state;
    END IF;
END $$;
"""


class DB:
    def __init__(self, dsn: str) -> None:
        self.dsn = dsn
        self.pool: asyncpg.Pool | None = None

    async def connect(self) -> None:
        self.pool = await asyncpg.create_pool(self.dsn, min_size=1, max_size=10)
        assert self.pool is not None
        async with self.pool.acquire() as conn:
            await conn.execute(SCHEMA)
        log.info("db ready")

    async def close(self) -> None:
        if self.pool is not None:
            await self.pool.close()

    def _pool(self) -> asyncpg.Pool:
        if self.pool is None:
            raise RuntimeError("db not connected — call DB.connect() first")
        return self.pool

    # ---- servers ----------------------------------------------------------------

    async def ensure_server(self, guild_id: int) -> None:
        await self._pool().execute(
            "INSERT INTO servers (guild_id) VALUES ($1) ON CONFLICT (guild_id) DO NOTHING",
            guild_id,
        )

    async def is_configured(self, guild_id: int) -> bool:
        row = await self._pool().fetchrow(
            "SELECT configured FROM servers WHERE guild_id = $1", guild_id
        )
        return bool(row and row["configured"])

    async def mark_configured(self, guild_id: int) -> None:
        await self._pool().execute(
            """
            INSERT INTO servers (guild_id, configured, configured_at)
            VALUES ($1, TRUE, NOW())
            ON CONFLICT (guild_id) DO UPDATE
                SET configured = TRUE, configured_at = COALESCE(servers.configured_at, NOW())
            """,
            guild_id,
        )

    async def set_kitchen_open(self, guild_id: int, open_: bool) -> None:
        await self._pool().execute(
            """
            INSERT INTO servers (guild_id, kitchen_open) VALUES ($1, $2)
            ON CONFLICT (guild_id) DO UPDATE SET kitchen_open = EXCLUDED.kitchen_open
            """,
            guild_id, open_,
        )

    async def is_kitchen_open(self, guild_id: int) -> bool:
        row = await self._pool().fetchrow(
            "SELECT kitchen_open FROM servers WHERE guild_id = $1", guild_id
        )
        # Default open if no row yet — /menu hasn't been run but we shouldn't block.
        return True if row is None else bool(row["kitchen_open"])

    # ---- settings ---------------------------------------------------------------

    async def get_setting(self, guild_id: int, key: str) -> Any:
        row = await self._pool().fetchrow(
            "SELECT value FROM settings WHERE guild_id = $1 AND key = $2", guild_id, key
        )
        return row["value"] if row else None

    async def set_setting(
        self, guild_id: int, key: str, value: Any, actor_id: int | None = None
    ) -> None:
        import json

        await self._pool().execute(
            """
            INSERT INTO settings (guild_id, key, value, updated_by, updated_at)
            VALUES ($1, $2, $3::jsonb, $4, NOW())
            ON CONFLICT (guild_id, key) DO UPDATE
                SET value = EXCLUDED.value, updated_by = EXCLUDED.updated_by, updated_at = NOW()
            """,
            guild_id, key, json.dumps(value), actor_id,
        )

    async def all_settings(self, guild_id: int) -> dict[str, Any]:
        rows = await self._pool().fetch(
            "SELECT key, value FROM settings WHERE guild_id = $1", guild_id
        )
        return {r["key"]: r["value"] for r in rows}

    # ---- mod roles --------------------------------------------------------------

    async def set_mod_roles(self, guild_id: int, role_ids: list[int]) -> None:
        async with self._pool().acquire() as conn, conn.transaction():
            await conn.execute("DELETE FROM mod_roles WHERE guild_id = $1", guild_id)
            if role_ids:
                await conn.executemany(
                    "INSERT INTO mod_roles (guild_id, role_id) VALUES ($1, $2)",
                    [(guild_id, rid) for rid in role_ids],
                )

    async def get_mod_roles(self, guild_id: int) -> list[int]:
        rows = await self._pool().fetch(
            "SELECT role_id FROM mod_roles WHERE guild_id = $1", guild_id
        )
        return [r["role_id"] for r in rows]

    # ---- feed channels ----------------------------------------------------------

    async def set_feed_channels(
        self, guild_id: int, channels: list[tuple[int, str | None]]
    ) -> None:
        async with self._pool().acquire() as conn, conn.transaction():
            await conn.execute("DELETE FROM feed_channels WHERE guild_id = $1", guild_id)
            if channels:
                await conn.executemany(
                    "INSERT INTO feed_channels (guild_id, channel_id, category) VALUES ($1, $2, $3)",
                    [(guild_id, cid, cat) for cid, cat in channels],
                )

    async def get_feed_channels(
        self, guild_id: int, category: str | None = None
    ) -> list[tuple[int, str | None]]:
        if category:
            rows = await self._pool().fetch(
                "SELECT channel_id, category FROM feed_channels WHERE guild_id = $1 AND category = $2",
                guild_id, category,
            )
        else:
            rows = await self._pool().fetch(
                "SELECT channel_id, category FROM feed_channels WHERE guild_id = $1", guild_id
            )
        return [(r["channel_id"], r["category"]) for r in rows]

    # ---- orders -----------------------------------------------------------------

    async def create_order(
        self, guild_id: int, requester_id: int, request_text: str, summary: str
    ) -> Order:
        row = await self._pool().fetchrow(
            """
            INSERT INTO orders (guild_id, requester_id, request_text, summary, status)
            VALUES ($1, $2, $3, $4, $5)
            RETURNING *
            """,
            guild_id, requester_id, request_text, summary, OrderStatus.PREPPING.value,
        )
        return _row_to_order(row)

    async def update_order(
        self,
        order_id: int,
        *,
        status: OrderStatus | None = None,
        issue_number: int | None = None,
        pr_number: int | None = None,
        error_log: str | None = None,
    ) -> None:
        sets: list[str] = ["updated_at = NOW()"]
        args: list[Any] = []
        if status is not None:
            args.append(status.value)
            sets.append(f"status = ${len(args)}")
        if issue_number is not None:
            args.append(issue_number)
            sets.append(f"issue_number = ${len(args)}")
        if pr_number is not None:
            args.append(pr_number)
            sets.append(f"pr_number = ${len(args)}")
        if error_log is not None:
            args.append(error_log)
            sets.append(f"error_log = ${len(args)}")
        args.append(order_id)
        await self._pool().execute(
            f"UPDATE orders SET {', '.join(sets)} WHERE id = ${len(args)}", *args
        )

    async def get_order(self, order_id: int) -> Order | None:
        row = await self._pool().fetchrow("SELECT * FROM orders WHERE id = $1", order_id)
        return _row_to_order(row) if row else None

    async def get_order_by_issue(self, issue_number: int) -> Order | None:
        row = await self._pool().fetchrow(
            "SELECT * FROM orders WHERE issue_number = $1", issue_number
        )
        return _row_to_order(row) if row else None

    async def in_flight_orders(self, guild_id: int) -> list[Order]:
        rows = await self._pool().fetch(
            """
            SELECT * FROM orders
            WHERE guild_id = $1 AND status NOT IN ('served', 'burnt', 'sent_back')
            ORDER BY created_at DESC
            """,
            guild_id,
        )
        return [_row_to_order(r) for r in rows]

    async def recent_orders(
        self, guild_id: int, since_days: int = 30, limit: int = 50
    ) -> list[Order]:
        rows = await self._pool().fetch(
            """
            SELECT * FROM orders
            WHERE guild_id = $1 AND created_at > NOW() - ($2 || ' days')::interval
            ORDER BY created_at DESC LIMIT $3
            """,
            guild_id, str(since_days), limit,
        )
        return [_row_to_order(r) for r in rows]

    async def all_orders(self, guild_id: int, limit: int = 100) -> list[Order]:
        rows = await self._pool().fetch(
            "SELECT * FROM orders WHERE guild_id = $1 ORDER BY created_at DESC LIMIT $2",
            guild_id, limit,
        )
        return [_row_to_order(r) for r in rows]

    async def last_failed_deploy(self, guild_id: int) -> Order | None:
        """Most recent order — if it's burnt at the deploy step, we're pipeline-red."""
        row = await self._pool().fetchrow(
            """
            SELECT * FROM orders WHERE guild_id = $1
            ORDER BY created_at DESC LIMIT 1
            """,
            guild_id,
        )
        if not row:
            return None
        order = _row_to_order(row)
        return order if order.status == OrderStatus.BURNT else None

    # ---- rate limits ------------------------------------------------------------

    async def incr_user_rate(
        self, user_id: int, guild_id: int, command: str, day: date
    ) -> int:
        row = await self._pool().fetchrow(
            """
            INSERT INTO rate_limits (user_id, guild_id, command, day, count)
            VALUES ($1, $2, $3, $4, 1)
            ON CONFLICT (user_id, guild_id, command, day) DO UPDATE
                SET count = rate_limits.count + 1
            RETURNING count
            """,
            user_id, guild_id, command, day,
        )
        return int(row["count"])

    async def get_user_rate(
        self, user_id: int, guild_id: int, command: str, day: date
    ) -> int:
        row = await self._pool().fetchrow(
            "SELECT count FROM rate_limits WHERE user_id=$1 AND guild_id=$2 AND command=$3 AND day=$4",
            user_id, guild_id, command, day,
        )
        return int(row["count"]) if row else 0

    async def incr_server_rate(self, guild_id: int, command: str, day: date) -> int:
        row = await self._pool().fetchrow(
            """
            INSERT INTO server_rate_limits (guild_id, command, day, count)
            VALUES ($1, $2, $3, 1)
            ON CONFLICT (guild_id, command, day) DO UPDATE
                SET count = server_rate_limits.count + 1
            RETURNING count
            """,
            guild_id, command, day,
        )
        return int(row["count"])

    async def get_server_rate(self, guild_id: int, command: str, day: date) -> int:
        row = await self._pool().fetchrow(
            "SELECT count FROM server_rate_limits WHERE guild_id=$1 AND command=$2 AND day=$3",
            guild_id, command, day,
        )
        return int(row["count"]) if row else 0

    async def set_cooldown(self, user_id: int, guild_id: int, command: str) -> None:
        await self._pool().execute(
            """
            INSERT INTO cooldowns (user_id, guild_id, command, last_used_at)
            VALUES ($1, $2, $3, NOW())
            ON CONFLICT (user_id, guild_id, command) DO UPDATE
                SET last_used_at = NOW()
            """,
            user_id, guild_id, command,
        )

    async def get_cooldown(
        self, user_id: int, guild_id: int, command: str
    ) -> datetime | None:
        row = await self._pool().fetchrow(
            "SELECT last_used_at FROM cooldowns WHERE user_id=$1 AND guild_id=$2 AND command=$3",
            user_id, guild_id, command,
        )
        return row["last_used_at"] if row else None

    # ---- discourse history ------------------------------------------------------

    async def add_discourse(self, guild_id: int, category: str, summary: str) -> None:
        await self._pool().execute(
            "INSERT INTO discourse_history (guild_id, category, topic_summary) VALUES ($1, $2, $3)",
            guild_id, category, summary,
        )

    async def recent_discourse(
        self, guild_id: int, category: str, limit: int = 10
    ) -> list[tuple[str, datetime]]:
        """Recent topic summaries with timestamps for state-aware dedup. Last 72h."""
        rows = await self._pool().fetch(
            """
            SELECT topic_summary, created_at FROM discourse_history
            WHERE guild_id = $1 AND category = $2 AND created_at > NOW() - INTERVAL '72 hours'
            ORDER BY created_at DESC LIMIT $3
            """,
            guild_id, category, limit,
        )
        return [(r["topic_summary"], r["created_at"]) for r in rows]

    async def recent_discourse_all(
        self, guild_id: int, limit: int = 20
    ) -> list[tuple[str, str, datetime]]:
        """Recent topics across ALL categories — used by the mood scheduler's dedup."""
        rows = await self._pool().fetch(
            """
            SELECT category, topic_summary, created_at FROM discourse_history
            WHERE guild_id = $1 AND created_at > NOW() - INTERVAL '72 hours'
            ORDER BY created_at DESC LIMIT $2
            """,
            guild_id, limit,
        )
        return [(r["category"], r["topic_summary"], r["created_at"]) for r in rows]

    async def prune_discourse(self) -> None:
        await self._pool().execute(
            "DELETE FROM discourse_history WHERE created_at < NOW() - INTERVAL '72 hours'"
        )

    # ---- audit ------------------------------------------------------------------

    async def audit(
        self,
        guild_id: int,
        actor_id: int | None,
        action: str,
        target: str | None = None,
        before: Any = None,
        after: Any = None,
    ) -> None:
        import json

        await self._pool().execute(
            """
            INSERT INTO audit_log (guild_id, actor_id, action, target, before, after)
            VALUES ($1, $2, $3, $4, $5::jsonb, $6::jsonb)
            """,
            guild_id, actor_id, action, target,
            json.dumps(before) if before is not None else None,
            json.dumps(after) if after is not None else None,
        )

    async def prune_audit(self) -> None:
        await self._pool().execute(
            "DELETE FROM audit_log WHERE timestamp < NOW() - INTERVAL '90 days'"
        )

    # ---- discourse schedule -----------------------------------------------------

    async def get_schedule(self, guild_id: int) -> ScheduleState:
        row = await self._pool().fetchrow(
            "SELECT * FROM discourse_schedule WHERE guild_id = $1", guild_id
        )
        if not row:
            return ScheduleState(
                guild_id=guild_id, mode=MoodMode.CHILL, last_changed_by=None,
                last_changed_at=None, posts_today=0, last_post_at=None,
            )
        return ScheduleState(
            guild_id=row["guild_id"],
            mode=MoodMode(row["mode"]),
            last_changed_by=row["last_changed_by"],
            last_changed_at=row["last_changed_at"],
            posts_today=row["posts_today"],
            last_post_at=row["last_post_at"],
        )

    async def set_schedule(self, guild_id: int, mode: MoodMode, actor_id: int) -> None:
        await self._pool().execute(
            """
            INSERT INTO discourse_schedule (guild_id, mode, last_changed_by, last_changed_at)
            VALUES ($1, $2, $3, NOW())
            ON CONFLICT (guild_id) DO UPDATE
                SET mode = EXCLUDED.mode,
                    last_changed_by = EXCLUDED.last_changed_by,
                    last_changed_at = NOW()
            """,
            guild_id, mode.value, actor_id,
        )

    async def record_schedule_post(self, guild_id: int, today: date) -> None:
        await self._pool().execute(
            """
            INSERT INTO discourse_schedule (guild_id, posts_today, posts_day, last_post_at)
            VALUES ($1, 1, $2, NOW())
            ON CONFLICT (guild_id) DO UPDATE SET
                posts_today = CASE
                    WHEN discourse_schedule.posts_day = EXCLUDED.posts_day
                        THEN discourse_schedule.posts_today + 1
                    ELSE 1
                END,
                posts_day = EXCLUDED.posts_day,
                last_post_at = NOW()
            """,
            guild_id, today,
        )

    async def all_configured_guilds(self) -> list[int]:
        rows = await self._pool().fetch(
            "SELECT guild_id FROM servers WHERE configured = TRUE"
        )
        return [r["guild_id"] for r in rows]


def _row_to_order(row: Any) -> Order:
    return Order(
        id=row["id"],
        issue_number=row["issue_number"],
        pr_number=row["pr_number"],
        requester_id=row["requester_id"],
        guild_id=row["guild_id"],
        request_text=row["request_text"],
        summary=row["summary"],
        status=OrderStatus(row["status"]),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        error_log=row["error_log"],
    )
