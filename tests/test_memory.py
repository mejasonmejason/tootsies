"""Tests for long-term memory: schedule gates, the fenced prompts, and the
/forget redaction matcher.

No live DB or API: schedule logic + name matching are pure functions, and the
Claude memory methods are exercised by patching _call (same pattern as the
preflight/ask tests).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch
from zoneinfo import ZoneInfo

import pytest

from claude_client import ClaudeClient
from cogs.memory import (
    WEEKLY_ROLLUP_WEEKDAY,
    daily_due,
    hourly_due,
    hourly_window,
    weekly_due,
)
from db import _name_in_text

ET = ZoneInfo("America/New_York")


# ---- schedule gates ---------------------------------------------------------


def _et(year: int, month: int, day: int, hour: int, minute: int = 0) -> datetime:
    return datetime(year, month, day, hour, minute, tzinfo=ET)


def test_hourly_due_with_no_prior_note() -> None:
    assert hourly_due(_et(2026, 5, 29, 4, 30), None) is True


def test_hourly_not_due_when_recent() -> None:
    now = _et(2026, 5, 29, 16, 30)
    # Written 20 min ago, inside the ~hour min-gap.
    assert hourly_due(now, now - timedelta(minutes=20)) is False


def test_hourly_due_after_an_hour() -> None:
    now = _et(2026, 5, 29, 16, 30)
    # ~1h ago clears the 55-min gap.
    assert hourly_due(now, now - timedelta(minutes=60)) is True


def test_hourly_window_defaults_to_one_hour() -> None:
    now = _et(2026, 5, 29, 16, 0)
    # No prior note, or a sub-hour gap, floors at the nominal hour.
    assert hourly_window(now, None) == timedelta(hours=1)
    assert hourly_window(now, now - timedelta(minutes=30)) == timedelta(hours=1)


def test_hourly_window_tiles_the_gap_and_caps() -> None:
    now = _et(2026, 5, 29, 16, 0)
    # A 90-min gap (a missed tick) is covered with no hole.
    assert hourly_window(now, now - timedelta(minutes=90)) == timedelta(minutes=90)
    # A long outage is capped, not unbounded.
    assert hourly_window(now, now - timedelta(hours=10)) == timedelta(hours=3)


def test_daily_due_after_rollup_time_no_prior() -> None:
    assert daily_due(_et(2026, 5, 29, 5, 30), None) is True


def test_daily_not_due_before_rollup_time() -> None:
    assert daily_due(_et(2026, 5, 29, 3, 0), None) is False


def test_daily_not_due_when_just_rolled() -> None:
    now = _et(2026, 5, 29, 6, 0)
    assert daily_due(now, now - timedelta(hours=2)) is False


def test_weekly_only_on_rollup_weekday() -> None:
    d = _et(2026, 5, 25, 6, 0)  # Monday
    while d.weekday() == WEEKLY_ROLLUP_WEEKDAY:
        d += timedelta(days=1)
    assert weekly_due(d, None) is False


def test_weekly_due_on_rollup_weekday_after_time() -> None:
    d = _et(2026, 5, 25, 6, 0)
    while d.weekday() != WEEKLY_ROLLUP_WEEKDAY:
        d += timedelta(days=1)
    assert weekly_due(d, None) is True


def test_weekly_not_due_before_rollup_time() -> None:
    d = _et(2026, 5, 25, 1, 0)  # 01:00, before the rollup time
    while d.weekday() != WEEKLY_ROLLUP_WEEKDAY:
        d += timedelta(days=1)
    d = d.replace(hour=1, minute=0)
    assert weekly_due(d, None) is False


def test_weekly_not_due_when_just_rolled() -> None:
    d = _et(2026, 5, 25, 6, 0)
    while d.weekday() != WEEKLY_ROLLUP_WEEKDAY:
        d += timedelta(days=1)
    assert weekly_due(d, d - timedelta(hours=1)) is False


# ---- /forget redaction matcher ----------------------------------------------


def test_name_in_text_word_boundary() -> None:
    assert _name_in_text("alex carried the knicks slander all week", "alex")
    # "al" must not match inside "always" / "slander".
    assert not _name_in_text("always slander season", "al")


def test_name_in_text_case_insensitive() -> None:
    assert _name_in_text("Alex was on one", "alex")
    assert _name_in_text("alex was on one", "Alex")


def test_name_in_text_blank_name_never_matches() -> None:
    assert not _name_in_text("anything at all", "")
    assert not _name_in_text("anything at all", "   ")


def test_name_in_text_no_false_substring() -> None:
    # display name "ana" should not match "banana" or "canada".
    assert not _name_in_text("banana republic and canada", "ana")


def test_name_in_text_unicode_normalization() -> None:
    # A display name with a combining accent, two ways: precomposed (NFC) vs
    # decomposed (NFD: base + U+0301 combining acute). /forget must match across
    # the mismatch or a user is left half-forgotten. Build both forms from code
    # points so the test never depends on how this file was saved/normalized.
    import unicodedata

    base = "Jos" + "e" + "\u0301"  # "Jose" + combining acute
    decomposed = unicodedata.normalize("NFD", base)
    precomposed = unicodedata.normalize("NFC", base)
    assert precomposed != decomposed  # sanity: genuinely different code points
    assert _name_in_text(decomposed + " carried the debate", precomposed)
    assert _name_in_text(precomposed + " carried the debate", decomposed)


# ---- fenced memory prompts --------------------------------------------------


@dataclass
class _FakeResult:
    text: str
    stop_reason: str | None = "end_turn"
    input_tokens: int = 0
    output_tokens: int = 0


@pytest.fixture
def client() -> ClaudeClient:
    return ClaudeClient(api_key="test")


@pytest.mark.asyncio
async def test_memory_note_uses_sonnet_and_fences_inference(
    client: ClaudeClient,
) -> None:
    # The writer runs on Sonnet, not Haiku: the fence eval showed Haiku leaked
    # stated sensitive disclosures (health, coming-out) past the fence even after
    # the wording was tightened; Sonnet holds it. See scripts/eval_memory_fence.py.
    from claude_client import SONNET

    fake = AsyncMock(return_value=_FakeResult(text="alex drove the knicks debate."))
    with patch.object(client, "_call", fake):
        out = await client.memory_note("#general:\n[2h ago] alex: knicks in 6")
    kwargs = fake.call_args.kwargs
    assert kwargs["model"] == SONNET
    assert kwargs["purpose"] == "memory_hourly"
    # No web_search / tools for a private memory pass.
    assert kwargs.get("tools") is None
    system = kwargs["system_extra"].lower()
    # The fence is load-bearing: it must forbid trait inference and transcripts,
    # AND refuse to retain sensitive disclosures even when stated outright.
    assert "infer" in system or "guess" in system
    assert "transcript" in system or "quoting full messages" in system
    assert "even if stated" in system
    assert out == "alex drove the knicks debate."


@pytest.mark.asyncio
async def test_memory_note_passes_forgotten_names_into_prompt(
    client: ClaudeClient,
) -> None:
    fake = AsyncMock(return_value=_FakeResult(text="note"))
    with patch.object(client, "_call", fake):
        await client.memory_note("blob", forgotten_names=["ghostuser"])
    system = fake.call_args.kwargs["system_extra"]
    assert "ghostuser" in system
    assert "forgotten" in system.lower()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("period", "lower_word"),
    [("daily", "hourly"), ("weekly", "daily")],
)
async def test_memory_rollup_uses_sonnet_and_period(
    client: ClaudeClient, period: str, lower_word: str,
) -> None:
    # Rollups run on Sonnet (not Haiku like the hourly writer): far fewer of
    # them, they produce the durable daily/weekly tiers, and compacting many
    # notes while honoring the fence wants the stronger judgment.
    from claude_client import SONNET

    fake = AsyncMock(return_value=_FakeResult(text="arc"))
    with patch.object(client, "_call", fake):
        await client.memory_rollup("note 1\n\nnote 2", period=period)
    kwargs = fake.call_args.kwargs
    assert kwargs["model"] == SONNET
    assert kwargs["purpose"] == f"memory_{period}"
    # The rollup prompt compacts the tier BELOW it.
    assert lower_word in kwargs["system_extra"].lower()


@pytest.mark.asyncio
async def test_memory_note_span_label_flows_into_prompt(client: ClaudeClient) -> None:
    # The backfill reuses memory_note with a span label other than the hourly
    # default; it must reach the prompt so the model frames the window right.
    fake = AsyncMock(return_value=_FakeResult(text="note"))
    with patch.object(client, "_call", fake):
        await client.memory_note("blob", span_label="this week")
    system = fake.call_args.kwargs["system_extra"]
    assert "this week" in system


def test_remember_ranges_cover_the_choices() -> None:
    from cogs.memory import REMEMBER_RANGES

    assert REMEMBER_RANGES == {"week": 7, "month": 30, "2months": 60}


@pytest.mark.asyncio
async def test_backfill_window_skips_when_span_already_covered() -> None:
    # Idempotency: if a note already overlaps the span, the backfill must skip
    # without spending a Claude call or writing a duplicate.
    from datetime import datetime as _dt

    from cogs.memory import Memory

    cog = Memory.__new__(Memory)  # bypass __init__ so no scheduler loop starts
    cog.bot = MagicMock()
    cog.bot.db.has_memory_note_overlapping = AsyncMock(return_value=True)
    cog.bot.db.add_memory_note = AsyncMock()
    cog.bot.claude.memory_note = AsyncMock()

    start = _dt(2026, 5, 1, tzinfo=ET)
    end = _dt(2026, 5, 2, tzinfo=ET)
    wrote = await cog._backfill_window(
        MagicMock(), [1], MagicMock(), "daily", start, end, 200, "this day", [],
    )
    assert wrote is False
    cog.bot.claude.memory_note.assert_not_called()
    cog.bot.db.add_memory_note.assert_not_called()


@pytest.mark.asyncio
async def test_ask_injects_memory_context(client: ClaudeClient) -> None:
    fake = AsyncMock(return_value=MagicMock(text="answer", web_search_urls=[]))
    with patch.object(client, "_call", fake):
        await client.ask("q", memory_context="[recently] alex hates drake")
    user_message = fake.call_args.kwargs["user_message"]
    assert "alex hates drake" in user_message
    assert "REMEMBER" in user_message
