"""End-to-end eval of the new Kalshi discovery flow.

Background: Kalshi's API has no free-text search. Previous attempts at
aggregator-based search (PolyRouter, Prediction Hunt) returned either 502s
or constant wrong results. The shipped approach instead does:

  1. Boot: KalshiClient pulls all ~10K series via `/series?include_volume=true`,
     sorts by volume_fp desc, keeps top-N as a {ticker, title} index.
  2. Per query: ClaudeClient.pick_kalshi_series asks Haiku to pick the best
     match from the cached index.
  3. Fetch: KalshiClient.get_events_for_series hits
     `/events?series_ticker=X&with_nested_markets=true` for real markets.

This script exercises that exact chain against live APIs with the same 10
queries we used for the aggregator evals so the outputs are directly
comparable. Pass = topically relevant Kalshi markets for each query; the
previous broken state returned KXMVE exotic combo markets regardless.

Required env: ANTHROPIC_API_KEY (Haiku). Kalshi reads are public, no key.
Usage: python evals/eval_kalshi_discovery.py

Cost: ~10 Haiku calls + 11 Kalshi reads. Sub-cent total.
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

# Same dotenv shim as the other eval scripts.
_repo_root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_repo_root))

try:
    from dotenv import load_dotenv

    # Load parent first, then worktree so worktree-specific keys override.
    # The previous "break on first match" pattern dropped keys held only in
    # the parent (e.g. ANTHROPIC_API_KEY) once we put a worktree .env in place.
    for _candidate in (_repo_root.parent / ".env", _repo_root / ".env"):
        if _candidate.exists():
            load_dotenv(_candidate, override=True)
except ImportError:
    pass

from claude_client import ClaudeClient  # noqa: E402
from utils.markets import KalshiClient  # noqa: E402

# Queries chosen for May 2026 topical relevance + probable open events on
# Kalshi today. Mix of evergreen (crypto, Fed) and time-sensitive (NBA
# finals window, next Fed meeting, current culture moments). Avoids dead
# queries from the previous batch like "trump 2028" / "2028 presidential
# election" where the canonical series (PRES) is dormant until Kalshi
# opens the 2028 cycle.
QUERIES = [
    # Sports — NBA finals window
    "nba finals winner",
    "thunder vs pacers tonight",
    # Crypto — always active
    "bitcoin price end of month",
    "bitcoin 150k by end of year",
    # Fed — next decision cycle
    "fed rate cut june",
    # Tech / AI
    "top ai model end of year",
    "openai for profit conversion",
    # Culture / entertainment
    "taylor swift travis kelce wedding",
    "next oscars best picture",
    # Geopolitics
    "khamenei out",
]


async def main() -> None:
    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("ERROR: ANTHROPIC_API_KEY not set", file=sys.stderr)
        sys.exit(2)

    kalshi = KalshiClient()
    claude = ClaudeClient()

    print("=" * 78)
    print("STEP 1: refresh Kalshi series index (live /series fetch)")
    print("=" * 78)
    ok = await kalshi.refresh_series_index()
    print(f"refresh ok: {ok}, top-N kept: {len(kalshi.series_index)}")
    if kalshi.series_index:
        print("first 10 by volume:")
        for s in kalshi.series_index[:10]:
            print(f"  {s['ticker']:<25} {s['title'][:50]}")
    if not kalshi.series_index:
        print("ERROR: series_index empty after refresh, aborting")
        sys.exit(1)

    print()
    print("=" * 78)
    print("STEP 2: per-query Haiku pick + Kalshi events fetch")
    print("=" * 78)
    for q in QUERIES:
        chosen = await claude.pick_kalshi_series(q, kalshi.series_index)
        print(f"\n[{q}]  -> Haiku picked: {chosen or 'NONE'}")
        if not chosen:
            continue
        snaps = await kalshi.get_events_for_series(chosen, limit=3)
        if not snaps:
            print("  (no open events found under this series)")
            continue
        for snap in snaps[:3]:
            prob = f"{snap.probability:.0%}" if snap.probability is not None else "?"
            print(f"  - {snap.title[:70]} | yes={prob}")
            if snap.url:
                print(f"    {snap.url}")


if __name__ == "__main__":
    asyncio.run(main())
