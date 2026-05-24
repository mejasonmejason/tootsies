"""Structured event emitter for log-based dashboards.

Every "metric-worthy" thing the bot does (command invocation, Claude API call,
order state change, rate limit hit) emits one JSON-encoded log line via this
helper. Each line begins with the literal prefix `EVENT ` so Railway log
queries can isolate dashboard data from operational chatter.

Schema for every event:

    EVENT {"event": "<kind>", "ts": "<iso>", ...kind-specific fields...}

Known kinds (keep this list in sync with what's emitted):

  - command            : slash command invocation (utils/metrics.py)
      cmd, user_id, guild_id, duration_ms, ok, error
  - claude_api         : Anthropic API call (claude_client.py)
      model, purpose, input_tokens, output_tokens, duration_ms, stop_reason
  - order_state        : /order pipeline state change (cogs/order.py)
      order_id, issue_number, guild_id, user_id, from, to
  - rate_limit_hit     : a user or server bumped a daily cap (utils/rate_limits.py)
      scope (user|server), command, user_id, guild_id, count, cap
  - deploy_event       : bot startup or shutdown
      kind (boot|shutdown), guilds, commit (if known)
  - error              : caught exception in a cog or the global error handler
      source (e.g. `ask`, `order_preflight`, `undo`), error (exception class),
      guild_id, user_id, plus optional context (command, order_id, category, ...)
  - recap_deflected    : /recap fell back to the "dead channel" quip (truly zero messages)
      guild_id, user_id, period, channel_id, channel_name,
      reason (`no_permission` | `no_messages`),
      can_read_history, total_messages
  - discourse_fallback : /discourse fell back to the canned fallback quip
      guild_id, user_id, category, source_count, recent_topic_count, reason
  - chipin_evaluated   : chip-in considered a channel buffer and decided
      decision (mood_off_gate | hours_gate | cooldown_gate | daily_cap_gate |
                vibe_gate | threshold_gate | empty_generation),
      guild_id, channel_id, optional: score, vibe, count_today, local_hour_et
  - chipin_posted      : chip-in actually posted a take
      guild_id, channel_id, score, vibe, hook

Railway dashboards: filter logs by `EVENT ` then group by the `event` field for
counts, or extract numeric fields (`duration_ms`, `input_tokens`) for percentiles.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from typing import Any

log = logging.getLogger("tootsies.events")


def emit(event: str, **fields: Any) -> None:
    """Emit one structured event line.

    Fields with a None value are stripped, they make JSON noisier and, more
    importantly, cause false-positive matches in log search (e.g. a successful
    command emitting `"error":null` would otherwise match an "all errors" panel
    that filters on the substring `error`).
    """
    payload = {
        "event": event,
        "ts": datetime.now(UTC).isoformat(timespec="milliseconds"),
        **{k: v for k, v in fields.items() if v is not None},
    }
    # `default=str` is a safety net for things like datetime, Decimal, or IDs that
    # asyncpg returns as non-JSON-native types.
    log.info("EVENT %s", json.dumps(payload, default=str, separators=(",", ":")))
