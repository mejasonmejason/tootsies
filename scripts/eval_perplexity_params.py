"""Eval: does setting Sonar search-control params kill the 'can't verify' hedging?

Background (docs/perplexity-handoff.md): utils/perplexity.py used to call Sonar
with only model+messages, inheriting search_context_size="low" (shallow
retrieval) and no recency window. Symptom: low-signal evergreen filler and
responses that open with "I can't verify live trends / results are mostly
YouTube mixes". The fix sets per-surface params; this harness measures the
before/after so the bump is justified, not vibes.

For each surface it runs the SAME query across the grid:
  context_size in {low, medium, high}  x  recency in {off, per-surface default}
and, because hedging is intermittent (handoff finding), repeats each cell
REPEATS times and reports a real fraction: hedging rate, avg source count,
avg latency.

Not a pytest test (makes live API calls). Needs PERPLEXITY_API_KEY (pull from
Railway per CLAUDE.md "Debugging Railway deploys"). Run:
  PERPLEXITY_API_KEY=... python scripts/eval_perplexity_params.py
"""
from __future__ import annotations

import asyncio
import os
import time

from utils.perplexity import _SEARCH_CONFIG, PerplexityClient, build_search_query

# How many times to sample each grid cell. Hedging is intermittent, so a single
# sample is a coin flip, not a rate. 3 keeps the live API spend bounded while
# still distinguishing "always hedges" from "flaky".
REPEATS = 3

# One representative query per surface, built the same way production does.
PROBES: list[dict[str, str]] = [
    {"surface": "discourse", "category": "rnb", "user_input": ""},
    {"surface": "discourse", "category": "nba", "user_input": ""},
    {"surface": "music", "category": "afrobeats", "user_input": ""},
    {"surface": "ask", "category": "", "user_input": "how many #1 hot 100s does drake have"},
    {"surface": "recap", "category": "", "user_input": "lebron trade rumors"},
    {"surface": "chimein", "category": "", "user_input": "kendrick dropped a new song"},
]

CONTEXT_SIZES = ["low", "medium", "high"]

# Phrases that signal Sonar punted instead of retrieving. Lowercased substring
# match against the response body (the SOURCES block is excluded so a real URL
# containing one of these words can't false-positive).
HEDGE_MARKERS = (
    "can't verify",
    "cannot verify",
    "couldn't find",
    "could not find",
    "do not have",
    "don't have access",
    "i'm not able to",
    "unable to verify",
    "no verifiable",
    "mostly youtube mixes",
    "playlist pages",
)


def _purpose_for(probe: dict[str, str]) -> str:
    # music rides discourse-shaped query text but its own search params.
    return "music" if probe["surface"] == "music" and probe["category"] else probe["surface"]


def _is_hedged(text: str) -> bool:
    body = text.split("SOURCES:")[0].lower()
    return any(m in body for m in HEDGE_MARKERS)


def _source_count(text: str) -> int:
    # Mirrors the SOURCES block emitted by PerplexityClient.search (one
    # "\n  [N] url" line per citation). If that format changes, update both.
    if "SOURCES:" not in text:
        return 0
    return text.split("SOURCES:")[1].count("\n  [")


async def main() -> None:
    key = os.getenv("PERPLEXITY_API_KEY")
    if not key:
        print("PERPLEXITY_API_KEY unset. Pull it from Railway (see CLAUDE.md).")
        return
    client = PerplexityClient(key)
    try:
        for probe in PROBES:
            purpose = _purpose_for(probe)
            default_recency = _SEARCH_CONFIG.get(purpose, {}).get("recency")
            query = build_search_query(
                probe["user_input"],
                surface=probe["surface"],
                category=probe["category"] or None,
            )
            print(f"\n{'=' * 72}")
            print(f"surface={probe['surface']} purpose={purpose} "
                  f"default_recency={default_recency or 'off'} (n={REPEATS}/cell)")
            print(f"query: {query[:90]}...")
            print(f"{'=' * 72}")
            print(f"{'context':>8} {'recency':>8} {'hedge_rate':>11} {'avg_src':>8} {'avg_ms':>7}")
            for ctx in CONTEXT_SIZES:
                recency_opts = [None]
                if default_recency:
                    recency_opts.append(default_recency)
                for rec in recency_opts:
                    hedged = 0
                    sources: list[int] = []
                    latencies: list[int] = []
                    errors = 0
                    for _ in range(REPEATS):
                        t0 = time.monotonic()
                        text = await client.search(
                            query, purpose=purpose,
                            search_context_size=ctx, recency=rec,
                        )
                        latencies.append(int((time.monotonic() - t0) * 1000))
                        if text is None:
                            errors += 1
                            continue
                        if _is_hedged(text):
                            hedged += 1
                        sources.append(_source_count(text))
                    ok = REPEATS - errors
                    rate = f"{hedged}/{ok}" if ok else "ERR"
                    avg_src = f"{sum(sources) / len(sources):.1f}" if sources else "-"
                    avg_ms = sum(latencies) // len(latencies) if latencies else 0
                    print(f"{ctx:>8} {(rec or 'off'):>8} {rate:>11} {avg_src:>8} {avg_ms:>7}")
    finally:
        await client.close()


if __name__ == "__main__":
    asyncio.run(main())
