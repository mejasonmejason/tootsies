"""Unit tests for utils.abuse_tracker: violation counting, silencing, events.

Detection itself lives on ClaudeClient.classify_abuse (Haiku); covered by
tests/test_claude_client.py. This file only exercises the bookkeeping layer.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator

import pytest

from utils import abuse_tracker


@pytest.fixture(autouse=True)
def reset_tracker() -> Iterator[None]:
    """Wipe in-memory state between tests so they don't bleed into each other."""
    abuse_tracker._violations.clear()
    abuse_tracker._silenced.clear()
    yield
    abuse_tracker._violations.clear()
    abuse_tracker._silenced.clear()


# ---- violation counting ----------------------------------------------------------


def test_record_violation_increments() -> None:
    count = abuse_tracker.record_violation(1, 100)
    assert count == 1
    count = abuse_tracker.record_violation(1, 100)
    assert count == 2


def test_record_violation_independent_per_user() -> None:
    abuse_tracker.record_violation(1, 100)
    abuse_tracker.record_violation(1, 100)
    assert abuse_tracker.get_violations(1, 100) == 2
    assert abuse_tracker.get_violations(1, 200) == 0


def test_record_violation_independent_per_guild() -> None:
    abuse_tracker.record_violation(100, 1)
    abuse_tracker.record_violation(100, 1)
    assert abuse_tracker.get_violations(100, 1) == 2
    assert abuse_tracker.get_violations(200, 1) == 0


# ---- silencing -------------------------------------------------------------------


def test_silenced_after_threshold() -> None:
    for _ in range(abuse_tracker.ABUSE_THRESHOLD):
        abuse_tracker.record_violation(1, 1)
    assert abuse_tracker.is_silenced(1, 1)


def test_not_silenced_below_threshold() -> None:
    for _ in range(abuse_tracker.ABUSE_THRESHOLD - 1):
        abuse_tracker.record_violation(1, 1)
    assert not abuse_tracker.is_silenced(1, 1)


def test_silenced_does_not_spread_to_other_users() -> None:
    for _ in range(abuse_tracker.ABUSE_THRESHOLD):
        abuse_tracker.record_violation(1, 10)
    assert abuse_tracker.is_silenced(1, 10)
    assert not abuse_tracker.is_silenced(1, 20)


def test_silenced_does_not_spread_to_other_guilds() -> None:
    for _ in range(abuse_tracker.ABUSE_THRESHOLD):
        abuse_tracker.record_violation(100, 1)
    assert abuse_tracker.is_silenced(100, 1)
    assert not abuse_tracker.is_silenced(200, 1)


# ---- warn threshold --------------------------------------------------------------


def test_warn_at_below_silence_threshold() -> None:
    assert abuse_tracker.WARN_AT < abuse_tracker.ABUSE_THRESHOLD


def test_warn_at_count_not_yet_silenced() -> None:
    for _ in range(abuse_tracker.WARN_AT):
        abuse_tracker.record_violation(1, 1)
    assert abuse_tracker.get_violations(1, 1) == abuse_tracker.WARN_AT
    assert not abuse_tracker.is_silenced(1, 1)


# ---- event emission --------------------------------------------------------------


def test_abuse_silenced_event_emitted(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO, logger="tootsies.events")
    for _ in range(abuse_tracker.ABUSE_THRESHOLD):
        abuse_tracker.record_violation(42, 99)
    messages = [r.getMessage() for r in caplog.records if r.name == "tootsies.events"]
    assert any("abuse_silenced" in m for m in messages)


def test_abuse_warned_event_emitted(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO, logger="tootsies.events")
    for _ in range(abuse_tracker.WARN_AT):
        abuse_tracker.record_violation(42, 99)
    messages = [r.getMessage() for r in caplog.records if r.name == "tootsies.events"]
    assert any("abuse_warned" in m for m in messages)


def test_abuse_silenced_event_emitted_exactly_once(caplog: pytest.LogCaptureFixture) -> None:
    """Silence event fires exactly once at threshold even if record_violation is called more."""
    caplog.set_level(logging.INFO, logger="tootsies.events")
    for _ in range(abuse_tracker.ABUSE_THRESHOLD + 2):
        abuse_tracker.record_violation(42, 99)
    silence_events = [
        r for r in caplog.records
        if r.name == "tootsies.events" and "abuse_silenced" in r.getMessage()
    ]
    assert len(silence_events) == 1
