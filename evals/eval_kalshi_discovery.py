"""End-to-end eval of the Kalshi two-stage discovery flow.

Background: Kalshi's API has no free-text search. The shipped approach:

  1. Boot: KalshiClient.refresh_series_index pulls /series?include_volume=true,
     filters by open-events, keeps top-N as {ticker, title} index.
  2. Stage 1 (per query): ClaudeClient.pick_kalshi_series asks Haiku to pick
     the best matching series from the cached index.
  3. Live fetch: KalshiClient.get_events_for_series hits /events with the
     picked series_ticker, returns nested markets as MarketSnapshots.
  4. Stage 2 (per query): ClaudeClient.pick_kalshi_market asks Haiku to
     narrow within the series's markets to a specific one (or NONE for
     broad queries that should show the whole series).

This script exercises the full two-stage chain against live APIs.

Required env: ANTHROPIC_API_KEY (Haiku). Kalshi reads are public, no key.
Usage: python evals/eval_kalshi_discovery.py

Cost: 10-20 Haiku calls (depending on stage-2 invocations) + 11 Kalshi reads.
Sub-cent total.
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
    print("STEP 2: per-query two-stage Haiku pick + Kalshi events fetch")
    print("=" * 78)
    for q in QUERIES:
        # Stage 1: pick the series from the cached index.
        chosen_series = await claude.pick_kalshi_series(q, kalshi.series_index)
        print(f"\n[{q}]")
        print(f"  stage 1 series  -> {chosen_series or 'NONE'}")
        if not chosen_series:
            continue
        # Live fetch of the picked series's open events with markets.
        series_snaps = await kalshi.get_events_for_series(
            chosen_series, limit=4,
        )
        if not series_snaps:
            print("  (no open markets under this series)")
            continue
        # Stage 2: narrow within the series's markets.
        market_candidates = [
            {"ticker": str(s.meta.get("ticker", "")), "title": s.title}
            for s in series_snaps if s.meta.get("ticker")
        ]
        chosen_market = None
        if len(market_candidates) > 1:
            chosen_market = await claude.pick_kalshi_market(q, market_candidates)
        print(
            f"  stage 2 market  -> "
            f"{chosen_market or ('NONE (show whole series)' if len(market_candidates) > 1 else 'skipped (1 market)')}"
        )
        # Show the narrowed result.
        if chosen_market:
            final = [s for s in series_snaps if s.meta.get("ticker") == chosen_market]
        else:
            final = series_snaps[:3]
        for snap in final:
            prob = f"{snap.probability:.0%}" if snap.probability is not None else "?"
            print(f"  - {snap.title[:70]} | yes={prob}")
            if snap.url:
                print(f"    {snap.url}")


if __name__ == "__main__":
    asyncio.run(main())
