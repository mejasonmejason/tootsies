"""Tests for utils.events.emit, JSON shape, EVENT prefix, None stripping."""

from __future__ import annotations

import json
import logging

import pytest

from utils.events import emit


@pytest.fixture
def event_log(caplog: pytest.LogCaptureFixture) -> pytest.LogCaptureFixture:
    caplog.set_level(logging.INFO, logger="tootsies.events")
    return caplog


def _payload(record_message: str) -> dict[str, object]:
    """Extract the JSON portion after the `EVENT ` prefix."""
    assert record_message.startswith("EVENT "), record_message
    return json.loads(record_message[len("EVENT "):])


def test_emit_writes_event_prefixed_json(event_log: pytest.LogCaptureFixture) -> None:
    emit("command", cmd="ask", duration_ms=412, ok=True)
    msgs = [r.getMessage() for r in event_log.records]
    assert len(msgs) == 1
    payload = _payload(msgs[0])
    assert payload["event"] == "command"
    assert payload["cmd"] == "ask"
    assert payload["duration_ms"] == 412
    assert payload["ok"] is True
    assert "ts" in payload


def test_emit_strips_none_fields(event_log: pytest.LogCaptureFixture) -> None:
    """Critical: a successful command should not emit `"error":null`, since that
    would false-positive against an "all errors" dashboard query filtering on the
    substring `error`. Same for user_id=None on server-scope rate limits, etc.
    """
    emit("command", cmd="recap", ok=True, error=None, user_id=None)
    payload = _payload(event_log.records[0].getMessage())
    assert "error" not in payload
    assert "user_id" not in payload
    assert payload["ok"] is True


def test_emit_preserves_falsy_non_none_values(event_log: pytest.LogCaptureFixture) -> None:
    """Strip None only, keep 0, False, "" since those are real values."""
    emit("command", cmd="ask", duration_ms=0, ok=False)
    payload = _payload(event_log.records[0].getMessage())
    assert payload["duration_ms"] == 0
    assert payload["ok"] is False


def test_emit_serializes_non_native_types_via_default_str(
    event_log: pytest.LogCaptureFixture,
) -> None:
    """asyncpg returns Decimal, datetime, etc, default=str rescues them."""
    from datetime import UTC, datetime

    emit("test", when=datetime(2026, 5, 24, tzinfo=UTC))
    payload = _payload(event_log.records[0].getMessage())
    when = payload["when"]
    assert isinstance(when, str)
    assert "2026-05-24" in when


def test_emit_includes_iso_timestamp(event_log: pytest.LogCaptureFixture) -> None:
    emit("ping")
    payload = _payload(event_log.records[0].getMessage())
    ts = payload["ts"]
    assert isinstance(ts, str)
    assert "T" in ts  # ISO format
    assert ts.endswith("+00:00") or ts.endswith("Z")
