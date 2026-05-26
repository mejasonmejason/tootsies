"""Environment-backed config. Read once at import time so failures surface fast."""

from __future__ import annotations

import os
from dataclasses import dataclass

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass


def _required(name: str) -> str:
    val = os.environ.get(name)
    if not val:
        raise RuntimeError(f"missing required env var: {name}")
    return val


@dataclass(frozen=True)
class Config:
    discord_token: str
    anthropic_api_key: str
    github_token: str
    github_repo: str
    database_url: str
    perplexity_api_key: str | None
    sports_game_odds_api_key: str | None
    railway_api_token: str | None
    railway_service_id: str | None
    bot_logs_verbosity: str  # full | milestones | errors
    health_port: int
    log_level: str

    @classmethod
    def from_env(cls) -> Config:
        return cls(
            discord_token=_required("DISCORD_TOKEN"),
            anthropic_api_key=_required("ANTHROPIC_API_KEY"),
            github_token=_required("GITHUB_TOKEN"),
            github_repo=os.environ.get("GITHUB_REPO", "mejasonmejason/tootsies"),
            database_url=_required("DATABASE_URL"),
            perplexity_api_key=os.environ.get("PERPLEXITY_API_KEY") or None,
            sports_game_odds_api_key=os.environ.get("SPORTS_GAME_ODDS_API_KEY") or None,
            railway_api_token=os.environ.get("RAILWAY_API_TOKEN") or None,
            railway_service_id=os.environ.get("RAILWAY_SERVICE_ID") or None,
            bot_logs_verbosity=os.environ.get("BOT_LOGS_VERBOSITY", "milestones").lower(),
            health_port=int(os.environ.get("HEALTH_PORT", "8080")),
            log_level=os.environ.get("LOG_LEVEL", "INFO").upper(),
        )
