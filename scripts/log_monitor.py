"""Railway log monitor for the Tootsies bot.

Pulls the last ~2 hours of Railway runtime logs, parses for structured `error`
EVENT lines + python tracebacks, groups by `(exception_class, source_field)`,
and files a GitHub issue for any NEW signature (or one we haven't seen in 7+
days). For recurring signatures already filed, if frequency in this run is
high we drop a "burst noted" comment on the existing issue.

State is persisted to `.log-monitor-state.json` at the repo root and committed
back by the GitHub Actions workflow so the dedup memory survives between runs.

Designed to run from a cron-driven Actions workflow (see
.github/workflows/log-monitor.yml). The script itself is stdlib + a single
optional dependency (the `anthropic` SDK, only loaded when filing a new
issue body). External CLIs invoked: `railway` (logs) and `gh` (issues).

Usage:
    # Production run (called from Actions):
    RAILWAY_API_TOKEN=... ANTHROPIC_API_KEY=... GITHUB_TOKEN=... \
        python scripts/log_monitor.py

    # Local development, no API calls, prints what it WOULD file:
    python scripts/log_monitor.py --dry-run --fixture tests/fixtures/sample_logs.json

Exit codes:
    0  ran cleanly (state may or may not have changed)
    1  unrecoverable error (CLI missing, state file corrupt, etc.)
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

# Make repo importable when the script lives in scripts/.
REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

log = logging.getLogger("tootsies.log_monitor")

STATE_PATH = REPO_ROOT / ".log-monitor-state.json"

# How far back to look on each tick. The cron is every 30 min so 2h gives plenty
# of overlap to catch errors emitted during a tick we already ran (dedup handles
# the rest).
WINDOW_HOURS = 2
DEFAULT_LOG_LINES = 5000

# Burst threshold: more than this many occurrences in the window for an
# already-filed signature triggers a "burst noted" comment on the issue.
BURST_THRESHOLD = 10

# Re-file an old signature when its last sighting was more than this many days
# ago (likely the underlying bug came back).
REFILE_AFTER_DAYS = 7

# Source-field prefixes that are background ticks. We log errors from these for
# operational visibility but don't auto-file issues (most are flaky-net /
# transient and don't warrant a ticket per occurrence).
BACKGROUND_SOURCES = frozenset({
    "chimein_score", "chimein_post", "chimein_tick",
    "mood_scheduled", "mood_tick", "discourse_scheduled",
})

# Exception classes we never want to file, already handled elsewhere.
SUPPRESSED_EXCEPTIONS = frozenset({
    # asyncpg auto-recycles on cache invalidation; db.py wraps the retry.
    "InvalidCachedStatementError",
})

# Pattern-level suppression: (exception_class, substring_in_payload) ->
# explanation. The substring is matched against the full payload JSON (and the
# traceback if present), so we catch e.g. "Unable to download" detail messages
# coming back from the Anthropic SDK as BadRequestError.
SUPPRESSED_PATTERNS: list[tuple[str, str, str]] = [
    ("BadRequestError", "Unable to download", "tracked as issue #15"),
]


# ---- data shapes -----------------------------------------------------------


@dataclass
class ErrorEvent:
    """One structured `error` event we extracted from the logs."""
    ts: str
    exception_class: str
    source: str
    payload: dict[str, Any]
    # Optional traceback chunk (the python `Traceback (most recent call last):`
    # block) that appeared near this event in the log stream. Helpful for
    # building a useful issue body.
    traceback: str = ""

    @property
    def signature(self) -> str:
        """Stable key for dedup. `(source, exception_class)` is coarse enough
        that one logical bug groups together but fine enough that a different
        exception class in the same source surfaces separately."""
        return f"{self.source}:{self.exception_class}"


@dataclass
class SignatureState:
    """Per-signature memory: when did we first/last see it, how many times in
    THIS run, what's the issue we filed for it."""
    first_seen_at: str
    last_seen_at: str
    occurrence_count: int = 0
    issue_number: int | None = None
    issue_url: str | None = None
    # Per-run-only field, not serialized. Reset each tick.
    run_occurrences: int = field(default=0, repr=False)


@dataclass
class State:
    last_run_at: str
    signatures: dict[str, SignatureState] = field(default_factory=dict)

    @classmethod
    def load(cls, path: Path) -> State:
        if not path.exists():
            return cls(last_run_at=_now_iso(), signatures={})
        with path.open("r", encoding="utf-8") as f:
            data = json.load(f)
        sigs = {
            k: SignatureState(
                first_seen_at=v["first_seen_at"],
                last_seen_at=v["last_seen_at"],
                occurrence_count=v.get("occurrence_count", 0),
                issue_number=v.get("issue_number"),
                issue_url=v.get("issue_url"),
            )
            for k, v in data.get("signatures", {}).items()
        }
        return cls(last_run_at=data.get("last_run_at", _now_iso()), signatures=sigs)

    def dump(self, path: Path) -> None:
        payload = {
            "last_run_at": self.last_run_at,
            "signatures": {
                k: {
                    "first_seen_at": v.first_seen_at,
                    "last_seen_at": v.last_seen_at,
                    "occurrence_count": v.occurrence_count,
                    "issue_number": v.issue_number,
                    "issue_url": v.issue_url,
                }
                for k, v in sorted(self.signatures.items())
            },
        }
        path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")


# ---- log fetching ----------------------------------------------------------


def fetch_railway_logs(
    *, lines: int = DEFAULT_LOG_LINES, service: str = "tootsies",
    runner: Any = None,
) -> list[dict[str, Any]]:
    """Shell out to `railway logs` and return the parsed JSON line list.

    Each line is one JSON object as emitted by the Railway CLI. We tolerate
    blank lines and non-JSON noise (just skip them) so a malformed line never
    crashes the run.

    `runner` is injectable for tests; defaults to subprocess.run.
    """
    runner = runner or subprocess.run
    cmd = ["railway", "logs", "--service", service, "--lines", str(lines), "--json"]
    log.info("running: %s", " ".join(cmd))
    result = runner(cmd, capture_output=True, text=True, check=False, timeout=60)
    if result.returncode != 0:
        # Surface stderr but don't log the token; railway CLI shouldn't print it,
        # but be defensive.
        raise RuntimeError(
            f"railway logs failed (exit {result.returncode}): "
            f"{(result.stderr or '')[:500]}"
        )
    out: list[dict[str, Any]] = []
    for line in (result.stdout or "").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            # Some CLI versions intersperse status lines. Skip silently.
            continue
    return out


# ---- parsing ---------------------------------------------------------------


EVENT_PREFIX_RE = re.compile(r'EVENT\s+(\{.*\})\s*$')
TRACEBACK_START_RE = re.compile(r"Traceback \(most recent call last\):")


def extract_message(entry: dict[str, Any]) -> str:
    """Return the message text from a Railway log entry, regardless of shape.

    Different CLI versions emit `message`, `text`, or nest things in `body`.
    """
    for key in ("message", "text", "msg", "body", "log"):
        v = entry.get(key)
        if isinstance(v, str) and v:
            return v
    return ""


def parse_event_lines(entries: list[dict[str, Any]]) -> list[ErrorEvent]:
    """Walk parsed log entries, pull out `error` EVENT lines + nearby traceback.

    The tracebacks emitted by `log.exception(...)` print AFTER the event line
    (since events are emitted first, then the unhandled exception bubbles).
    We attach the next traceback we see within ~20 entries to the most recent
    error event we extracted, on a best-effort basis.
    """
    events: list[ErrorEvent] = []
    pending_tb_index: int | None = None  # index into events awaiting a tb
    tb_lines: list[str] = []
    tb_remaining_entries = 0

    def _flush_tb() -> None:
        nonlocal pending_tb_index, tb_lines, tb_remaining_entries
        if pending_tb_index is not None and tb_lines:
            events[pending_tb_index].traceback = "\n".join(tb_lines).strip()
        pending_tb_index = None
        tb_lines = []
        tb_remaining_entries = 0

    for entry in entries:
        msg = extract_message(entry)
        if not msg:
            continue

        m = EVENT_PREFIX_RE.search(msg)
        if m:
            try:
                payload = json.loads(m.group(1))
            except json.JSONDecodeError:
                continue
            if payload.get("event") != "error":
                continue
            exc_class = str(payload.get("error", "Unknown"))
            source = str(payload.get("source", "unknown"))
            ts = str(payload.get("ts", entry.get("timestamp", _now_iso())))
            # Close out any pending tb chase before starting a new event.
            _flush_tb()
            events.append(ErrorEvent(
                ts=ts, exception_class=exc_class, source=source, payload=payload,
            ))
            pending_tb_index = len(events) - 1
            tb_lines = []
            tb_remaining_entries = 20
            continue

        # Traceback collection: once we see the start, gather until we hit a
        # line that obviously doesn't belong (a new EVENT, blank chain, or we
        # exhaust the window).
        if pending_tb_index is not None:
            if TRACEBACK_START_RE.search(msg):
                tb_lines = [msg]
                continue
            # Continuation lines: indented frame, "  File ...", "    ...",
            # or the final "ExceptionClass: ..." line.
            if tb_lines and (
                msg.startswith((" ", "\t"))
                or re.match(r"\w+(\.\w+)*(Error|Exception)\b", msg)
            ):
                tb_lines.append(msg)
                # The final line of a tb is the ExcClass: message. Once we
                # have it, flush.
                if re.match(r"\w+(\.\w+)*(Error|Exception)\b", msg):
                    _flush_tb()
                    continue
            tb_remaining_entries -= 1
            if tb_remaining_entries <= 0:
                _flush_tb()

    _flush_tb()
    return events


# ---- suppression -----------------------------------------------------------


def should_suppress(ev: ErrorEvent) -> tuple[bool, str]:
    """Return (suppress?, reason). reason is empty when not suppressed."""
    if ev.exception_class in SUPPRESSED_EXCEPTIONS:
        return True, f"{ev.exception_class} is in the hard suppression list"
    if ev.source in BACKGROUND_SOURCES:
        return True, f"source={ev.source} is a background tick (operational, not actionable)"
    payload_blob = json.dumps(ev.payload) + " " + ev.traceback
    for exc, needle, why in SUPPRESSED_PATTERNS:
        if ev.exception_class == exc and needle in payload_blob:
            return True, f"{exc} with '{needle}' detail: {why}"
    return False, ""


# ---- grouping --------------------------------------------------------------


def group_by_signature(events: list[ErrorEvent]) -> dict[str, list[ErrorEvent]]:
    groups: dict[str, list[ErrorEvent]] = {}
    for ev in events:
        suppress, reason = should_suppress(ev)
        if suppress:
            log.info("suppressed %s: %s", ev.signature, reason)
            continue
        groups.setdefault(ev.signature, []).append(ev)
    return groups


# ---- issue body generation -------------------------------------------------


_ISSUE_BODY_PROMPT = """Write a GitHub issue body for an auto-detected bug in
the Tootsies Discord bot. Be concise. Use the structure exactly:

## What
One sentence: what's failing.

## Where
The source field + a guess at the responsible cog/module from the field name.

## Proposed fix
If the cause is obvious from the traceback, name it in one or two sentences.
Otherwise write: "Needs investigation; reproduce by triggering {source}."

## Recent occurrences
A short factual line: how many times in the last {window}h.

Do NOT include the raw traceback (the bot will append it as a fenced block
below). Do NOT speculate about user data or paste any user-supplied text.
Plain markdown, no emoji, no em dashes."""


def build_issue_body_via_claude(
    *, exc_class: str, source: str, occurrence_count: int,
    sample_traceback: str, window_hours: int,
) -> str:
    """Call Claude Haiku to write a structured issue body.

    Imported lazily so the script's dry-run + tests don't require the SDK at
    import time.
    """
    from claude_client import HAIKU, ClaudeClient  # local import: avoids SDK at module load

    client = ClaudeClient()

    truncated_tb = sample_traceback[:1000] if sample_traceback else "(no traceback captured)"
    user_message = (
        f"Exception class: {exc_class}\n"
        f"Source field: {source}\n"
        f"Occurrences in window: {occurrence_count}\n"
        f"Window: {window_hours}h\n"
        f"\n"
        f"Sample traceback (truncated to 1000 chars):\n"
        f"```\n{truncated_tb}\n```\n"
    )
    system_extra = _ISSUE_BODY_PROMPT.format(source=source, window=window_hours)

    import asyncio

    async def _go() -> str:
        result = await client._call(
            model=HAIKU,
            user_message=user_message,
            system_extra=system_extra,
            max_tokens=400,
            purpose="log_monitor_issue_body",
        )
        return result.text

    return asyncio.run(_go()).strip()


def append_traceback_block(body: str, sample_traceback: str) -> str:
    """Append the captured traceback to the Claude-written body, fenced."""
    if not sample_traceback:
        return body
    return (
        body
        + "\n\n## Sample traceback (auto-captured)\n```\n"
        + sample_traceback[:4000]
        + "\n```\n"
    )


def issue_title(exc_class: str, source: str) -> str:
    return f"[bug] {source}: {exc_class} (auto-filed by log-monitor)"


# ---- gh CLI wrappers -------------------------------------------------------


def gh_create_issue(
    *, title: str, body: str, labels: list[str], runner: Any = None,
) -> tuple[int, str]:
    """Create an issue via the gh CLI. Returns (issue_number, issue_url)."""
    runner = runner or subprocess.run
    cmd = [
        "gh", "issue", "create",
        "--title", title,
        "--body", body,
        "--label", ",".join(labels),
    ]
    result = runner(cmd, capture_output=True, text=True, check=False, timeout=30)
    if result.returncode != 0:
        raise RuntimeError(
            f"gh issue create failed (exit {result.returncode}): "
            f"{(result.stderr or '')[:500]}"
        )
    # gh prints the issue URL on stdout. Extract the number from the trailing path.
    url = (result.stdout or "").strip().splitlines()[-1].strip()
    m = re.search(r"/issues/(\d+)$", url)
    if not m:
        raise RuntimeError(f"could not parse issue number from gh output: {url!r}")
    return int(m.group(1)), url


def gh_comment_issue(
    *, issue_number: int, body: str, runner: Any = None,
) -> None:
    runner = runner or subprocess.run
    cmd = ["gh", "issue", "comment", str(issue_number), "--body", body]
    result = runner(cmd, capture_output=True, text=True, check=False, timeout=30)
    if result.returncode != 0:
        raise RuntimeError(
            f"gh issue comment failed (exit {result.returncode}): "
            f"{(result.stderr or '')[:500]}"
        )


# ---- main loop -------------------------------------------------------------


def _now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _parse_iso(s: str) -> datetime:
    """Parse an ISO timestamp, tolerant of trailing Z or offset variants."""
    s = s.replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return datetime.now(UTC)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt


def needs_filing(sig_state: SignatureState | None, *, now: datetime | None = None) -> bool:
    """A signature needs a NEW issue if we've never filed one, or the last
    sighting was more than REFILE_AFTER_DAYS ago (likely regression).
    """
    if sig_state is None or sig_state.issue_number is None:
        return True
    last_seen = _parse_iso(sig_state.last_seen_at)
    now = now or datetime.now(UTC)
    return (now - last_seen) > timedelta(days=REFILE_AFTER_DAYS)


def reconcile(
    *, groups: dict[str, list[ErrorEvent]], state: State,
    dry_run: bool, window_hours: int,
    create_issue: Any = None, comment_issue: Any = None,
    build_body: Any = None,
) -> None:
    """Walk grouped events, update state, file/comment as needed.

    `create_issue`, `comment_issue`, `build_body` are injectable for tests.
    """
    create_issue = create_issue or gh_create_issue
    comment_issue = comment_issue or gh_comment_issue
    build_body = build_body or build_issue_body_via_claude
    now = datetime.now(UTC)
    now_iso = now.isoformat(timespec="seconds")

    for sig, evs in sorted(groups.items()):
        exc_class = evs[0].exception_class
        source = evs[0].source
        sample_tb = next((e.traceback for e in evs if e.traceback), "")
        run_count = len(evs)

        existing = state.signatures.get(sig)

        if needs_filing(existing, now=now):
            log.info(
                "filing new issue for signature=%s (run_count=%d, first_time=%s)",
                sig, run_count, existing is None,
            )
            title = issue_title(exc_class, source)
            if dry_run:
                body = (
                    f"[DRY-RUN body for {sig}, would have called Claude]\n"
                    f"occurrences={run_count}, sample_tb={'yes' if sample_tb else 'no'}"
                )
                issue_number = -1
                issue_url = "https://github.com/dry-run/-/issues/-1"
            else:
                claude_body = build_body(
                    exc_class=exc_class, source=source,
                    occurrence_count=run_count, sample_traceback=sample_tb,
                    window_hours=window_hours,
                )
                body = append_traceback_block(claude_body, sample_tb)
                issue_number, issue_url = create_issue(
                    title=title, body=body, labels=["bug", "auto-filed"],
                )
            state.signatures[sig] = SignatureState(
                first_seen_at=(existing.first_seen_at if existing else now_iso),
                last_seen_at=now_iso,
                occurrence_count=(existing.occurrence_count if existing else 0) + run_count,
                issue_number=issue_number,
                issue_url=issue_url,
            )
            continue

        # Existing signature, still alive. Update counters, decide whether to
        # comment about a burst.
        assert existing is not None
        existing.last_seen_at = now_iso
        existing.occurrence_count += run_count
        if run_count > BURST_THRESHOLD and existing.issue_number:
            comment_body = (
                f"log-monitor noticed a burst: **{run_count} occurrences** of "
                f"`{exc_class}` from `{source}` in the last {window_hours}h "
                f"(run at {now_iso}).\n\nTotal seen since first sighting: "
                f"{existing.occurrence_count}."
            )
            log.info("commenting burst on issue #%d (run_count=%d)",
                     existing.issue_number, run_count)
            if not dry_run:
                comment_issue(issue_number=existing.issue_number, body=comment_body)
        else:
            log.info(
                "signature=%s already filed as #%s (run_count=%d, below burst threshold)",
                sig, existing.issue_number, run_count,
            )


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--dry-run", action="store_true",
        help="don't call gh or Claude; print what would be filed/commented.",
    )
    p.add_argument(
        "--fixture", type=Path, default=None,
        help="path to a JSON file of Railway log entries to use instead of calling the CLI.",
    )
    p.add_argument(
        "--service", default="tootsies",
        help="Railway service name (default: tootsies).",
    )
    p.add_argument(
        "--lines", type=int, default=DEFAULT_LOG_LINES,
        help=f"number of log lines to pull (default: {DEFAULT_LOG_LINES}).",
    )
    p.add_argument(
        "--state-path", type=Path, default=STATE_PATH,
        help=f"state file path (default: {STATE_PATH}).",
    )
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    args = parse_args(argv)

    if args.fixture:
        with args.fixture.open("r", encoding="utf-8") as f:
            entries = json.load(f)
    else:
        if not args.dry_run and not os.environ.get("RAILWAY_API_TOKEN"):
            log.error("RAILWAY_API_TOKEN not set; refusing to run without it.")
            return 1
        try:
            entries = fetch_railway_logs(lines=args.lines, service=args.service)
        except (RuntimeError, FileNotFoundError) as exc:
            log.error("failed to fetch railway logs: %s", exc)
            return 1

    events = parse_event_lines(entries)
    log.info("parsed %d error events from %d log entries", len(events), len(entries))

    groups = group_by_signature(events)
    log.info("grouped into %d distinct signatures (after suppression)", len(groups))

    state = State.load(args.state_path)
    try:
        reconcile(
            groups=groups, state=state, dry_run=args.dry_run,
            window_hours=WINDOW_HOURS,
        )
    except Exception:
        log.exception("reconcile failed")
        # Still write state so progress isn't lost. The next run will retry
        # whatever didn't get filed.
        state.last_run_at = _now_iso()
        state.dump(args.state_path)
        return 1

    state.last_run_at = _now_iso()
    state.dump(args.state_path)
    log.info("wrote state to %s", args.state_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
