# Changelog

All notable changes to Tootsies. Dates in PT.

## [1.0.0] — 2026-05-24 (initial launch)

### Commands

**Everyone**
- `/ask <question>` — answers in Toots voice. Reads recent channel chatter (vibe only), web-searches for facts, sees up to 8 recent images via Claude vision.
- `/recap period:<1h | 1d | today>` — channel summary with reactions weighted. Web-augmented for facts the room is referencing. Sees recent images so it can name the meme that got reactions.
- `/discourse category:<pop | sports | cinema | hiphop | nba | custom>` — drops a discourse starter in the invoked channel. Pulls from configured feed channels, current channel's last hour, and the web. State-aware dedup over the last 72h.
- `@Toots <question>` — same backend as `/ask`. Discord's auto-mention on replies is ignored; explicit @Toots required.
- `/help` — overview of every command, who can run what, the daily caps.

**Mods only**
- `/order new <feature>` — files a GitHub issue tagged `@claude`. Claude writes a PR, CI runs (ruff/mypy/pytest with ≥50% coverage), auto-merges if green, Railway redeploys. Live narration in `#bot-logs`.
- `/order status [filter:mine | all | in-progress | failed]` — list orders.
- `/order retry <issue#>` — retry a failed order.
- `/order cancel <issue#>` — kill an in-flight order.
- `/discourse mood:<chill | yaps | off | status>` — control the scheduled posting cadence.
- `/menu` — interactive setup. Loads saved settings if already configured. Configure channels, mod roles, feed channels.
- `/close` / `/open` — toggle whether new `/order` requests are accepted.
- `/undo` — roll back to the previous successful Railway deploy via the Railway API.

### Pipeline

- GitHub Actions: ruff + mypy + pytest (coverage gate ≥50%) on every PR.
- Claude Code Action with `@claude` trigger writes PRs in response to `/order` issues.
- Railway auto-deploys `main` on push, gated by "Wait for CI."
- Healthcheck on `/health` triggers automatic restart on failure.
- `/undo` uses Railway GraphQL `deploymentRedeploy(usePreviousImageTag: true)` for fast rollback (no rebuild).

### Persona & guardrails

- Toots persona: late-20s bartender voice; sharp, lowercase, terse; ~140 char answer cap.
- **No em dashes** — enforced by test (`tests/test_persona.py::test_no_em_dashes_in_persona_constitution_or_voice`).
- **Restate the question** on `/ask` so answers read self-contained later in scrollback. Restatement doesn't count toward the cap.
- **Constitution** (`constitution.py`): no doxxing, no NSFW, no slurs, no impersonation, no medical/legal/financial advice, no moderation actions, guild-only, minors handled with flattened persona, crisis content breaks character with real resources. Cannot be loosened by `/order`.
- **Protected paths** in `/order` pre-flight: constitution, persona core voice, `.github/`, Dockerfile, railway.toml, Procfile, db.py connection, bot.py boot, requirements.txt deletions. Pre-flight Sonnet pass rejects these with a Toots-voice deflection ("that's plumbing, regular. ask the architect.").

### Observability

- Structured JSON event lines prefixed with `EVENT` for Railway log-based dashboards. See [CLAUDE.md](CLAUDE.md#structured-events-for-dashboards) for the event catalog.
- Event kinds: `command`, `claude_api`, `order_state`, `rate_limit_hit`, `deploy_event`, `error`, `recap_deflected`, `discourse_fallback`.
- `command_metrics` Postgres table for per-invocation latency/success/error tracking (30-day retention).
- `audit_log` table for /menu changes, /close, /open, /undo, order rejections, state transitions (90-day retention).
- Verbosity-gated `#bot-logs` posts for order status, recap deflections, discourse fallbacks.

### Infrastructure

- Python 3.11+, discord.py 2.4, asyncpg 0.30, anthropic 0.40, aiohttp 3.10.
- Postgres for settings, rate limits, order history, audit log, discourse dedup, command metrics, schedule state.
- Idempotent schema bootstrap on every boot, including a legacy `mood_state` → `discourse_schedule` migration block.
- Dockerfile + railway.toml; healthcheck path `/health` with 30s timeout, restart-on-failure with max 5 retries.

### Cost controls

- Persona prompt cached with `cache_control: ephemeral` (~1 k tokens saved per repeat call within 5 min).
- Time context prefix (~25 tokens) fixes day-of-week hallucinations.
- Vision hard cap: 10 images per Claude call (with smaller per-caller caps).
- 5 MB image size cap (Anthropic vision limit).

### Documentation

- [CLAUDE.md](CLAUDE.md) — developer-facing project intro, conventions, event catalog.
- [docs/ALGORITHMS.md](docs/ALGORITHMS.md) — per-command flow + tunable knobs reference.
- [EXECUTION_PLAN.md](EXECUTION_PLAN.md) — frozen v1 design artifact.
- [.github/pull_request_template.md](.github/pull_request_template.md) — PREVIEW convention for UI changes.

### Test coverage

- 50.84% total (gate at 50%). Core utils all at ≥75%: `claude_client` 100%, `rate_limits` 100%, `events` 100%, `metrics` 98%, `permissions` 95%, `voice` 94%, `feeds` 92%, `config` 89%, `github` 82%, `railway` 75%.

---

## Unreleased

See [GitHub Issues](https://github.com/mejasonmejason/tootsies/issues) for the v1.1 backlog. Top candidates:

- **GIPHY gifs in replies** ([#2](https://github.com/mejasonmejason/tootsies/issues/2)) — Toots sends gifs when they land harder than words.
- **Per-user `/ask` memory** — "I asked about Lakers yesterday, what's new?"
- **`/stats` admin command** — reads `command_metrics` for in-Discord visibility.
- **Cog test coverage push** — bring `cogs/*` from 25–36% to 60%+.
- **AI code review pass on `/order` PRs** — second Claude reviews first Claude's PR.
- **`/order` from screenshot** — vision parses a posted image into a spec.
- **Thread-based `/ask`** — continue a conversation in a thread.
- **Voting / leaderboards** — track members' hot-take wins.
- **Real-time NBA scores tool** — balldontlie.io, no key needed.
