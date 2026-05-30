# tootsies

A Discord bot for the Tootsies server. The bot is "Toots", a hip city-girl bartender persona. Mods ship new features by typing `/order <feature>` in Discord; Claude writes the code, CI runs, Railway redeploys.

**→ For mods, members, and a single-page overview**: https://mejasonmejason.github.io/tootsies/

---

## For developers

### Stack

- Python 3.11+, `discord.py` 2.4
- Postgres on Railway (`asyncpg`)
- Anthropic API: Haiku 4.5 for `/ask`, `/recap`, scheduler, chime-in scoring, market intent routing, deflections; Sonnet 4.6 for `/discourse`, chime-in posting, the long-term-memory write pipeline, and `/order` pre-flight
- Live data: SportsGameOdds (sports lines), Polymarket + Kalshi (prediction markets), Perplexity (news/trends) — see env vars below
- GitHub Actions running `claude-code-action` for the `/order` pipeline
- Railway for hosting + auto-deploy on push to `main`

### Local dev

```bash
python3.11 -m venv .venv
source .venv/bin/activate
pip install -r requirements-dev.txt
cp .env.example .env  # fill in tokens
python bot.py
```

You need a running Postgres reachable via `DATABASE_URL`. The bot bootstraps its own schema on startup.

Required env vars: `DISCORD_TOKEN`, `ANTHROPIC_API_KEY`, `GITHUB_TOKEN`, `DATABASE_URL`. Optional enrichers degrade gracefully when unset: `SPORTS_GAME_ODDS_API_KEY` (live sports lines), `PERPLEXITY_API_KEY` (news/trends; Polymarket + Kalshi need no key). See [.env.example](.env.example) for the full list.

### Checks (all enforced in CI)

```bash
ruff check .
mypy .
pytest        # includes --cov + --cov-fail-under=50
```

Offline eval harnesses (not part of CI; run against the live API): `evals/eval_ask.py`, `evals/eval_kalshi_discovery.py`, `scripts/eval_commands.py`, `scripts/eval_memory_fence.py`, `scripts/eval_music_post.py`.

### Deployment

Push to `main`. Railway builds via the [Dockerfile](Dockerfile) and starts the bot. Healthcheck on `/health`. Slash commands re-sync per guild on every startup, so deploys pick up new commands automatically.

### Adding features

In Discord, as a mod:

```
/order new add a /dadjoke command that tells a dad joke
```

The bot pre-flight-checks via Sonnet, files a GitHub issue tagged `@claude`, the action writes a PR, CI runs, auto-merges if green, Railway redeploys. Status narrated in `#bot-logs`.

### Reference

- **[CLAUDE.md](CLAUDE.md)**: developer intro, structured event catalog, conventions
- **[docs/ALGORITHMS.md](docs/ALGORITHMS.md)**: per-command flow + tunable knobs
- **[CHANGELOG.md](CHANGELOG.md)**: what changed and when
- **[EXECUTION_PLAN.md](EXECUTION_PLAN.md)**: frozen v1 design artifact
- **[docs/](docs/)**: GitHub Pages source for the public site
