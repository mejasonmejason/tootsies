"""Unit tests for the deterministic half of the ops monitor.

Covers parse/aggregate/evaluate/render on synthetic event dicts. The Railway
I/O is integration-only and excluded from coverage.
"""

from __future__ import annotations

from scripts.ops_monitor import (
    aggregate,
    evaluate,
    parse_event_lines,
    render,
)


def test_parse_event_lines_extracts_json_after_marker() -> None:
    lines = [
        '2026-05-30 01:00 INFO tootsies.events: EVENT {"event":"command","cmd":"ask","ok":true}',
        "some non-event log line",
        'EVENT {"event":"error","source":"ask","error":"ValueError"}',
        "EVENT not-json-here",
    ]
    events = parse_event_lines(lines)
    assert len(events) == 2
    assert events[0]["cmd"] == "ask"
    assert events[1]["event"] == "error"


def test_aggregate_dedups_on_event_and_ts() -> None:
    ev = {"event": "claude_api", "ok": True, "purpose": "ask", "ts": "t1",
          "duration_ms": 100, "input_tokens": 10, "output_tokens": 5}
    agg = aggregate([ev, dict(ev)])  # same (event, ts) twice
    assert agg.purposes["ask"].n == 1
    assert agg.total_events == 1


def test_aggregate_collects_latency_tokens_and_zero_tool_calls() -> None:
    events = [
        {"event": "claude_api", "ok": True, "purpose": "chimein_post", "ts": "1",
         "duration_ms": 5000, "had_tools_available": True, "tool_call_count": 0,
         "response_preview": "a hollow take"},
        {"event": "claude_api", "ok": True, "purpose": "chimein_post", "ts": "2",
         "duration_ms": 7000, "had_tools_available": True, "tool_call_count": 1,
         "response_preview": "a grounded take"},
    ]
    agg = aggregate(events)
    st = agg.purposes["chimein_post"]
    assert st.n == 2
    assert st.avg_ms == 6000
    assert st.zero_tool_calls == 1  # only the first ran no search
    assert len(st.samples) == 2


def test_evaluate_flags_nonrecoverable_errors_as_high_with_traceback() -> None:
    agg = aggregate([
        {"event": "error", "source": "order", "error": "TimeoutError", "ts": "1",
         "traceback": ["frame a", "frame b: raise TimeoutError"],
         "context": {"model": "sonnet"}},
        {"event": "error", "source": "order", "error": "TimeoutError", "ts": "2"},
    ])
    findings = evaluate(agg)
    err = [f for f in findings if f.kind == "error"]
    assert len(err) == 1
    assert err[0].severity == "high"  # non-recoverable -> user-impacting
    assert "2x" in err[0].detail
    assert "2 non-recoverable" in err[0].detail
    assert "model" in err[0].detail  # context surfaced
    # The deepest traceback frame rides along as a sample for the judge.
    assert err[0].samples == ["frame b: raise TimeoutError"]


def test_evaluate_recoverable_error_is_low_until_it_bursts() -> None:
    # A handful of recovered errors is informational (low severity).
    agg = aggregate([
        {"event": "error", "source": "chimein_score", "error": "APITimeout",
         "recoverable": True, "ts": str(i)}
        for i in range(3)
    ])
    err = [f for f in evaluate(agg) if f.kind == "error"]
    assert len(err) == 1
    assert err[0].severity == "low"
    assert "0 non-recoverable, 3 recovered" in err[0].detail

    # But once the same recoverable signature bursts past the threshold, bump it.
    agg2 = aggregate([
        {"event": "error", "source": "chimein_score", "error": "APITimeout",
         "recoverable": True, "ts": str(i)}
        for i in range(12)  # >= BURST_ERROR_MIN (10)
    ])
    err2 = [f for f in evaluate(agg2) if f.kind == "error"]
    assert err2[0].severity == "medium"


def test_evaluate_flags_latency_over_ceiling() -> None:
    # music_post ceiling is 45s; 50s avg should flag.
    agg = aggregate([
        {"event": "claude_api", "ok": True, "purpose": "music_post", "ts": "1",
         "duration_ms": 50_000},
    ])
    findings = evaluate(agg)
    lat = [f for f in findings if f.kind == "latency"]
    assert len(lat) == 1
    assert lat[0].command == "music_post"


def test_evaluate_does_not_flag_latency_under_ceiling() -> None:
    agg = aggregate([
        {"event": "claude_api", "ok": True, "purpose": "ask", "ts": "1",
         "duration_ms": 6000},
    ])
    assert [f for f in evaluate(agg) if f.kind == "latency"] == []


def test_evaluate_flags_hallucinated_links_over_threshold() -> None:
    agg = aggregate([
        {"event": "link_stripped", "purpose": "discourse_scheduled",
         "reason": "hallucinated", "count": 3, "urls": ["http://x"], "ts": "1"},
    ])
    findings = evaluate(agg)
    hl = [f for f in findings if f.kind == "hallucinated_links"]
    assert len(hl) == 1
    assert "http://x" in hl[0].samples


def test_evaluate_ignores_few_hallucinated_links() -> None:
    agg = aggregate([
        {"event": "link_stripped", "purpose": "ask", "reason": "hallucinated",
         "count": 1, "ts": "1"},
    ])
    assert [f for f in evaluate(agg) if f.kind == "hallucinated_links"] == []


def test_evaluate_flags_ungrounded_chimein_rate() -> None:
    events = [
        {"event": "claude_api", "ok": True, "purpose": "chimein_post", "ts": str(i),
         "duration_ms": 4000, "had_tools_available": True, "tool_call_count": 0}
        for i in range(5)  # 5/5 ran no search -> 100% ungrounded
    ]
    findings = evaluate(aggregate(events))
    ung = [f for f in findings if f.kind == "ungrounded"]
    assert len(ung) == 1
    assert "100%" in ung[0].detail


def test_evaluate_skips_ungrounded_when_too_few_posts() -> None:
    events = [
        {"event": "claude_api", "ok": True, "purpose": "chimein_post", "ts": "1",
         "duration_ms": 4000, "had_tools_available": True, "tool_call_count": 0},
    ]
    assert [f for f in evaluate(aggregate(events)) if f.kind == "ungrounded"] == []


def test_evaluate_flags_command_failures_and_rate_limits() -> None:
    agg = aggregate([
        {"event": "command", "cmd": "recap", "ok": False, "ts": "1"},
        {"event": "rate_limit_hit", "command": "ask", "scope": "user", "ts": "2"},
    ])
    kinds = {f.kind for f in evaluate(agg)}
    assert "command_failure" in kinds
    assert "rate_limit" in kinds


def test_render_all_clear_when_no_findings() -> None:
    agg = aggregate([
        {"event": "claude_api", "ok": True, "purpose": "ask", "ts": "1",
         "duration_ms": 5000, "input_tokens": 10, "output_tokens": 5},
    ])
    report = render(agg, evaluate(agg))
    assert "All clear" in report
    assert "Per-purpose metrics" in report
    assert "| ask |" in report


def test_render_lists_findings_high_severity_first() -> None:
    agg = aggregate([
        {"event": "error", "source": "ask", "error": "Boom", "ts": "1"},
        {"event": "claude_api", "ok": True, "purpose": "music_post", "ts": "2",
         "duration_ms": 50_000},
    ])
    report = render(agg, evaluate(agg))
    assert report.index("[HIGH]") < report.index("[MEDIUM]")
    assert "finding(s)" in report
