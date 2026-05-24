# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

Tootsies is a Discord bot ("Toots"), a hip city-girl bartender persona. Mods can ship new features by typing `/order <feature>` in Discord; Claude Code Action writes the PR, CI runs, Railway redeploys. The bot is live on Railway and auto-deploys on push to `main`.

## Commands

```bash
# Setup
python3.11 -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt
cp .env.example .env  # fill in tokens

# Run
python bot.py  # needs Postgres via DATABASE_URL

# Checks (all three must pass for CI)
ruff check .
mypy .
pytest

# Run a single test
pytest tests/test_preflight.py::test_preflight_allow -v
```

## Architecture

**Entrypoint:** `bot.py`, boots Discord client, opens DB pool, exposes `/health`, loads cogs, syncs slash commands per guild on every startup.

**Claude API layer:** `claude_client.py` wraps the Anthropic SDK. Model routing: Haiku for `/ask`, `/recap`, deflections (fast/cheap); Sonnet for `/discourse` and `/order` pre-flight (needs judgment). System prompt is cached via `cache_control: ephemeral`. Every API call gets the full constitution + persona prepended (~120 tokens).

**Persona:** `persona.py` composes the system prompt from `constitution.py` (hard rules, house rules, calibration) + persona core + voice examples. `constitution.py` is non-negotiable and cannot be loosened by `/order`.

**Database:** `db.py`, raw `asyncpg` with inline SQL, no ORM. Schema is idempotent `CREATE TABLE IF NOT EXISTS` statements that run on every startup. Add new tables here; never drop columns without a migration plan.

**Models:** `models.py`, plain dataclasses for DB rows and StrEnums for `OrderStatus` and `MoodMode`. No ORM behavior.

**Cogs** (in `cogs/`):
- `ask.py`, `/ask` + `@Toots` mention handler. Mentions and `/ask` share a rate-limit counter. Fail-open on DB errors (better to answer than go silent).
- `recap.py`, `/recap period:[1h|today]`
- `discourse.py`, `/discourse category:` (manual posts) + `/discourse mood:` (schedule control) + the mood scheduler background task
- `order.py`, `/order new|status|retry|cancel`. Pre-flight sanity check, one-at-a-time enforcement, pipeline-red blocking. Mod-only via `_mod_gate`.
- `admin.py`, `/close`, `/open`, `/undo`
- `settings.py`, `/menu` interactive wizard

**Utils** (in `utils/`):
- `rate_limits.py`, per-user daily limits (`/ask`, `/recap`) and server-wide daily limits (`/discourse`, `/order`) + cooldowns
- `permissions.py`, `is_mod()` checks against `mod_roles` table
- `gates.py`, `require_configured()` guard for pre-`/menu` state
- `feeds.py`, channel history fetching for context
- `voice.py`, canned quip pools (rate limit, permission denied, pipeline red, etc.) with `pick()` for random selection
- `bot_logs.py`, structured logging to the guild's `#bot-logs` channel
- `github.py`, `GitHubClient` for filing issues/PRs via the GitHub API
- `railway.py`, Railway API for `/undo` rollbacks
- `healthcheck.py`, aiohttp server at `/health`

## Protected paths

The `/order` pre-flight (in `claude_client.py:preflight_order`) rejects orders that would touch:
- `constitution.py`, `persona.py` core voice, `.github/`, `Dockerfile`, `railway.toml`, `Procfile`, `db.py` connection setup, `bot.py` boot logic, `requirements.txt` deletions

Exceptions exist (e.g., adding new cogs, new tables, new deps, voice library additions in `utils/voice.py` are all allowed).

## Testing

Tests use `conftest.py` to stub env vars so imports don't blow up without real secrets. No live DB or API calls in tests, patch `_call` on `ClaudeClient` for API tests. `pytest-asyncio` with `asyncio_mode = "auto"`.

## Key conventions

- Python 3.11+. Ruff for linting (line length 100, E501 ignored). Mypy with `ignore_missing_imports = true` and `check_untyped_defs = true`.
- All user-facing text goes through the Toots voice, lowercase, short, no emoji unless the user used one first. Plumbing (PR titles, env vars, logs) stays plain.
- Rate limits: per-user daily (default 20) for `/ask`+mentions and `/recap`; server-wide daily (default 20) for `/discourse` and `/order`. `/order` also has a 15min per-user cooldown.
- Order states flow: Prepping -> On the stove -> Plating -> Served (or Burnt/Sent back at any step).
- Config is a frozen dataclass in `config.py`, read from env vars at startup. Required: `DISCORD_TOKEN`, `ANTHROPIC_API_KEY`, `GITHUB_TOKEN`, `DATABASE_URL`.

## Structured events for dashboards

Every metric-worthy thing emits a JSON event line via `utils.events.emit(kind, **fields)`.
Each line is prefixed with the literal `EVENT ` so Railway log queries can isolate
dashboard data from operational logs.

Existing event kinds (keep [utils/events.py](utils/events.py) docstring in sync):

| event | source | fields |
|---|---|---|
| `command` | utils/metrics.py (`@track_command`) | cmd, user_id, guild_id, duration_ms, ok, error |
| `claude_api` | claude_client.py (`_call`) | model, purpose, input_tokens, output_tokens, duration_ms, stop_reason, ok |
| `order_state` | cogs/order.py | order_id, issue_number, guild_id, user_id, from, to |
| `rate_limit_hit` | utils/rate_limits.py | scope, command, user_id, guild_id, count, cap |
| `deploy_event` | bot.py | kind (boot/shutdown), guilds |
| `error` | cogs/* + bot.py error handler | source (e.g. `ask`, `order_preflight`, `undo`), error (exception class), guild_id, user_id, optional context |
| `recap_deflected` | cogs/recap.py | guild_id, user_id, period, channel_id, channel_name, reason (`no_permission`/`no_messages`), can_read_history, total_messages |
| `discourse_fallback` | cogs/discourse.py | guild_id, user_id, category, source_count, recent_topic_count, reason |

**Adding a new event:** call `emit("your_kind", key1=..., key2=...)` and add a row to
the table above + the events.py docstring. Use snake_case for kinds and fields. Don't
include full message content (data minimization, per the constitution).

**Railway dashboard queries:** filter logs for the `EVENT ` prefix, then parse the JSON
suffix. Typical queries: count of `event=command` per minute, p95 of `duration_ms`
where `event=claude_api`, sum of `output_tokens` where `purpose=ask` for cost tracking.

## Commit and PR conventions

**Always include a `PREVIEW:` section in commit bodies and PR descriptions when the change is user-facing.** The bot's UI is Discord, so screenshots are awkward, render an ASCII/markdown mock of the relevant surface instead. Reviewers shouldn't have to deploy the change to know what it looks like.

- **UI changes** (embeds, views, slash command shape) → ASCII mock of the embed and any buttons/selects:
  ```
  PREVIEW:
  ┌─ embed: "toots' menu" ──────────────────────────┐
  │ description text...                             │
  ├──────────────────────────────────────────────────┤
  │ ▾ select 1                                       │
  │ [button] [button]                                │
  └──────────────────────────────────────────────────┘
  ```
- **Copy / persona / voice-library changes** → quote 2-3 sample outputs:
  ```
  PREVIEW (sample /ask response):
  > "is drake done"
  > → "he's been done four times this decade and keeps eating. give it up."
  ```
- **New command** → mock the slash command picker entry + an example response.
- **Pure backend changes** (db schema, refactors, dep bumps) → no PREVIEW section needed.

PR descriptions follow `.github/pull_request_template.md` which prompts for the same. Skip the section when it genuinely doesn't apply, don't pad with "N/A".
