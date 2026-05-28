"""Tests for utils.abuse_tracker: the thin event-emitting facade over db.DB.

Detection itself lives on ClaudeClient.classify_abuse (Haiku); covered by
tests/test_claude_client.py. The DB-level persistence is unit-tested via
the cached-plan wrapper in tests/test_db.py. Here we just verify that the
facade emits the right events at the right thresholds.
"""

from __future__ import annotations

import logging
from unittest.mock import AsyncMock

import pytest

from utils import abuse_tracker


def _fake_db(count: int, just_silenced: bool) -> AsyncMock:
    db = AsyncMock()
    db.record_abuse_violation = AsyncMock(return_value=(count, just_silenced))
    return db


async def test_record_violation_warn_emits_warned_event(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger="tootsies.events")
    db = _fake_db(count=abuse_tracker.WARN_AT, just_silenced=False)
    count = await abuse_tracker.record_violation(db, guild_id=1, user_id=10)
    assert count == abuse_tracker.WARN_AT
    msgs = [r.getMessage() for r in caplog.records if r.name == "tootsies.events"]
    assert any("abuse_warned" in m for m in msgs)
    assert not any("abuse_silenced" in m for m in msgs)


async def test_record_violation_threshold_emits_silenced_event(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger="tootsies.events")
    db = _fake_db(count=abuse_tracker.ABUSE_THRESHOLD, just_silenced=True)
    count = await abuse_tracker.record_violation(db, guild_id=1, user_id=10)
    assert count == abuse_tracker.ABUSE_THRESHOLD
    msgs = [r.getMessage() for r in caplog.records if r.name == "tootsies.events"]
    assert any("abuse_silenced" in m for m in msgs)


async def test_record_violation_first_offense_no_event(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Single offense neither warns nor silences (constitution handles it)."""
    caplog.set_level(logging.INFO, logger="tootsies.events")
    db = _fake_db(count=1, just_silenced=False)
    await abuse_tracker.record_violation(db, guild_id=1, user_id=10)
    msgs = [r.getMessage() for r in caplog.records if r.name == "tootsies.events"]
    assert not any("abuse_warned" in m for m in msgs)
    assert not any("abuse_silenced" in m for m in msgs)


async def test_record_violation_already_silenced_no_duplicate_event(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Once silenced, additional violations don't re-emit silenced."""
    caplog.set_level(logging.INFO, logger="tootsies.events")
    db = _fake_db(count=abuse_tracker.ABUSE_THRESHOLD + 1, just_silenced=False)
    await abuse_tracker.record_violation(db, guild_id=1, user_id=10)
    msgs = [r.getMessage() for r in caplog.records if r.name == "tootsies.events"]
    assert not any("abuse_silenced" in m for m in msgs)


async def test_is_silenced_delegates_to_db() -> None:
    db = AsyncMock()
    db.is_user_silenced = AsyncMock(return_value=True)
    assert await abuse_tracker.is_silenced(db, guild_id=1, user_id=10) is True
    db.is_user_silenced.assert_awaited_once_with(1, 10)


def test_thresholds_ordered() -> None:
    assert abuse_tracker.WARN_AT < abuse_tracker.ABUSE_THRESHOLD
