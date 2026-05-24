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


# ---- emit_error -------------------------------------------------------------


from utils.events import emit_error  # noqa: E402


def _raise_and_catch() -> BaseException:
    """Helper: produce a real exception with a populated __traceback__."""
    try:
        raise ValueError("boom from a test")
    except ValueError as exc:
        return exc


def test_emit_error_extracts_class_name(event_log: pytest.LogCaptureFixture) -> None:
    exc = _raise_and_catch()
    emit_error(source="ask", exc=exc, guild_id=111, user_id=222)
    msgs = [r.getMessage() for r in event_log.records]
    payload = _payload(msgs[-1])
    assert payload["event"] == "error"
    assert payload["source"] == "ask"
    assert payload["error"] == "ValueError"
    assert payload["guild_id"] == 111
    assert payload["user_id"] == 222


def test_emit_error_inlines_traceback_frames(event_log: pytest.LogCaptureFixture) -> None:
    exc = _raise_and_catch()
    emit_error(source="ask", exc=exc)
    payload = _payload(event_log.records[-1].getMessage())
    # Traceback present + at least one frame, each frame is a str
    assert "traceback" in payload
    frames = payload["traceback"]
    assert isinstance(frames, list)
    assert len(frames) >= 1
    assert all(isinstance(f, str) for f in frames)
    # The helper that raised should appear in the frame text
    assert any("_raise_and_catch" in f for f in frames)


def test_emit_error_defaults_recoverable_false(event_log: pytest.LogCaptureFixture) -> None:
    """Default recoverable=False means the error caused a user-facing failure.
    This is the safe assumption for user-impact paths."""
    exc = _raise_and_catch()
    emit_error(source="ask", exc=exc)
    payload = _payload(event_log.records[-1].getMessage())
    assert payload["recoverable"] is False


def test_emit_error_recoverable_true_marks_background_skip(
    event_log: pytest.LogCaptureFixture,
) -> None:
    """recoverable=True is set for background-task error paths that skip cleanly
    (chimein tick, scheduler tick) so the log-monitor can deprioritize them."""
    exc = _raise_and_catch()
    emit_error(source="chimein_score", exc=exc, recoverable=True)
    payload = _payload(event_log.records[-1].getMessage())
    assert payload["recoverable"] is True


def test_emit_error_includes_context_dict(event_log: pytest.LogCaptureFixture) -> None:
    """context carries operation snapshot fields that help an agent triage."""
    exc = _raise_and_catch()
    emit_error(
        source="ask", exc=exc,
        context={"had_image_urls": 3, "model": "haiku-4.5"},
    )
    payload = _payload(event_log.records[-1].getMessage())
    assert payload["context"] == {"had_image_urls": 3, "model": "haiku-4.5"}


def test_emit_error_omits_context_when_none(event_log: pytest.LogCaptureFixture) -> None:
    """A missing context dict should not appear in the payload at all
    (don't add "context": null noise)."""
    exc = _raise_and_catch()
    emit_error(source="ask", exc=exc)
    payload = _payload(event_log.records[-1].getMessage())
    assert "context" not in payload


def test_emit_error_passes_through_extra_kwargs(
    event_log: pytest.LogCaptureFixture,
) -> None:
    """Backward-compat: existing call sites pass guild_id, user_id, command,
    category, etc. as kwargs. These land at the top level of the payload
    same as emit('error', ...) would."""
    exc = _raise_and_catch()
    emit_error(
        source="discourse", exc=exc,
        guild_id=999, user_id=888, category="nba",
    )
    payload = _payload(event_log.records[-1].getMessage())
    assert payload["guild_id"] == 999
    assert payload["user_id"] == 888
    assert payload["category"] == "nba"
