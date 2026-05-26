"""Light eval: validate the queries used in the market-features announcement.

Runs each announcement example through the market intent classifier and,
if routed, through the actual market fetch to confirm the pipeline finds
relevant results. Does NOT run the full /ask generation (that's eval_ask.py).

This is a "does the plumbing connect" check, not a voice/quality check.

Usage:
  python evals/eval_announcement_examples.py

Required env: ANTHROPIC_API_KEY (Haiku classifier).
Optional env: SPORTS_GAME_ODDS_API_KEY (else SGO examples show "skipped").
"""

from __future__ import annotations

import asyncio
import os
import sys
from dataclasses import dataclass
from pathlib import Path

_repo_root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_repo_root))

try:
    from dotenv import load_dotenv

    for _candidate in (_repo_root.parent / ".env", _repo_root / ".env"):
        if _candidate.exists():
            load_dotenv(_candidate, override=True)
except ImportError:
    pass

from claude_client import ClaudeClient  # noqa: E402
from utils.markets import (  # noqa: E402
    KalshiClient,
    MarketsManager,
    format_markets_for_prompt,
)
from utils.markets import (  # noqa: E402
    close_session as close_markets_session,
)


@dataclass
class Example:
    query: str
    expected_intent: str
    expected_league: str | None = None
    note: str = ""


EXAMPLES: list[Example] = [
    # Sports (May 2026, NBA finals + MLB + NHL playoffs)
    Example(
        "thunder spurs game tonight, who you got",
        "sports", "NBA",
        "NBA finals matchup",
    ),
    Example(
        "any good parlays on tonight's MLB slate",
        "sports", "MLB",
        "MLB regular season, broad slate ask",
    ),
    Example(
        "avalanche golden knights over/under",
        "sports", "NHL",
        "NHL playoffs, specific matchup",
    ),
    # Prediction markets (hits both Poly + Kalshi)
    Example(
        "will beyonce drop a new album this year",
        "prediction_market", None,
        "culture + prediction market, should hit Poly + Kalshi",
    ),
    Example(
        "who wins best picture at the oscars",
        "prediction_market", None,
        "multi-outcome, Poly full field + Kalshi per-film contracts",
    ),
    Example(
        "will GTA 6 come out this year",
        "prediction_market", None,
        "gaming + prediction market, should hit Poly + Kalshi",
    ),
    Example(
        "will kendrick drop another album this year",
        "prediction_market", None,
        "music + prediction market, should hit Poly + Kalshi",
    ),
    Example(
        "who wins the nba championship this year",
        "sports", "NBA",
        "championship futures, SGO lines",
    ),
    Example(
        "what does the smart money think about crypto right now",
        "prediction_market", None,
        "opinion-style, Kalshi crypto returns contracts",
    ),
    Example(
        "what is the market saying about the economy",
        "prediction_market", None,
        "broad opinion, Kalshi state-of-economy contracts",
    ),
    # Control (should NOT trigger markets)
    Example(
        "is beyonce the greatest performer alive",
        "none", None,
        "opinion question, no market intent",
    ),
    Example(
        "best taco spot in oakland",
        "none", None,
        "food rec, zero market signal",
    ),
]


async def main() -> None:
    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("ERROR: ANTHROPIC_API_KEY not set", file=sys.stderr)
        sys.exit(2)

    claude = ClaudeClient()
    kalshi = KalshiClient()
    sgo_key = os.environ.get("SPORTS_GAME_ODDS_API_KEY")
    markets = MarketsManager(
        sgo_key,
        intent_classifier=claude.classify_market_intent,
        kalshi_series_picker=claude.pick_kalshi_series,
        kalshi_market_picker=claude.pick_kalshi_market,
    )

    # Warm Kalshi index for PM queries
    print("warming Kalshi series index...")
    ok = await kalshi.refresh_series_index()
    print(f"  index ok: {ok}, series cached: {len(kalshi.series_index)}")
    markets.kalshi = kalshi

    passed = 0
    failed = 0

    print(f"\n{'=' * 72}")
    print(f"ANNOUNCEMENT EXAMPLE EVAL ({len(EXAMPLES)} queries)")
    print(f"{'=' * 72}")

    for ex in EXAMPLES:
        print(f"\n--- {ex.query}")
        if ex.note:
            print(f"    note: {ex.note}")

        # Step 1: classify intent
        intent_result = await claude.classify_market_intent(ex.query)
        intent = intent_result.get("intent", "none") if intent_result else "none"
        league = intent_result.get("league") if intent_result else None
        search_terms = intent_result.get("search_terms", ex.query) if intent_result else ex.query

        intent_ok = intent == ex.expected_intent
        league_ok = ex.expected_league is None or league == ex.expected_league

        print(f"    intent:  {intent} (expected {ex.expected_intent}) {'OK' if intent_ok else 'FAIL'}")
        if ex.expected_league:
            print(f"    league:  {league} (expected {ex.expected_league}) {'OK' if league_ok else 'FAIL'}")

        # Step 2: if market intent, try the actual fetch
        snapshots = []
        if intent == "sports" and sgo_key:
            snaps = await markets._sports_snapshots(league or "NBA")
            snapshots = snaps or []
        elif intent == "sports" and not sgo_key:
            print("    fetch:   SKIPPED (no SPORTS_GAME_ODDS_API_KEY)")
        elif intent == "prediction_market":
            snaps = await markets._pm_snapshots(search_terms)
            snapshots = snaps or []

        if snapshots:
            print(f"    results: {len(snapshots)} snapshot(s)")
            for s in snapshots[:3]:
                prob = f" yes={s.probability:.0%}" if s.probability is not None else ""
                outcomes = f" outcomes={len(s.outcomes)}" if s.outcomes else ""
                print(f"      [{s.source}] {s.title[:60]}{prob}{outcomes}")
            prompt_block = format_markets_for_prompt(snapshots)
            has_url = "http" in prompt_block
            print(f"    urls:    {'present' if has_url else 'MISSING'}")
        elif intent != "none":
            print("    results: 0 (no snapshots returned)")

        if intent_ok and league_ok:
            passed += 1
        else:
            failed += 1

    await close_markets_session()

    print(f"\n{'=' * 72}")
    print(f"RESULTS: {passed} passed, {failed} failed out of {len(EXAMPLES)}")
    print(f"{'=' * 72}")

    if failed:
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
