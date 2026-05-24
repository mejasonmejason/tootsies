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
      Required: model, purpose, duration_ms, ok
      On success: input_tokens, output_tokens, stop_reason
      Diagnostics (added to debug hallucinations + tool-use behavior):
        response_preview     first ~200 chars of model output
        response_chars       full char count of output
        tool_calls           list of {name, query} dicts for each tool_use
                             block in the response (None if no tools fired)
        tool_call_count      int count of tool invocations this call
        had_tools_available  bool, did we even pass tools= to the call
      The diagnostics let log-monitors answer "did web_search fire on a
      sports claim?" without re-running the call. tool_call_count=0 on a
      hallucinated mood_post means the model ignored the VERIFY rule;
      tool_call_count>=1 but the output is still wrong means the search
      returned bad data.
  - order_state        : /order pipeline state change (cogs/order.py)
      order_id, issue_number, guild_id, user_id, from, to
  - rate_limit_hit     : a user or server bumped a daily cap (utils/rate_limits.py)
      scope (user|server), command, user_id, guild_id, count, cap
  - deploy_event       : bot startup or shutdown
      kind (boot|shutdown), guilds, commit (if known)
  - error              : caught exception in a cog or the global error handler
      Required: source (e.g. `ask`, `order_preflight`, `undo`), error (exception class).
      Optional:
        guild_id, user_id, command, order_id, category, ...
        recoverable: bool   true = caught + retried/skipped cleanly with no
                            user-visible failure; false = caused a deflection
                            or undelivered response. Lets log-monitor agents
                            triage urgency.
        context: dict       small operation snapshot when the error fired,
                            e.g. {"had_image_urls": 3, "model": "haiku"}.
                            Keep PII out (no raw user input, no SQL params).
        traceback: list[str] last 3 stack frames, truncated. Auto-populated
                             by emit_error() below. Inline so log-monitor
                             agents don't have to grep two log streams to
                             correlate the EVENT with its traceback.
  - recap_deflected    : /recap fell back to the "dead channel" quip (truly zero messages)
      guild_id, user_id, period, channel_id, channel_name,
      reason (`no_permission` | `no_messages`),
      can_read_history, total_messages
  - discourse_fallback : /discourse fell back to the canned fallback quip
      guild_id, user_id, category, source_count, recent_topic_count, reason
  - chimein_evaluated  : chime-in considered a channel buffer and decided
      decision (mood_off_gate | hours_gate | cooldown_gate | daily_cap_gate |
                vibe_gate | threshold_gate | empty_generation),
      guild_id, channel_id, optional: score, vibe, count_today,
      local_hour_et, mood
  - chimein_posted     : chime-in actually posted a take
      guild_id, channel_id, score, vibe, hook, mood
  - link_enrich        : per-URL social-link enrichment attempt (utils/link_enrich.py)
      platform (twitter|tiktok|youtube|reddit|bluesky), url_host, ok,
      duration_ms, cache_hit

Railway dashboards: filter logs by `EVENT ` then group by the `event` field for
counts, or extract numeric fields (`duration_ms`, `input_tokens`) for percentiles.
"""

from __future__ import annotations

import json
import logging
import traceback as _traceback
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


def emit_error(
    *,
    source: str,
    exc: BaseException,
    recoverable: bool = False,
    context: dict[str, Any] | None = None,
    **extra: Any,
) -> None:
    """Emit a structured `error` event with traceback + optional context.

    This is the preferred way to emit errors from caught-exception paths.
    A single emit_error() call carries enough detail for a downstream
    log-monitor agent to identify the exception class, the source function,
    the top stack frames, whether the bot recovered, and any operation
    snapshot context (had_image_urls, model name, etc.).

    Args:
        source: cog/function label, e.g. "ask_mention", "chimein_score".
            This is the primary signature dimension log-monitors group on.
        exc: the caught exception. Class name AND traceback frames are
            extracted automatically.
        recoverable: True if the bot caught the exception and recovered
            (retry succeeded, or we skipped cleanly with no user-visible
            failure). False if the error caused a deflection / undelivered
            response / failed request. Lets log-monitors triage urgency:
            recoverable=True is informational, recoverable=False is
            user-impact.
        context: optional small dict of operation state when the error
            fired. Keep PII out (no raw user input, no full SQL params).
            Good examples: {"had_image_urls": 3, "use_web": True,
            "model": "haiku-4.5"}. Bad examples: {"user_message": "..."},
            {"sql": "INSERT INTO users VALUES ($1)"}.
        **extra: backward-compat for guild_id, user_id, command, order_id,
            category, etc. These become top-level event fields, same as
            calling emit("error", ...) directly.
    """
    payload: dict[str, Any] = {
        "source": source,
        "error": type(exc).__name__,
        "recoverable": recoverable,
        **extra,
    }
    if context:
        payload["context"] = context
    if exc.__traceback__ is not None:
        # Keep the last 3 frames (deepest = where the error actually
        # raised; closest to where the fix lives). Each `format_tb` entry
        # is a multi-line string with file/line/code, truncated to bound
        # event size since some lines can be quite long.
        frames = _traceback.format_tb(exc.__traceback__)[-3:]
        payload["traceback"] = [f.strip()[:400] for f in frames]
    emit("error", **payload)
