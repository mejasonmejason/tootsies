# Evals

One-off scripts for evaluating bot behavior against real APIs. These hit live
services (Anthropic, Polymarket, Kalshi, SGO, Perplexity) so each run costs a
few cents. They're not part of CI and aren't auto-discovered by `pytest`.

## What's here

| Script | Purpose | Cost per run |
|---|---|---|
| [`eval_ask.py`](eval_ask.py) | Run prompts through the real `cogs/ask.py` pipeline and check responses against length / banned-word / tool-narration / URL-citation rules. | ~$0.10 for 24 prompts |

## Run

```bash
# all eval_ask defaults
python evals/eval_ask.py

# quick smoke test (5 prompts only)
python evals/eval_ask.py --quick

# filter by prompt tag
python evals/eval_ask.py --tag sports
python evals/eval_ask.py --tags multi,url

# one-off prompt
python evals/eval_ask.py "any nba parlays tonight"

# skip Perplexity even if PERPLEXITY_API_KEY is set
python evals/eval_ask.py --quick --no-perplexity
```

## Env

Auto-loaded from this worktree's `.env` OR the parent worktree's `.env` (in
that order, with `override=True` so an empty inherited shell var doesn't
shadow the real value).

```bash
ANTHROPIC_API_KEY=...         # required
SPORTS_GAME_ODDS_API_KEY=...  # optional; SGO routes skipped if unset
PERPLEXITY_API_KEY=...        # optional; Perplexity skipped if unset
```

## Safety: evals must not mutate state

**Rule: always call the inner compose method on a cog, never the cog's slash-
command or `on_message` wrapper.** The outer wrappers do DB writes (rate-limit
consumption, discourse history, chime-in history). The inner compose methods
are pure compute + HTTP — safe to call repeatedly without affecting limits.

| Surface | Outer (DB-mutating, AVOID) | Inner (safe to call from evals) |
|---|---|---|
| `/ask` + mention | `Ask.ask()` / `Ask.on_message` | `Ask._answer()` |
| `/discourse` manual | `Discourse._handle_manual_post` | `Discourse._compose()` |
| `/discourse` scheduled | scheduler tick | `Discourse._compose()` |
| `/chimein` | `Chimein` tick | `ClaudeClient.chimein_score` + `chimein_post` |

`eval_ask.py` follows this rule: it instantiates the `Ask` cog with a stub
bot that has no `.db` attribute and calls `_answer` directly. Any future eval
that touches `_handle_manual_post`, `consume_user`, `add_discourse`, etc.
would mutate production state and is a bug.

## When to add a new eval

- Validating a new external API's signal quality BEFORE building on it
  (e.g. eval_poly_comments was the reason we removed comments from v3)
- Smoke-testing a new prompt path end-to-end with real models
- Regression checks before risky merges to the prompt/voice surface
