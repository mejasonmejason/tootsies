"""Unit tests for scripts/log_monitor.

Covers:
- EVENT line parsing into (exception_class, source) signatures
- Suppression: InvalidCachedStatementError, image-fetch BadRequest, background
  ticks (chimein_*).
- State dedupe: a signature seen 3 days ago doesn't re-file; 8 days ago does.
- Burst threshold: more than BURST_THRESHOLD occurrences in one run triggers
  a comment on the existing issue.
- End-to-end reconcile flow against a fixture, with all external CLIs mocked.

No live Railway, GitHub, or Anthropic calls happen in this suite.
"""

from __future__ import annotations

import json
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from scripts import log_monitor as lm

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "sample_logs.json"


# ---- parsing ---------------------------------------------------------------


def _load_fixture() -> list[dict[str, Any]]:
    with FIXTURE_PATH.open("r", encoding="utf-8") as f:
        return list(json.load(f))


def test_parse_event_lines_extracts_error_events_only() -> None:
    """The fixture has one non-error `command` event, plus several errors. Only
    the errors should make it through; the `command` event is ignored.
    """
    events = lm.parse_event_lines(_load_fixture())
    # 5 error events in the fixture (ask_mention, order_preflight, db_query,
    # chimein_score, image_fetch).
    assert len(events) == 5
    classes = {ev.exception_class for ev in events}
    sources = {ev.source for ev in events}
    assert "BadRequestError" in classes
    assert "ask_mention" in sources
    # command event must NOT be in here
    for ev in events:
        assert ev.payload.get("event") == "error"


def test_signature_combines_source_and_exception_class() -> None:
    """The dedupe key is (source, exception_class). Same exception in two
    different sources = two distinct signatures.
    """
    ev_a = lm.ErrorEvent(
        ts="2026-05-24T16:00:00Z",
        exception_class="BadRequestError",
        source="ask_mention",
        payload={"event": "error"},
    )
    ev_b = lm.ErrorEvent(
        ts="2026-05-24T16:01:00Z",
        exception_class="BadRequestError",
        source="order_preflight",
        payload={"event": "error"},
    )
    assert ev_a.signature != ev_b.signature
    assert ev_a.signature == "ask_mention:BadRequestError"


def test_parse_event_lines_attaches_traceback_to_preceding_event() -> None:
    """The fixture has a traceback block right after the ask_mention error.
    The parser should attach it to that event.
    """
    events = lm.parse_event_lines(_load_fixture())
    ask = next(ev for ev in events if ev.source == "ask_mention")
    assert "Traceback" in ask.traceback
    assert "BadRequestError" in ask.traceback


def test_parse_event_lines_ignores_malformed_event_json() -> None:
    """Malformed EVENT lines should be skipped, not crash the parser."""
    entries = [
        {"message": "EVENT {not valid json"},
        {"message": "EVENT {\"event\":\"error\",\"source\":\"x\",\"error\":\"E\"}"},
    ]
    events = lm.parse_event_lines(entries)
    assert len(events) == 1
    assert events[0].source == "x"


def test_extract_message_handles_alternate_keys() -> None:
    """Railway CLI versions vary on which key holds the log text. Try all common ones."""
    assert lm.extract_message({"message": "hi"}) == "hi"
    assert lm.extract_message({"text": "hi"}) == "hi"
    assert lm.extract_message({"msg": "hi"}) == "hi"
    assert lm.extract_message({}) == ""


# ---- suppression -----------------------------------------------------------


def test_suppress_invalid_cached_statement_error() -> None:
    ev = lm.ErrorEvent(
        ts="now", exception_class="InvalidCachedStatementError",
        source="db_query", payload={"event": "error"},
    )
    suppress, reason = lm.should_suppress(ev)
    assert suppress is True
    assert "suppression list" in reason


def test_suppress_image_fetch_bad_request_pattern() -> None:
    """BadRequestError with 'Unable to download' in the payload is already
    tracked as issue #15. The pattern matcher should catch it.
    """
    ev = lm.ErrorEvent(
        ts="now", exception_class="BadRequestError",
        source="image_fetch",
        payload={"event": "error", "detail": "Unable to download image from url"},
    )
    suppress, reason = lm.should_suppress(ev)
    assert suppress is True
    assert "issue #15" in reason


def test_suppress_background_chimein_sources() -> None:
    """Background tick sources (chimein_score, chimein_post, mood_scheduled)
    are noisy and not actionable per the spec.
    """
    for src in ("chimein_score", "chimein_post", "mood_scheduled"):
        ev = lm.ErrorEvent(
            ts="now", exception_class="ValueError",
            source=src, payload={"event": "error"},
        )
        suppress, reason = lm.should_suppress(ev)
        assert suppress is True, f"{src} should be suppressed"
        assert "background tick" in reason


def test_does_not_suppress_actionable_error() -> None:
    """A normal foreground error should NOT be suppressed."""
    ev = lm.ErrorEvent(
        ts="now", exception_class="TimeoutError",
        source="order_preflight", payload={"event": "error"},
    )
    suppress, _ = lm.should_suppress(ev)
    assert suppress is False


def test_group_by_signature_drops_suppressed_events() -> None:
    """The end-to-end grouper applies suppression. The fixture has 5 error
    events but 3 are suppressed (InvalidCachedStatementError, chimein_score
    ValueError, image_fetch BadRequest), so 2 distinct groups remain.
    """
    events = lm.parse_event_lines(_load_fixture())
    groups = lm.group_by_signature(events)
    assert "ask_mention:BadRequestError" in groups
    assert "order_preflight:TimeoutError" in groups
    assert "db_query:InvalidCachedStatementError" not in groups
    assert "chimein_score:ValueError" not in groups
    assert "image_fetch:BadRequestError" not in groups
    assert len(groups) == 2


# ---- state dedupe ----------------------------------------------------------


def test_needs_filing_when_signature_is_new() -> None:
    assert lm.needs_filing(None) is True


def test_does_not_refile_recent_signature() -> None:
    """Seen 3 days ago, already has an issue, threshold is 7 days = no refile."""
    now = datetime(2026, 5, 24, tzinfo=UTC)
    sig = lm.SignatureState(
        first_seen_at=(now - timedelta(days=10)).isoformat(),
        last_seen_at=(now - timedelta(days=3)).isoformat(),
        occurrence_count=12,
        issue_number=42,
        issue_url="https://github.com/x/x/issues/42",
    )
    assert lm.needs_filing(sig, now=now) is False


def test_refiles_stale_signature() -> None:
    """Seen 8 days ago, threshold is 7, so we refile (likely regression)."""
    now = datetime(2026, 5, 24, tzinfo=UTC)
    sig = lm.SignatureState(
        first_seen_at=(now - timedelta(days=30)).isoformat(),
        last_seen_at=(now - timedelta(days=8)).isoformat(),
        occurrence_count=5,
        issue_number=42,
        issue_url="https://github.com/x/x/issues/42",
    )
    assert lm.needs_filing(sig, now=now) is True


def test_state_roundtrip(tmp_path: Path) -> None:
    """Dumping then loading state preserves all fields."""
    path = tmp_path / "state.json"
    state = lm.State(
        last_run_at="2026-05-24T16:00:00+00:00",
        signatures={
            "ask_mention:BadRequestError": lm.SignatureState(
                first_seen_at="2026-05-24T15:00:00+00:00",
                last_seen_at="2026-05-24T16:00:00+00:00",
                occurrence_count=3,
                issue_number=99,
                issue_url="https://github.com/x/x/issues/99",
            ),
        },
    )
    state.dump(path)
    loaded = lm.State.load(path)
    assert loaded.last_run_at == state.last_run_at
    assert "ask_mention:BadRequestError" in loaded.signatures
    sig = loaded.signatures["ask_mention:BadRequestError"]
    assert sig.issue_number == 99
    assert sig.occurrence_count == 3


def test_state_load_missing_file_returns_empty(tmp_path: Path) -> None:
    """First-ever run: no state file yet. Loader must return a fresh empty state."""
    state = lm.State.load(tmp_path / "does-not-exist.json")
    assert state.signatures == {}
    assert state.last_run_at  # something ISO-ish


# ---- reconcile flow --------------------------------------------------------


def _make_event(source: str, exc: str, *, ts: str = "2026-05-24T17:00:00+00:00") -> lm.ErrorEvent:
    return lm.ErrorEvent(
        ts=ts, exception_class=exc, source=source,
        payload={"event": "error", "source": source, "error": exc, "ts": ts},
        traceback=f"Traceback (most recent call last):\n  some frame\n{exc}: details",
    )


def test_reconcile_files_new_signature() -> None:
    """A signature not in state should be filed via the create_issue hook."""
    state = lm.State(last_run_at="2026-05-24T16:00:00+00:00", signatures={})
    groups = {"ask_mention:TimeoutError": [_make_event("ask_mention", "TimeoutError")]}

    create = MagicMock(return_value=(101, "https://github.com/x/x/issues/101"))
    comment = MagicMock()
    build_body = MagicMock(return_value="## What\nIt timed out.\n")

    lm.reconcile(
        groups=groups, state=state, dry_run=False, window_hours=2,
        create_issue=create, comment_issue=comment, build_body=build_body,
    )

    assert create.call_count == 1
    assert comment.call_count == 0
    sig_state = state.signatures["ask_mention:TimeoutError"]
    assert sig_state.issue_number == 101
    assert sig_state.issue_url is not None
    assert sig_state.issue_url.endswith("/101")
    # title format is enforced
    title_arg = create.call_args.kwargs["title"]
    assert title_arg == "[bug] ask_mention: TimeoutError (auto-filed by log-monitor)"
    # labels are right
    assert create.call_args.kwargs["labels"] == ["bug", "auto-filed"]


def test_reconcile_skips_when_signature_recent_and_below_burst() -> None:
    """Already-filed signature with low recent volume: do nothing (no file, no comment)."""
    state = lm.State(
        last_run_at="2026-05-24T16:00:00+00:00",
        signatures={
            "ask_mention:TimeoutError": lm.SignatureState(
                first_seen_at="2026-05-24T15:00:00+00:00",
                last_seen_at="2026-05-24T15:30:00+00:00",
                occurrence_count=2,
                issue_number=50,
                issue_url="https://github.com/x/x/issues/50",
            ),
        },
    )
    groups = {"ask_mention:TimeoutError": [
        _make_event("ask_mention", "TimeoutError") for _ in range(3)
    ]}

    create = MagicMock()
    comment = MagicMock()

    lm.reconcile(
        groups=groups, state=state, dry_run=False, window_hours=2,
        create_issue=create, comment_issue=comment, build_body=MagicMock(),
    )

    assert create.call_count == 0
    assert comment.call_count == 0
    # but the running count and last_seen_at should have been updated
    sig = state.signatures["ask_mention:TimeoutError"]
    assert sig.occurrence_count == 5  # 2 + 3


def test_reconcile_comments_on_existing_issue_when_burst() -> None:
    """High-frequency (>10 in this window) on an already-filed signature
    triggers a comment on the existing issue.
    """
    state = lm.State(
        last_run_at="2026-05-24T16:00:00+00:00",
        signatures={
            "ask_mention:TimeoutError": lm.SignatureState(
                first_seen_at="2026-05-24T15:00:00+00:00",
                last_seen_at="2026-05-24T15:30:00+00:00",
                occurrence_count=2,
                issue_number=50,
                issue_url="https://github.com/x/x/issues/50",
            ),
        },
    )
    # 15 occurrences this window, well over the threshold of 10.
    groups = {"ask_mention:TimeoutError": [
        _make_event("ask_mention", "TimeoutError") for _ in range(15)
    ]}

    create = MagicMock()
    comment = MagicMock()

    lm.reconcile(
        groups=groups, state=state, dry_run=False, window_hours=2,
        create_issue=create, comment_issue=comment, build_body=MagicMock(),
    )

    assert create.call_count == 0  # already filed, don't refile
    assert comment.call_count == 1
    kwargs = comment.call_args.kwargs
    assert kwargs["issue_number"] == 50
    assert "15 occurrences" in kwargs["body"]


def test_reconcile_dry_run_makes_no_external_calls() -> None:
    """In dry-run we never hit gh or Claude; state still updates locally."""
    state = lm.State(last_run_at="2026-05-24T16:00:00+00:00", signatures={})
    groups = {"ask_mention:TimeoutError": [_make_event("ask_mention", "TimeoutError")]}

    create = MagicMock()
    comment = MagicMock()
    build_body = MagicMock()

    lm.reconcile(
        groups=groups, state=state, dry_run=True, window_hours=2,
        create_issue=create, comment_issue=comment, build_body=build_body,
    )

    assert create.call_count == 0
    assert comment.call_count == 0
    assert build_body.call_count == 0
    # state still got the signature added (with the dry-run sentinel issue number)
    assert "ask_mention:TimeoutError" in state.signatures
    assert state.signatures["ask_mention:TimeoutError"].issue_number == -1


def test_reconcile_refiles_stale_signature() -> None:
    """Stale signature (last seen 8 days ago) gets a brand new issue filed."""
    old = datetime(2026, 5, 24, tzinfo=UTC) - timedelta(days=8)
    state = lm.State(
        last_run_at="2026-05-24T16:00:00+00:00",
        signatures={
            "ask_mention:TimeoutError": lm.SignatureState(
                first_seen_at=old.isoformat(),
                last_seen_at=old.isoformat(),
                occurrence_count=5,
                issue_number=10,
                issue_url="https://github.com/x/x/issues/10",
            ),
        },
    )
    groups = {"ask_mention:TimeoutError": [_make_event("ask_mention", "TimeoutError")]}

    create = MagicMock(return_value=(200, "https://github.com/x/x/issues/200"))
    comment = MagicMock()
    build_body = MagicMock(return_value="## What\nback again\n")

    lm.reconcile(
        groups=groups, state=state, dry_run=False, window_hours=2,
        create_issue=create, comment_issue=comment, build_body=build_body,
    )

    assert create.call_count == 1  # refiled
    sig = state.signatures["ask_mention:TimeoutError"]
    assert sig.issue_number == 200
    # first_seen_at preserved from the original sighting
    assert sig.first_seen_at == old.isoformat()


# ---- CLI wrappers ----------------------------------------------------------


def test_fetch_railway_logs_parses_json_lines() -> None:
    """The railway CLI emits one JSON object per line; parser tolerates blanks
    and trailing non-JSON noise.
    """
    fake_stdout = (
        '{"timestamp":"2026-05-24T16:00:00Z","message":"hi"}\n'
        '\n'
        'INFO connecting...\n'
        '{"timestamp":"2026-05-24T16:00:01Z","message":"hello"}\n'
    )
    fake_runner = MagicMock(return_value=subprocess.CompletedProcess(
        args=[], returncode=0, stdout=fake_stdout, stderr="",
    ))
    entries = lm.fetch_railway_logs(lines=10, service="tootsies", runner=fake_runner)
    assert len(entries) == 2
    assert entries[0]["message"] == "hi"


def test_fetch_railway_logs_raises_on_nonzero_exit() -> None:
    fake_runner = MagicMock(return_value=subprocess.CompletedProcess(
        args=[], returncode=1, stdout="", stderr="not authenticated",
    ))
    with pytest.raises(RuntimeError, match="railway logs failed"):
        lm.fetch_railway_logs(runner=fake_runner)


def test_gh_create_issue_extracts_number_from_url() -> None:
    fake_runner = MagicMock(return_value=subprocess.CompletedProcess(
        args=[], returncode=0,
        stdout="Creating issue in owner/repo\n\nhttps://github.com/owner/repo/issues/123\n",
        stderr="",
    ))
    n, url = lm.gh_create_issue(
        title="t", body="b", labels=["bug", "auto-filed"], runner=fake_runner,
    )
    assert n == 123
    assert url == "https://github.com/owner/repo/issues/123"


def test_gh_create_issue_raises_on_nonzero_exit() -> None:
    fake_runner = MagicMock(return_value=subprocess.CompletedProcess(
        args=[], returncode=1, stdout="", stderr="auth error",
    ))
    with pytest.raises(RuntimeError, match="gh issue create failed"):
        lm.gh_create_issue(
            title="t", body="b", labels=["bug"], runner=fake_runner,
        )


def test_gh_comment_issue_passes_args() -> None:
    fake_runner = MagicMock(return_value=subprocess.CompletedProcess(
        args=[], returncode=0, stdout="", stderr="",
    ))
    lm.gh_comment_issue(issue_number=42, body="burst noted", runner=fake_runner)
    cmd = fake_runner.call_args.args[0]
    assert "comment" in cmd
    assert "42" in cmd
    assert "burst noted" in cmd


# ---- end-to-end via main ---------------------------------------------------


def test_main_dry_run_with_fixture_writes_state(tmp_path: Path) -> None:
    """Smoke test: pointing main at the fixture in dry-run mode parses, dedups,
    and writes a state file. No external calls happen.
    """
    state_path = tmp_path / "state.json"
    rc = lm.main([
        "--dry-run",
        "--fixture", str(FIXTURE_PATH),
        "--state-path", str(state_path),
    ])
    assert rc == 0
    assert state_path.exists()
    data = json.loads(state_path.read_text())
    # 2 distinct, non-suppressed signatures in the fixture
    assert "ask_mention:BadRequestError" in data["signatures"]
    assert "order_preflight:TimeoutError" in data["signatures"]
    # the dry-run sentinel issue number
    assert data["signatures"]["ask_mention:BadRequestError"]["issue_number"] == -1


def test_main_writes_state_even_when_reconcile_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If reconcile blows up partway through, the script still writes whatever
    state changes accumulated and exits non-zero so the workflow surfaces it.
    """
    state_path = tmp_path / "state.json"

    def explode(**_kwargs: Any) -> None:
        raise RuntimeError("simulated failure")

    monkeypatch.setattr(lm, "reconcile", explode)

    rc = lm.main([
        "--dry-run",
        "--fixture", str(FIXTURE_PATH),
        "--state-path", str(state_path),
    ])
    assert rc == 1
    assert state_path.exists()
