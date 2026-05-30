#!/usr/bin/env python3
"""Twice-daily ops monitor for the bot.

Pulls the bot's structured EVENT logs from Railway, computes health metrics over
the recent window, and emits a Markdown report of regressions (threshold
crossings) with sample outputs. A scheduled GitHub Actions workflow
(.github/workflows/ops-monitor.yml) runs this, then hands the report to Claude
Code, which judges the flagged samples and files deduped `auto-eval` issues.

This one routine owns BOTH halves of the bot's QA:
  - command quality: per-purpose latency over ceilings, hallucinated-link rate,
    ungrounded chime-ins, failed slash commands, rate-limit pressure.
  - error triage: error signatures grouped by (source, exception_class), split
    by the `recoverable` tag from emit_error, with the inline traceback +
    context attached and bursts escalated. (This replaces the standalone
    Railway log-monitor routine: one interface, one cadence.)

This is the DETERMINISTIC half of the hybrid: it does the reliable counting and
flagging; the LLM does the fuzzy "is this take actually hollow/wrong" judgment.

Run locally (needs RAILWAY_API_TOKEN + RAILWAY_SERVICE_ID in env):

    python scripts/ops_monitor.py            # print report to stdout
    python scripts/ops_monitor.py --out r.md # also write to a file

The pure functions (parse/aggregate/evaluate/render) take plain event dicts and
are unit-tested; the Railway I/O is integration-only (pragma: no cover).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.request
from dataclasses import dataclass, field

RAILWAY_GQL = "https://backboard.railway.com/graphql/v2"

# Average-latency ceilings per claude_api purpose (ms). A purpose whose mean
# duration exceeds its ceiling gets flagged. Only listed purposes are checked,
# unknown purposes are ignored to avoid noise. Tune as the bot evolves.
LATENCY_CEILINGS_MS: dict[str, int] = {
    "ask": 12_000,
    "recap": 12_000,
    "deflect": 8_000,
    "discourse_manual": 30_000,
    "discourse_scheduled": 30_000,
    "chimein_post": 20_000,
    "chimein_post_forced": 20_000,
    "music_post": 45_000,
    "memory_hourly": 30_000,
    "memory_daily": 45_000,
    "memory_weekly": 45_000,
    "discourse_score": 5_000,
    "chimein_score": 5_000,
    "market_intent": 5_000,
    "classify_abuse": 5_000,
}

# How many hallucinated-link strips (per surface) in the window before we flag.
HALLUCINATED_LINK_MIN = 3
# Fraction of chime-in posts that ran ZERO searches before we flag "ungrounded".
UNGROUNDED_CHIMEIN_RATE = 0.5
# Min chime-in posts before the ungrounded-rate check is meaningful.
UNGROUNDED_CHIMEIN_MIN_POSTS = 4
# Max sample outputs to attach to any one finding.
MAX_SAMPLES = 3
# Occurrences of one error signature in the window before we call it a "burst"
# (bumps an otherwise-recoverable signature up a severity notch).
BURST_ERROR_MIN = 10


@dataclass
class PurposeStats:
    n: int = 0
    in_tokens: int = 0
    out_tokens: int = 0
    ms: list[int] = field(default_factory=list)
    zero_tool_calls: int = 0
    samples: list[str] = field(default_factory=list)

    @property
    def avg_ms(self) -> int:
        return sum(self.ms) // len(self.ms) if self.ms else 0


@dataclass
class ErrorStats:
    """One error signature, keyed on (source, exception_class).

    Pulls the structure out of `emit_error` (utils/events.py) so the report can
    triage urgency the same way the bot tags it: `recoverable=True` means the
    bot caught + recovered (retry/skip, no user-visible failure), so those are
    informational; non-recoverable ones caused a deflection / undelivered
    response and are the ones that actually bite.
    """
    n: int = 0
    recoverable: int = 0
    non_recoverable: int = 0
    sample_traceback: str = ""
    sample_context: str = ""


@dataclass
class Aggregate:
    purposes: dict[str, PurposeStats] = field(default_factory=dict)
    errors: dict[tuple[str, str], ErrorStats] = field(default_factory=dict)
    command_failures: list[dict] = field(default_factory=list)
    hallucinated_links: dict[str, int] = field(default_factory=dict)
    hallucinated_samples: dict[str, list[str]] = field(default_factory=dict)
    rate_limit_hits: dict[tuple[str, str], int] = field(default_factory=dict)
    total_events: int = 0


@dataclass
class Finding:
    command: str
    kind: str
    severity: str  # "high" | "medium" | "low"
    detail: str
    samples: list[str] = field(default_factory=list)


def parse_event_lines(lines: list[str]) -> list[dict]:
    """Extract the JSON payload from each `... EVENT {json}` log line."""
    out: list[dict] = []
    for line in lines:
        marker = line.find("EVENT ")
        if marker == -1:
            continue
        try:
            out.append(json.loads(line[marker + len("EVENT ") :]))
        except (ValueError, TypeError):
            continue
    return out


def aggregate(events: list[dict]) -> Aggregate:
    """Roll a flat list of event dicts into per-command health metrics.

    Events are deduped on (event, ts) so overlapping deployment-log pulls don't
    double-count.
    """
    agg = Aggregate()
    seen: set[tuple] = set()
    for ev in events:
        kind = ev.get("event")
        key = (kind, ev.get("ts"))
        if ev.get("ts") is not None and key in seen:
            continue
        seen.add(key)
        agg.total_events += 1

        if kind == "claude_api" and ev.get("ok"):
            purpose = str(ev.get("purpose", "unknown"))
            stats = agg.purposes.setdefault(purpose, PurposeStats())
            stats.n += 1
            stats.in_tokens += int(ev.get("input_tokens") or 0)
            stats.out_tokens += int(ev.get("output_tokens") or 0)
            if ev.get("duration_ms") is not None:
                stats.ms.append(int(ev["duration_ms"]))
            if ev.get("had_tools_available") and not ev.get("tool_call_count"):
                stats.zero_tool_calls += 1
            preview = ev.get("response_preview")
            if preview and len(stats.samples) < MAX_SAMPLES:
                stats.samples.append(str(preview))
        elif kind == "error":
            ekey = (str(ev.get("source", "?")), str(ev.get("error", "?")))
            est = agg.errors.setdefault(ekey, ErrorStats())
            est.n += 1
            if ev.get("recoverable"):
                est.recoverable += 1
            else:
                est.non_recoverable += 1
            # Keep the first traceback/context we see for this signature, so the
            # judge can write an actionable bug without re-running the call.
            if not est.sample_traceback and ev.get("traceback"):
                tb = ev["traceback"]
                est.sample_traceback = tb[-1] if isinstance(tb, list) and tb else str(tb)
            if not est.sample_context and ev.get("context"):
                est.sample_context = str(ev["context"])
        elif kind == "command" and ev.get("ok") is False:
            agg.command_failures.append(ev)
        elif kind == "link_stripped" and ev.get("reason") == "hallucinated":
            surface = str(ev.get("purpose", "?"))
            agg.hallucinated_links[surface] = (
                agg.hallucinated_links.get(surface, 0) + int(ev.get("count") or 1)
            )
            urls = ev.get("urls") or []
            bucket = agg.hallucinated_samples.setdefault(surface, [])
            for u in urls:
                if len(bucket) < MAX_SAMPLES:
                    bucket.append(str(u))
        elif kind == "rate_limit_hit":
            rkey = (str(ev.get("command", "?")), str(ev.get("scope", "?")))
            agg.rate_limit_hits[rkey] = agg.rate_limit_hits.get(rkey, 0) + 1
    return agg


def evaluate(agg: Aggregate) -> list[Finding]:
    """Turn aggregated metrics into a list of issue-worthy findings."""
    findings: list[Finding] = []

    # 1. Error signatures, grouped by (source, exception). Non-recoverable
    #    errors (user-visible failures) are high; all-recoverable signatures are
    #    informational (low) unless they burst, which bumps them to medium. This
    #    deprioritizes background-tick noise without a hardcoded suppression list
    #    (the bot already tags those `recoverable=True`).
    for (source, error), est in sorted(agg.errors.items(), key=lambda x: -x[1].n):
        if est.non_recoverable:
            severity = "high"
        elif est.n >= BURST_ERROR_MIN:
            severity = "medium"
        else:
            severity = "low"
        detail = (
            f"{est.n}x `{error}` from `{source}` "
            f"({est.non_recoverable} non-recoverable, {est.recoverable} recovered)."
        )
        if est.sample_context:
            detail += f" context: {est.sample_context[:200]}"
        samples = [est.sample_traceback] if est.sample_traceback else []
        findings.append(Finding(
            command=source, kind="error", severity=severity,
            detail=detail, samples=samples,
        ))

    # 2. Slash-command invocations that returned ok=false.
    if agg.command_failures:
        by_cmd: dict[str, int] = {}
        for ev in agg.command_failures:
            c = str(ev.get("cmd", "?"))
            by_cmd[c] = by_cmd.get(c, 0) + 1
        for cmd, n in sorted(by_cmd.items(), key=lambda x: -x[1]):
            findings.append(Finding(
                command=cmd, kind="command_failure", severity="high",
                detail=f"{n} failed `/{cmd}` invocation(s) (command event ok=false).",
            ))

    # 3. Latency regressions per purpose.
    for purpose, ceiling in LATENCY_CEILINGS_MS.items():
        stats = agg.purposes.get(purpose)
        if stats and stats.avg_ms > ceiling:
            findings.append(Finding(
                command=purpose, kind="latency", severity="medium",
                detail=(
                    f"`{purpose}` averaged {stats.avg_ms}ms over {stats.n} call(s), "
                    f"ceiling {ceiling}ms."
                ),
                samples=stats.samples,
            ))

    # 4. Hallucinated links the URL guardrail had to strip.
    for surface, n in sorted(agg.hallucinated_links.items(), key=lambda x: -x[1]):
        if n >= HALLUCINATED_LINK_MIN:
            findings.append(Finding(
                command=surface, kind="hallucinated_links", severity="medium",
                detail=(
                    f"{n} hallucinated link(s) stripped from `{surface}` output. "
                    "The model is inventing URLs."
                ),
                samples=agg.hallucinated_samples.get(surface, []),
            ))

    # 5. Ungrounded chime-ins (posted without ever running a search).
    cp = agg.purposes.get("chimein_post")
    if cp and cp.n >= UNGROUNDED_CHIMEIN_MIN_POSTS:
        rate = cp.zero_tool_calls / cp.n
        if rate >= UNGROUNDED_CHIMEIN_RATE:
            findings.append(Finding(
                command="chimein_post", kind="ungrounded", severity="medium",
                detail=(
                    f"{cp.zero_tool_calls}/{cp.n} chime-ins ({rate:.0%}) ran ZERO "
                    "searches on the thinking-on path, asserting from stale memory. "
                    "(The forced-search fallback should be rescuing these, confirm "
                    "`chimein_post_forced` calls are present.)"
                ),
                samples=cp.samples,
            ))

    # 6. Rate-limit pressure.
    for (cmd, scope), n in sorted(agg.rate_limit_hits.items(), key=lambda x: -x[1]):
        findings.append(Finding(
            command=cmd, kind="rate_limit", severity="low",
            detail=f"{n} {scope} rate-limit hit(s) on `{cmd}`.",
        ))

    return findings


def render(agg: Aggregate, findings: list[Finding], window: str = "") -> str:
    """Render the report as Markdown for Claude Code to act on."""
    lines: list[str] = []
    lines.append("# Ops monitor report")
    if window:
        lines.append(f"_Window: {window}_")
    lines.append(f"_{agg.total_events} events analyzed._")
    lines.append("")

    if not findings:
        lines.append("**All clear.** No regressions crossed a threshold this run.")
    else:
        order = {"high": 0, "medium": 1, "low": 2}
        lines.append(f"## {len(findings)} finding(s)")
        lines.append("")
        for f in sorted(findings, key=lambda x: order.get(x.severity, 9)):
            lines.append(f"### [{f.severity.upper()}] {f.command} ({f.kind})")
            lines.append(f.detail)
            for s in f.samples[:MAX_SAMPLES]:
                lines.append(f"  - sample: `{s[:200]}`")
            lines.append("")

    # Always include a compact metrics table for context.
    lines.append("## Per-purpose metrics")
    lines.append("| purpose | n | avg_ms | avg_in | avg_out |")
    lines.append("|---|--:|--:|--:|--:|")
    for p, st in sorted(agg.purposes.items(), key=lambda x: -x[1].n):
        avg_in = st.in_tokens // st.n if st.n else 0
        avg_out = st.out_tokens // st.n if st.n else 0
        lines.append(f"| {p} | {st.n} | {st.avg_ms} | {avg_in} | {avg_out} |")
    return "\n".join(lines)


# ---- Railway I/O (integration-only) -----------------------------------------


def _gql(token: str, query: str, variables: dict) -> dict:  # pragma: no cover
    req = urllib.request.Request(
        RAILWAY_GQL,
        data=json.dumps({"query": query, "variables": variables}).encode(),
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.load(resp)


def _project_env(token: str, service_id: str) -> tuple[str, str]:  # pragma: no cover
    q = (
        "query($id: String!) { service(id: $id) { projectId "
        "project { environments { edges { node { id name } } } } } }"
    )
    d = _gql(token, q, {"id": service_id})
    svc = d["data"]["service"]
    project_id = svc["projectId"]
    envs = svc["project"]["environments"]["edges"]
    env_id = next(
        (e["node"]["id"] for e in envs if e["node"]["name"] == "production"),
        envs[0]["node"]["id"],
    )
    return project_id, env_id


def _recent_deployment_ids(
    token: str, project_id: str, env_id: str, service_id: str, n: int = 2,
) -> list[str]:  # pragma: no cover
    q = (
        "query($p: String!, $e: String!, $s: String!) { deployments(first: 5, "
        "input: { projectId: $p, environmentId: $e, serviceId: $s }) "
        "{ edges { node { id status } } } }"
    )
    d = _gql(token, q, {"p": project_id, "e": env_id, "s": service_id})
    edges = d["data"]["deployments"]["edges"]
    return [e["node"]["id"] for e in edges[:n]]


def _deployment_log_lines(token: str, deployment_id: str) -> list[str]:  # pragma: no cover
    q = (
        "query($id: String!) { deploymentLogs(deploymentId: $id, limit: 5000) "
        "{ message } }"
    )
    d = _gql(token, q, {"id": deployment_id})
    return [m["message"] for m in (d["data"].get("deploymentLogs") or [])]


def fetch_events() -> list[dict]:  # pragma: no cover
    token = os.environ.get("RAILWAY_API_TOKEN", "")
    service_id = os.environ.get("RAILWAY_SERVICE_ID", "")
    if not token or not service_id:
        raise RuntimeError("RAILWAY_API_TOKEN / RAILWAY_SERVICE_ID not set")
    project_id, env_id = _project_env(token, service_id)
    lines: list[str] = []
    for dep in _recent_deployment_ids(token, project_id, env_id, service_id):
        lines.extend(_deployment_log_lines(token, dep))
    return parse_event_lines(lines)


def main(argv: list[str] | None = None) -> int:  # pragma: no cover
    parser = argparse.ArgumentParser(description="Evaluate bot commands from Railway logs.")
    parser.add_argument("--out", help="also write the report to this file")
    args = parser.parse_args(argv)

    if not os.environ.get("RAILWAY_API_TOKEN") or not os.environ.get("RAILWAY_SERVICE_ID"):
        report = (
            "# Ops monitor report\n\n"
            "**SETUP INCOMPLETE.** `RAILWAY_API_TOKEN` / `RAILWAY_SERVICE_ID` are not "
            "available to this run, so no production logs could be pulled. Add them as "
            "GitHub Actions secrets so the eval can read Railway logs. File ONE issue "
            "for this (labeled `auto-eval`) if one isn't already open, then stop."
        )
    else:
        try:
            events = fetch_events()
            agg = aggregate(events)
            findings = evaluate(agg)
            report = render(agg, findings)
        except Exception as exc:
            report = (
                "# Ops monitor report\n\n"
                f"**EVAL FAILED.** Could not complete the evaluation: `{exc}`. "
                "File ONE issue (labeled `auto-eval`) if one isn't already open."
            )

    print(report)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            fh.write(report)
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
