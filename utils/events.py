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
  - discourse_skipped  : a scheduled discourse slot was dropped (no post sent)
      guild_id, channel_id, reason (rate_limited | compose_error | empty)
        rate_limited  : 429 persisted through the single retry (also emits an
                        `error` event with the exception detail).
        compose_error : compose raised a non-429 exception (also emits `error`).
        empty         : model returned blank / "EMPTY", nothing fresh to post.
  - chimein_evaluated  : chime-in considered a channel buffer and decided
      decision (mood_off_gate | hours_gate | cooldown_gate | daily_cap_gate |
                vibe_gate | threshold_gate | reacted | empty_generation),
      guild_id, channel_id, optional: score, vibe, count_today,
      local_hour_et, mood
        reacted : below post threshold but a near-miss, so Toots dropped a
                  reaction instead of a full take (see reaction_added).
  - chimein_posted     : chime-in actually posted a take
      guild_id, channel_id, score, vibe, hook, mood
  - reaction_added     : Toots reacted to a message (utils/reactions.py)
      source (e.g. `chimein`), guild_id, channel_id, message_id, emoji
  - link_enrich        : per-URL social-link enrichment attempt (utils/link_enrich.py)
      platform (twitter|tiktok|youtube|reddit|bluesky), url_host, ok,
      duration_ms, cache_hit
  - pplx_ask            : Perplexity Sonar call for /ask (utils/perplexity.py)
  - pplx_discourse      : Perplexity Sonar call for /discourse (utils/perplexity.py)
  - pplx_recap          : Perplexity Sonar call for /recap (utils/perplexity.py)
  - pplx_chimein        : Perplexity Sonar call for chime-in (utils/perplexity.py)
      All four: ok, duration_ms, input_tokens, output_tokens,
      response_chars, error (on failure)
  - link_stripped      : guardrail removed a URL from the model's output.
                         Fires once per reason: a single response can emit
                         hallucinated + redundant + dead_link events if the
                         model wrote multiple kinds of bad URL.
      purpose (discourse_manual|discourse_scheduled|ask|recap|music_post|chimein_post),
      reason (hallucinated|redundant|dead_link),
      count, urls (capped at 5)
        hallucinated : URL not in any source (feed/Perplexity/web_search).
        redundant    : URL is real but the user/room just saw it
                       (recently_seen_urls match) so re-posting would be
                       double-embed clutter.
        dead_link    : URL came from a real source but the host returned
                       404/410 (page was deleted between source-fetch and
                       post-time). Twitter status URLs check fxtwitter;
                       all other URLs are HEAD-checked.
  - discourse_scored   : post-generation quality gate for discourse posts
      guild_id, channel_id, score, reason, must_post, category,
      user_id (manual only), post_preview (first 120 chars)
  - music_scored       : post-generation quality gate for music drops (cogs/music.py)
      guild_id, channel_id, channel_name, score, reason, must_post,
      post_preview (first 120 chars)
  - music_dedup        : a composed music drop was too similar to a recent one
      guild_id, channel_id, channel_name, decision (similarity_gate), post_preview
  - music_fallback     : Claude returned EMPTY for a music drop
      guild_id, channel_id, channel_name, reason (claude_returned_empty)
  - music_link_missing : a music drop lacked an Apple Music / Spotify link
      guild_id, channel_id, channel_name, must_post, attempt (1|2), post_preview
  - market_fetch       : sports/prediction-market enricher call (utils/markets.py)
      source (sgo|polymarket|kalshi), query, ok, duration_ms,
      cache_hit, result_count, error (on failure)
  - abuse_warned       : user hit WARN_AT violations in a session (utils/abuse_tracker.py)
      guild_id, user_id, violations
  - abuse_silenced     : user hit ABUSE_THRESHOLD and is silenced for the session
      guild_id, user_id, violations
  - memory_write       : long-term memory writer ran (cogs/memory.py)
      guild_id, tier (hourly|daily|weekly), ok
      On a written note: chars, plus message_count + channel_count (hourly)
        or rolled_up (daily/weekly, # of lower-tier notes compacted).
      On a skip: skipped (low_activity|empty|truncated) instead of chars.
        truncated (ok=False) means a rollup hit max_tokens; the source notes
        were kept (not deleted) so the next window can retry. See #158.
      backfill: True when written by the /remember one-time backfill (vs the
        live scheduler), so dashboards can tell seeded notes from live ones.
  - memory_search      : the search_memory tool ran during /ask (cogs/ask.py)
      guild_id, query (<=120 chars), hits (# of notes returned)
  - memory_forget      : a user wiped themselves from memory via /forget (cogs/memory.py)
      guild_id, user_id, notes_deleted

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
