# tootsies

A Discord bot for the Tootsies server. The bot is "Toots" — a hip city-girl bartender persona. Mods can ship new features by typing `/order <feature>` in Discord; Claude Code Action writes the PR, CI runs, Railway redeploys.

See [EXECUTION_PLAN.md](EXECUTION_PLAN.md) for the full spec.

## Stack

- Python 3.11+, `discord.py`
- Postgres on Railway (`asyncpg`)
- Anthropic API (Haiku for `/ask`, `/recap`, `/mood`, deflections; Sonnet for `/discourse` and `/order` pre-flight)
- GitHub Actions running `claude-code-action` for the `/order` pipeline
- Railway for hosting + auto-deploy

## Local dev

```bash
python3.11 -m venv .venv
source .venv/bin/activate
pip install -r requirements-dev.txt
cp .env.example .env  # fill in tokens
python bot.py
```

You need a running Postgres reachable via `DATABASE_URL`. The bot bootstraps its own schema on startup.

## Checks

```bash
ruff check .
mypy .
pytest
```

## Deployment

Push to `main`. Railway builds via the [Dockerfile](Dockerfile) and starts the bot. Healthcheck is at `/health`. Slash commands are re-synced per guild on every startup, so deploys pick up new commands automatically.

## Adding features

In Discord, as a mod:

```
/order add a /dadjoke command that tells a dad joke
```

The bot pre-flight-checks the request with Sonnet, files a GitHub issue tagged `@claude`, the action writes a PR, CI runs, the PR auto-merges if green, Railway redeploys. Status narrated in `#bot-logs`.
