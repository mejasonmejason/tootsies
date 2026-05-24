"""Smoke tests — modules import cleanly, bot constructs, cogs load."""

from __future__ import annotations

import pytest


def test_imports() -> None:
    """Every package module imports without error."""
    import bot  # noqa: F401
    import claude_client  # noqa: F401
    import config  # noqa: F401
    import constitution  # noqa: F401
    import db  # noqa: F401
    import models  # noqa: F401
    import persona  # noqa: F401
    from cogs import admin, ask, discourse, order, recap, settings  # noqa: F401
    from utils import (  # noqa: F401
        bot_logs,
        feeds,
        gates,
        github,
        healthcheck,
        permissions,
        rate_limits,
        voice,
    )


def test_bot_constructs() -> None:
    """TootsiesBot can be constructed without connecting to Discord or DB."""
    from bot import TootsiesBot
    from config import Config

    cfg = Config.from_env()
    bot = TootsiesBot(cfg)
    assert bot.config is cfg
    assert bot.db is not None
    assert bot.claude is not None
    assert bot.gh is not None


@pytest.mark.asyncio
async def test_cogs_load_into_bot() -> None:
    """All cogs can be loaded into a freshly-constructed bot."""
    from bot import COGS, TootsiesBot
    from config import Config

    bot = TootsiesBot(Config.from_env())
    for cog in COGS:
        await bot.load_extension(cog)
    cog_names = {type(c).__name__ for c in bot.cogs.values()}
    assert "Ask" in cog_names
    assert "Recap" in cog_names
    assert "Discourse" in cog_names
    assert "Order" in cog_names
    assert "Admin" in cog_names
    assert "Settings" in cog_names
    await bot.close()
