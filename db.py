"""Postgres connection pool + schema bootstrap + typed CRUD helpers."""

from __future__ import annotations

import logging
from datetime import date, datetime
from typing import Any

import asyncpg
import asyncpg.exceptions

from models import MoodMode, Order, OrderStatus, ScheduleState

log = logging.getLogger(__name__)


def sql_op(query: str) -> str:
    """Extract a coarse SQL op label (e.g. 'SELECT FROM discourse_schedule').

    Stripped down so we never log full queries with $1/$2/etc. or params.
    Returns at most ~50 chars, safe to ship to a public mod-log channel.
    """
    import re

    q = " ".join(query.split())  # collapse whitespace
    # UPDATE and DELETE name the table right after the verb; SELECT and INSERT
    # need FROM/INTO. Walk those two cases separately so labels stay readable.
    m = re.match(
        r"^\s*(UPDATE|DELETE)\s+(?:FROM\s+)?([A-Za-z_][A-Za-z0-9_]*)",
        q,
        flags=re.IGNORECASE,
    )
    if m:
        verb = m.group(1).upper()
        table = m.group(2)
        connector = "FROM" if verb == "DELETE" else ""
        label = f"{verb} {connector} {table}".replace("  ", " ").strip()
        return label[:50]
    m = re.match(
        r"^\s*(SELECT|INSERT|WITH)\b(?:.*?\b(FROM|INTO)\s+([A-Za-z_][A-Za-z0-9_]*))?",
        q,
        flags=re.IGNORECASE,
    )
    if not m:
        return q[:50]
    verb = m.group(1).upper()
    target_verb = (m.group(2) or "").upper()
    table = m.group(3) or ""
    label = f"{verb} {target_verb} {table}" if table else verb
    return label[:50]

# Schema is idempotent, re-runs on every startup. Add new tables here; never drop columns
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
    error_log TEXT,
    announced_at TIMESTAMPTZ
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
    mood TEXT NOT NULL DEFAULT 'chill',
    last_changed_by BIGINT,
    last_changed_at TIMESTAMPTZ,
    posts_today INTEGER NOT NULL DEFAULT 0,
    last_post_at TIMESTAMPTZ,
    posts_day DATE
);

-- Migrate legacy `mood_state` table from the pre-rename schema (plan §7).
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM information_schema.tables WHERE table_name = 'mood_state') THEN
        INSERT INTO discourse_schedule (
            guild_id, mood, last_changed_by, last_changed_at, posts_today, last_post_at, posts_day
        )
        SELECT guild_id, mode, last_changed_by, last_changed_at, posts_today, last_post_at, posts_day
        FROM mood_state
        ON CONFLICT (guild_id) DO NOTHING;
        DROP TABLE mood_state;
    END IF;
END $$;

-- Rename legacy `mode` column to `mood` on the new table if a pre-rename deploy
-- already created the table with the old column name.
DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'discourse_schedule' AND column_name = 'mode'
    ) AND NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'discourse_schedule' AND column_name = 'mood'
    ) THEN
        ALTER TABLE discourse_schedule RENAME COLUMN mode TO mood;
    END IF;
END $$;

CREATE TABLE IF NOT EXISTS command_metrics (
    id BIGSERIAL PRIMARY KEY,
    guild_id BIGINT,
    user_id BIGINT NOT NULL,
    command TEXT NOT NULL,
    started_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    duration_ms INTEGER NOT NULL,
    ok BOOLEAN NOT NULL,
    error_class TEXT
);
CREATE INDEX IF NOT EXISTS command_metrics_ts_idx ON command_metrics (started_at DESC);

-- Chime-in feature: chime-in posting history (cooldown + daily cap tracking).
-- The listen channel is the configured discourse_channel; the on/off control
-- and cadence both come from the mood schedule (mood=off disables; chill/yaps
-- set different threshold/cap/cooldown). No separate enable table needed.
CREATE TABLE IF NOT EXISTS chimein_history (
    id BIGSERIAL PRIMARY KEY,
    guild_id BIGINT NOT NULL,
    channel_id BIGINT NOT NULL,
    posted_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    score REAL,
    vibe TEXT,
    hook TEXT
);
CREATE INDEX IF NOT EXISTS chimein_history_channel_ts_idx
    ON chimein_history (guild_id, channel_id, posted_at DESC);
CREATE TABLE IF NOT EXISTS chimein_reactions (
    id BIGSERIAL PRIMARY KEY,
    guild_id BIGINT NOT NULL,
    channel_id BIGINT NOT NULL,
    message_id BIGINT NOT NULL,
    emoji TEXT,
    reacted_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS chimein_reactions_channel_ts_idx
    ON chimein_reactions (guild_id, channel_id, reacted_at DESC);
CREATE INDEX IF NOT EXISTS command_metrics_guild_cmd_idx ON command_metrics (guild_id, command);

-- Add announced_at column if missing (idempotent migration).
DO $$ BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'orders' AND column_name = 'announced_at'
    ) THEN
        ALTER TABLE orders ADD COLUMN announced_at TIMESTAMPTZ;
    END IF;
END $$;

-- Multi-channel discourse support.
CREATE TABLE IF NOT EXISTS discourse_channels (
    guild_id BIGINT NOT NULL,
    channel_id BIGINT NOT NULL,
    PRIMARY KEY (guild_id, channel_id)
);

CREATE TABLE IF NOT EXISTS discourse_channel_slots (
    guild_id BIGINT NOT NULL,
    channel_id BIGINT NOT NULL,
    posts_today INTEGER NOT NULL DEFAULT 0,
    last_post_at TIMESTAMPTZ,
    posts_day DATE,
    PRIMARY KEY (guild_id, channel_id)
);

-- Music-lounge feature: per-guild channels + slot tracking + post history.
CREATE TABLE IF NOT EXISTS music_channels (
    guild_id BIGINT NOT NULL,
    channel_id BIGINT NOT NULL,
    PRIMARY KEY (guild_id, channel_id)
);

CREATE TABLE IF NOT EXISTS music_slots (
    guild_id BIGINT NOT NULL,
    channel_id BIGINT NOT NULL,
    posts_today INTEGER NOT NULL DEFAULT 0,
    last_post_at TIMESTAMPTZ,
    posts_day DATE,
    PRIMARY KEY (guild_id, channel_id)
);

CREATE TABLE IF NOT EXISTS music_history (
    id BIGSERIAL PRIMARY KEY,
    guild_id BIGINT NOT NULL,
    topic_summary TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS music_history_guild_ts_idx ON music_history (guild_id, created_at DESC);

CREATE TABLE IF NOT EXISTS abuse_violations (
    guild_id BIGINT NOT NULL,
    user_id BIGINT NOT NULL,
    violations INTEGER NOT NULL DEFAULT 0,
    last_violation_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    silenced_at TIMESTAMPTZ,
    lifted_at TIMESTAMPTZ,
    lifted_by BIGINT,
    PRIMARY KEY (guild_id, user_id)
);
CREATE INDEX IF NOT EXISTS abuse_silenced_idx
    ON abuse_violations (guild_id)
    WHERE silenced_at IS NOT NULL AND lifted_at IS NULL;

-- Migrate legacy discourse_channel setting (single channel) to discourse_channels.
-- NOT EXISTS guard: once a guild has ANY row in discourse_channels (from this
-- migration or from /menu), we never re-seed it, so removing a channel via /menu
-- won't get undone on the next boot.
DO $$
BEGIN
    INSERT INTO discourse_channels (guild_id, channel_id)
    SELECT s.guild_id, (s.value #>> '{}')::BIGINT
    FROM settings s
    WHERE s.key = 'discourse_channel'
      AND s.value IS NOT NULL
      AND jsonb_typeof(s.value) = 'number'
      AND NOT EXISTS (
          SELECT 1 FROM discourse_channels dc WHERE dc.guild_id = s.guild_id
      )
    ON CONFLICT (guild_id, channel_id) DO NOTHING;
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
            raise RuntimeError("db not connected, call DB.connect() first")
        return self.pool

    # ---- internal: cached-plan retry wrapper -----------------------------------
    # asyncpg keeps per-connection prepared statements. When the schema changes
    # under a long-lived pool connection (column rename, new table, etc.) the
    # cached plan raises InvalidCachedStatementError. The bad cache entry is
    # discarded as part of the failure, so a single retry rebuilds the prepared
    # statement and succeeds. Zero cost on the happy path; one extra round-trip
    # on the (very rare) error path. Permanent statement_cache_size=0 was
    # considered and rejected (10% per-query cost).

    async def _run(self, method: str, query: str, *args: Any, **kwargs: Any) -> Any:
        pool = self._pool()
        fn = getattr(pool, method)
        try:
            return await fn(query, *args, **kwargs)
        except asyncpg.exceptions.InvalidCachedStatementError:
            # The bad cache entry has already been evicted; one more try rebuilds it.
            log.warning(
                "asyncpg cached plan invalid for %s; retrying once. op=%s",
                method, sql_op(query),
            )
            return await fn(query, *args, **kwargs)

    async def _execute(self, query: str, *args: Any, **kwargs: Any) -> Any:
        return await self._run("execute", query, *args, **kwargs)

    async def _fetch(self, query: str, *args: Any, **kwargs: Any) -> Any:
        return await self._run("fetch", query, *args, **kwargs)

    async def _fetchrow(self, query: str, *args: Any, **kwargs: Any) -> Any:
        return await self._run("fetchrow", query, *args, **kwargs)

    async def _fetchval(self, query: str, *args: Any, **kwargs: Any) -> Any:
        return await self._run("fetchval", query, *args, **kwargs)

    # ---- servers ----------------------------------------------------------------

    async def ensure_server(self, guild_id: int) -> None:
        await self._execute(
            "INSERT INTO servers (guild_id) VALUES ($1) ON CONFLICT (guild_id) DO NOTHING",
            guild_id,
        )

    async def is_configured(self, guild_id: int) -> bool:
        row = await self._fetchrow(
            "SELECT configured FROM servers WHERE guild_id = $1", guild_id
        )
        return bool(row and row["configured"])

    async def mark_configured(self, guild_id: int) -> None:
        await self._execute(
            """
            INSERT INTO servers (guild_id, configured, configured_at)
            VALUES ($1, TRUE, NOW())
            ON CONFLICT (guild_id) DO UPDATE
                SET configured = TRUE, configured_at = COALESCE(servers.configured_at, NOW())
            """,
            guild_id,
        )

    async def set_kitchen_open(self, guild_id: int, open_: bool) -> None:
        await self._execute(
            """
            INSERT INTO servers (guild_id, kitchen_open) VALUES ($1, $2)
            ON CONFLICT (guild_id) DO UPDATE SET kitchen_open = EXCLUDED.kitchen_open
            """,
            guild_id, open_,
        )

    async def is_kitchen_open(self, guild_id: int) -> bool:
        row = await self._fetchrow(
            "SELECT kitchen_open FROM servers WHERE guild_id = $1", guild_id
        )
        # Default open if no row yet, /menu hasn't been run but we shouldn't block.
        return True if row is None else bool(row["kitchen_open"])

    # ---- settings ---------------------------------------------------------------

    async def get_setting(self, guild_id: int, key: str) -> Any:
        row = await self._fetchrow(
            "SELECT value FROM settings WHERE guild_id = $1 AND key = $2", guild_id, key
        )
        return row["value"] if row else None

    async def set_setting(
        self, guild_id: int, key: str, value: Any, actor_id: int | None = None
    ) -> None:
        import json

        await self._execute(
            """
            INSERT INTO settings (guild_id, key, value, updated_by, updated_at)
            VALUES ($1, $2, $3::jsonb, $4, NOW())
            ON CONFLICT (guild_id, key) DO UPDATE
                SET value = EXCLUDED.value, updated_by = EXCLUDED.updated_by, updated_at = NOW()
            """,
            guild_id, key, json.dumps(value), actor_id,
        )

    async def all_settings(self, guild_id: int) -> dict[str, Any]:
        rows = await self._fetch(
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
        rows = await self._fetch(
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
            rows = await self._fetch(
                "SELECT channel_id, category FROM feed_channels WHERE guild_id = $1 AND category = $2",
                guild_id, category,
            )
        else:
            rows = await self._fetch(
                "SELECT channel_id, category FROM feed_channels WHERE guild_id = $1", guild_id
            )
        return [(r["channel_id"], r["category"]) for r in rows]

    # ---- orders -----------------------------------------------------------------

    async def create_order(
        self, guild_id: int, requester_id: int, request_text: str, summary: str
    ) -> Order:
        row = await self._fetchrow(
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
        await self._execute(
            f"UPDATE orders SET {', '.join(sets)} WHERE id = ${len(args)}", *args
        )

    async def get_order(self, order_id: int) -> Order | None:
        row = await self._fetchrow("SELECT * FROM orders WHERE id = $1", order_id)
        return _row_to_order(row) if row else None

    async def get_order_by_issue(self, issue_number: int) -> Order | None:
        row = await self._fetchrow(
            "SELECT * FROM orders WHERE issue_number = $1", issue_number
        )
        return _row_to_order(row) if row else None

    async def in_flight_orders(self, guild_id: int) -> list[Order]:
        rows = await self._fetch(
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
        rows = await self._fetch(
            """
            SELECT * FROM orders
            WHERE guild_id = $1 AND created_at > NOW() - ($2 || ' days')::interval
            ORDER BY created_at DESC LIMIT $3
            """,
            guild_id, str(since_days), limit,
        )
        return [_row_to_order(r) for r in rows]

    async def all_orders(self, guild_id: int, limit: int = 100) -> list[Order]:
        rows = await self._fetch(
            "SELECT * FROM orders WHERE guild_id = $1 ORDER BY created_at DESC LIMIT $2",
            guild_id, limit,
        )
        return [_row_to_order(r) for r in rows]

    async def last_failed_deploy(self, guild_id: int) -> Order | None:
        """Most recent order, if it's burnt at the deploy step, we're pipeline-red."""
        row = await self._fetchrow(
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
        row = await self._fetchrow(
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
        row = await self._fetchrow(
            "SELECT count FROM rate_limits WHERE user_id=$1 AND guild_id=$2 AND command=$3 AND day=$4",
            user_id, guild_id, command, day,
        )
        return int(row["count"]) if row else 0

    async def incr_server_rate(self, guild_id: int, command: str, day: date) -> int:
        row = await self._fetchrow(
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
        row = await self._fetchrow(
            "SELECT count FROM server_rate_limits WHERE guild_id=$1 AND command=$2 AND day=$3",
            guild_id, command, day,
        )
        return int(row["count"]) if row else 0

    async def set_cooldown(self, user_id: int, guild_id: int, command: str) -> None:
        await self._execute(
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
        row = await self._fetchrow(
            "SELECT last_used_at FROM cooldowns WHERE user_id=$1 AND guild_id=$2 AND command=$3",
            user_id, guild_id, command,
        )
        return row["last_used_at"] if row else None

    # ---- discourse history ------------------------------------------------------

    async def add_discourse(self, guild_id: int, category: str, summary: str) -> None:
        await self._execute(
            "INSERT INTO discourse_history (guild_id, category, topic_summary) VALUES ($1, $2, $3)",
            guild_id, category, summary,
        )

    async def recent_discourse(
        self, guild_id: int, category: str, limit: int = 10
    ) -> list[tuple[str, datetime]]:
        """Recent topic summaries with timestamps for state-aware dedup. Last 72h."""
        rows = await self._fetch(
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
        """Recent topics across ALL categories, used by the mood scheduler's dedup."""
        rows = await self._fetch(
            """
            SELECT category, topic_summary, created_at FROM discourse_history
            WHERE guild_id = $1 AND created_at > NOW() - INTERVAL '72 hours'
            ORDER BY created_at DESC LIMIT $2
            """,
            guild_id, limit,
        )
        return [(r["category"], r["topic_summary"], r["created_at"]) for r in rows]

    async def prune_discourse(self) -> None:
        await self._execute(
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

        await self._execute(
            """
            INSERT INTO audit_log (guild_id, actor_id, action, target, before, after)
            VALUES ($1, $2, $3, $4, $5::jsonb, $6::jsonb)
            """,
            guild_id, actor_id, action, target,
            json.dumps(before) if before is not None else None,
            json.dumps(after) if after is not None else None,
        )

    async def prune_audit(self) -> None:
        await self._execute(
            "DELETE FROM audit_log WHERE timestamp < NOW() - INTERVAL '90 days'"
        )

    # ---- command metrics --------------------------------------------------------

    async def record_command(
        self,
        *,
        guild_id: int | None,
        user_id: int,
        command: str,
        duration_ms: int,
        ok: bool,
        error_class: str | None = None,
    ) -> None:
        await self._execute(
            """
            INSERT INTO command_metrics
                (guild_id, user_id, command, duration_ms, ok, error_class)
            VALUES ($1, $2, $3, $4, $5, $6)
            """,
            guild_id, user_id, command, duration_ms, ok, error_class,
        )

    async def prune_command_metrics(self) -> None:
        await self._execute(
            "DELETE FROM command_metrics WHERE started_at < NOW() - INTERVAL '30 days'"
        )

    # ---- discourse channels -------------------------------------------------------

    async def get_discourse_channels(self, guild_id: int) -> list[int]:
        rows = await self._fetch(
            "SELECT channel_id FROM discourse_channels WHERE guild_id = $1", guild_id
        )
        return [r["channel_id"] for r in rows]

    async def set_discourse_channels(self, guild_id: int, channel_ids: list[int]) -> None:
        async with self._pool().acquire() as conn, conn.transaction():
            await conn.execute("DELETE FROM discourse_channels WHERE guild_id = $1", guild_id)
            if channel_ids:
                await conn.executemany(
                    "INSERT INTO discourse_channels (guild_id, channel_id) VALUES ($1, $2)",
                    [(guild_id, cid) for cid in channel_ids],
                )

    async def record_channel_slot(self, guild_id: int, channel_id: int, today: date) -> None:
        await self._execute(
            """
            INSERT INTO discourse_channel_slots (guild_id, channel_id, posts_today, posts_day, last_post_at)
            VALUES ($1, $2, 1, $3, NOW())
            ON CONFLICT (guild_id, channel_id) DO UPDATE SET
                posts_today = CASE
                    WHEN discourse_channel_slots.posts_day = EXCLUDED.posts_day
                        THEN discourse_channel_slots.posts_today + 1
                    ELSE 1
                END,
                posts_day = EXCLUDED.posts_day,
                last_post_at = NOW()
            """,
            guild_id, channel_id, today,
        )

    async def get_channel_slot(
        self, guild_id: int, channel_id: int,
    ) -> tuple[int, datetime | None, date | None]:
        row = await self._fetchrow(
            "SELECT posts_today, last_post_at, posts_day FROM discourse_channel_slots "
            "WHERE guild_id = $1 AND channel_id = $2",
            guild_id, channel_id,
        )
        if not row:
            return (0, None, None)
        return (row["posts_today"], row["last_post_at"], row["posts_day"])

    # ---- chime-in ---------------------------------------------------------------

    async def record_chimein(
        self,
        guild_id: int,
        channel_id: int,
        *,
        score: float | None = None,
        vibe: str | None = None,
        hook: str | None = None,
    ) -> None:
        await self._execute(
            """
            INSERT INTO chimein_history (guild_id, channel_id, score, vibe, hook)
            VALUES ($1, $2, $3, $4, $5)
            """,
            guild_id, channel_id, score, vibe, hook,
        )

    async def last_chimein_at(
        self, guild_id: int, channel_id: int,
    ) -> datetime | None:
        row = await self._fetchrow(
            """
            SELECT MAX(posted_at) AS last_at FROM chimein_history
            WHERE guild_id = $1 AND channel_id = $2
            """,
            guild_id, channel_id,
        )
        return row["last_at"] if row else None

    async def chimein_count_today(self, guild_id: int, channel_id: int) -> int:
        row = await self._fetchrow(
            """
            SELECT COUNT(*) AS n FROM chimein_history
            WHERE guild_id = $1 AND channel_id = $2
              AND posted_at > NOW() - INTERVAL '24 hours'
            """,
            guild_id, channel_id,
        )
        return int(row["n"]) if row else 0

    async def record_reaction(
        self, guild_id: int, channel_id: int, message_id: int, emoji: str,
    ) -> None:
        await self._execute(
            """
            INSERT INTO chimein_reactions (guild_id, channel_id, message_id, emoji)
            VALUES ($1, $2, $3, $4)
            """,
            guild_id, channel_id, message_id, emoji,
        )

    async def last_reaction_at(
        self, guild_id: int, channel_id: int,
    ) -> datetime | None:
        row = await self._fetchrow(
            """
            SELECT MAX(reacted_at) AS last_at FROM chimein_reactions
            WHERE guild_id = $1 AND channel_id = $2
            """,
            guild_id, channel_id,
        )
        return row["last_at"] if row else None

    async def reaction_count_today(self, guild_id: int, channel_id: int) -> int:
        row = await self._fetchrow(
            """
            SELECT COUNT(*) AS n FROM chimein_reactions
            WHERE guild_id = $1 AND channel_id = $2
              AND reacted_at > NOW() - INTERVAL '24 hours'
            """,
            guild_id, channel_id,
        )
        return int(row["n"]) if row else 0

    async def prune_chimein_history(self) -> None:
        await self._execute(
            "DELETE FROM chimein_history WHERE posted_at < NOW() - INTERVAL '90 days'"
        )
        await self._execute(
            "DELETE FROM chimein_reactions WHERE reacted_at < NOW() - INTERVAL '90 days'"
        )

    # ---- discourse schedule -----------------------------------------------------

    async def get_schedule(self, guild_id: int) -> ScheduleState:
        row = await self._fetchrow(
            "SELECT * FROM discourse_schedule WHERE guild_id = $1", guild_id
        )
        if not row:
            return ScheduleState(
                guild_id=guild_id, mood=MoodMode.CHILL, last_changed_by=None,
                last_changed_at=None, posts_today=0, last_post_at=None,
            )
        return ScheduleState(
            guild_id=row["guild_id"],
            mood=MoodMode(row["mood"]),
            last_changed_by=row["last_changed_by"],
            last_changed_at=row["last_changed_at"],
            posts_today=row["posts_today"],
            last_post_at=row["last_post_at"],
        )

    async def set_schedule(self, guild_id: int, mood: MoodMode, actor_id: int) -> None:
        await self._execute(
            """
            INSERT INTO discourse_schedule (guild_id, mood, last_changed_by, last_changed_at)
            VALUES ($1, $2, $3, NOW())
            ON CONFLICT (guild_id) DO UPDATE
                SET mood = EXCLUDED.mood,
                    last_changed_by = EXCLUDED.last_changed_by,
                    last_changed_at = NOW()
            """,
            guild_id, mood.value, actor_id,
        )

    async def record_schedule_post(self, guild_id: int, today: date) -> None:
        await self._execute(
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

    async def unannounced_terminal_orders(self) -> list[Order]:
        rows = await self._fetch(
            """
            SELECT * FROM orders
            WHERE status IN ('served', 'burnt', 'sent_back')
              AND announced_at IS NULL
            ORDER BY updated_at ASC
            """
        )
        return [_row_to_order(r) for r in rows]

    async def mark_announced(self, order_id: int) -> None:
        await self._execute(
            "UPDATE orders SET announced_at = NOW() WHERE id = $1", order_id
        )

    async def all_configured_guilds(self) -> list[int]:
        rows = await self._fetch(
            "SELECT guild_id FROM servers WHERE configured = TRUE"
        )
        return [r["guild_id"] for r in rows]

    # ---- music channels ---------------------------------------------------------

    async def get_music_channels(self, guild_id: int) -> list[int]:
        rows = await self._fetch(
            "SELECT channel_id FROM music_channels WHERE guild_id = $1", guild_id,
        )
        return [r["channel_id"] for r in rows]

    async def set_music_channels(self, guild_id: int, channel_ids: list[int]) -> None:
        async with self._pool().acquire() as conn, conn.transaction():
            await conn.execute("DELETE FROM music_channels WHERE guild_id = $1", guild_id)
            if channel_ids:
                await conn.executemany(
                    "INSERT INTO music_channels (guild_id, channel_id) VALUES ($1, $2)",
                    [(guild_id, cid) for cid in channel_ids],
                )

    async def record_music_slot(self, guild_id: int, channel_id: int, today: date) -> None:
        await self._execute(
            """
            INSERT INTO music_slots (guild_id, channel_id, posts_today, posts_day, last_post_at)
            VALUES ($1, $2, 1, $3, NOW())
            ON CONFLICT (guild_id, channel_id) DO UPDATE SET
                posts_today = CASE
                    WHEN music_slots.posts_day = EXCLUDED.posts_day
                        THEN music_slots.posts_today + 1
                    ELSE 1
                END,
                posts_day = EXCLUDED.posts_day,
                last_post_at = NOW()
            """,
            guild_id, channel_id, today,
        )

    async def get_music_slot(
        self, guild_id: int, channel_id: int,
    ) -> tuple[int, datetime | None, date | None]:
        row = await self._fetchrow(
            "SELECT posts_today, last_post_at, posts_day FROM music_slots "
            "WHERE guild_id = $1 AND channel_id = $2",
            guild_id, channel_id,
        )
        if not row:
            return (0, None, None)
        return (row["posts_today"], row["last_post_at"], row["posts_day"])

    async def add_music_history(self, guild_id: int, summary: str) -> None:
        await self._execute(
            "INSERT INTO music_history (guild_id, topic_summary) VALUES ($1, $2)",
            guild_id, summary,
        )

    async def recent_music_history(self, guild_id: int, limit: int = 15) -> list[str]:
        rows = await self._fetch(
            """
            SELECT topic_summary FROM music_history
            WHERE guild_id = $1 AND created_at > NOW() - INTERVAL '72 hours'
            ORDER BY created_at DESC LIMIT $2
            """,
            guild_id, limit,
        )
        return [r["topic_summary"] for r in rows]

    async def prune_music_history(self) -> None:
        await self._execute(
            "DELETE FROM music_history WHERE created_at < NOW() - INTERVAL '72 hours'"
        )

    # ---- abuse violations + silencing ------------------------------------

    async def record_abuse_violation(
        self, guild_id: int, user_id: int, silence_threshold: int,
    ) -> tuple[int, bool]:
        """Increment violation count. Silence at threshold if not already.

        Returns (new_count, just_silenced) where just_silenced is True only
        on the exact call that crosses the threshold (so the cog can emit
        the silenced event + send the canned quip exactly once).

        A previously-lifted user starts over: lifted_at + silenced_at are
        cleared on each new violation cycle.
        """
        row = await self._fetchrow(
            """
            INSERT INTO abuse_violations
                (guild_id, user_id, violations, last_violation_at)
            VALUES ($1, $2, 1, NOW())
            ON CONFLICT (guild_id, user_id) DO UPDATE
                SET violations = abuse_violations.violations + 1,
                    last_violation_at = NOW()
            RETURNING violations, silenced_at, lifted_at
            """,
            guild_id, user_id,
        )
        count = row["violations"]
        already_silenced = row["silenced_at"] is not None and row["lifted_at"] is None
        just_silenced = False
        if count >= silence_threshold and not already_silenced:
            await self._execute(
                """
                UPDATE abuse_violations
                SET silenced_at = NOW(), lifted_at = NULL, lifted_by = NULL
                WHERE guild_id = $1 AND user_id = $2
                """,
                guild_id, user_id,
            )
            just_silenced = True
        return count, just_silenced

    async def is_user_silenced(self, guild_id: int, user_id: int) -> bool:
        val = await self._fetchval(
            """
            SELECT 1 FROM abuse_violations
            WHERE guild_id = $1 AND user_id = $2
              AND silenced_at IS NOT NULL AND lifted_at IS NULL
            """,
            guild_id, user_id,
        )
        return val is not None

    async def lift_silence(self, guild_id: int, user_id: int, lifted_by: int) -> bool:
        """Mod-triggered un-silence. Returns True if user was actually silenced."""
        row = await self._fetchrow(
            """
            UPDATE abuse_violations
            SET lifted_at = NOW(), lifted_by = $3,
                violations = 0
            WHERE guild_id = $1 AND user_id = $2
              AND silenced_at IS NOT NULL AND lifted_at IS NULL
            RETURNING user_id
            """,
            guild_id, user_id, lifted_by,
        )
        return row is not None

    async def list_silenced(
        self, guild_id: int,
    ) -> list[tuple[int, int, Any]]:
        """All currently-silenced users in a guild as (user_id, violations, silenced_at)."""
        rows = await self._fetch(
            """
            SELECT user_id, violations, silenced_at
            FROM abuse_violations
            WHERE guild_id = $1
              AND silenced_at IS NOT NULL AND lifted_at IS NULL
            ORDER BY silenced_at DESC
            """,
            guild_id,
        )
        return [(r["user_id"], r["violations"], r["silenced_at"]) for r in rows]

    async def manually_silence_user(
        self, guild_id: int, user_id: int, silence_threshold: int,
    ) -> bool:
        """Mod-triggered silence via /ignore add. Upserts the row and marks
        silenced_at NOW(). Bumps violations to the threshold so the row also
        shows up in /ignore violations. Returns True if state changed
        (False if already actively silenced)."""
        row = await self._fetchrow(
            """
            INSERT INTO abuse_violations
                (guild_id, user_id, violations, last_violation_at, silenced_at)
            VALUES ($1, $2, $3, NOW(), NOW())
            ON CONFLICT (guild_id, user_id) DO UPDATE
                SET violations = GREATEST(abuse_violations.violations, $3),
                    silenced_at = COALESCE(
                        CASE WHEN abuse_violations.lifted_at IS NULL
                             THEN abuse_violations.silenced_at
                             ELSE NULL END,
                        NOW()
                    ),
                    lifted_at = NULL,
                    lifted_by = NULL,
                    last_violation_at = NOW()
            RETURNING (xmax = 0) AS inserted, silenced_at
            """,
            guild_id, user_id, silence_threshold,
        )
        return row is not None

    async def list_abuse_violations(
        self, guild_id: int, min_count: int = 1, limit: int = 50,
    ) -> list[tuple[int, int, Any, Any]]:
        """All users with at least `min_count` violations (silenced or not).

        Returns (user_id, violations, last_violation_at, silenced_at).
        silenced_at is None for users below the silence threshold or who
        have been lifted (violations was reset to 0 on lift, so a lifted
        user won't appear here unless they reoffend).
        """
        rows = await self._fetch(
            """
            SELECT user_id, violations, last_violation_at, silenced_at
            FROM abuse_violations
            WHERE guild_id = $1 AND violations >= $2
              AND lifted_at IS NULL
            ORDER BY violations DESC, last_violation_at DESC
            LIMIT $3
            """,
            guild_id, min_count, limit,
        )
        return [
            (r["user_id"], r["violations"], r["last_violation_at"], r["silenced_at"])
            for r in rows
        ]


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
        announced_at=row.get("announced_at"),
    )
