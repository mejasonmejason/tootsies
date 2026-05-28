"""Basic command-shape tests, names registered, voice helpers behave."""

from __future__ import annotations

import pytest

from utils import voice


def test_voice_pools_are_nonempty() -> None:
    pools = [
        voice.RATE_LIMIT_HIT,
        voice.PERMISSION_DENIED,
        voice.ORDER_REFUSED,
        voice.PIPELINE_RED,
        voice.DUPLICATE_ORDER,
        voice.ORDER_IN_FLIGHT,
        voice.DB_ERROR,
        voice.CHANNEL_DEAD,
        voice.DISCOURSE_FALLBACK,
        voice.KITCHEN_CLOSED,
    ]
    for pool in pools:
        assert len(pool) >= 1
        for line in pool:
            assert isinstance(line, str)
            assert line.strip()


def test_order_in_flight_formats_count_and_cap() -> None:
    line = voice.order_in_flight(3, 3)
    assert isinstance(line, str) and line.strip()


@pytest.mark.asyncio
async def test_all_expected_commands_registered() -> None:
    """The day-one launch command surface is all present on the tree."""
    from bot import COGS, TootsiesBot
    from config import Config

    bot = TootsiesBot(Config.from_env())
    for cog in COGS:
        await bot.load_extension(cog)

    names: set[str] = set()
    for cmd in bot.tree.walk_commands():
        # GroupCog children show up with parent.child naming on tree walks; collect both.
        names.add(cmd.qualified_name)

    expected = {
        "ask",
        "recap",
        "discourse",  # both manual posts (category:) and schedule control (mood:) live here
        "order new",
        "order status",
        "order retry",
        "order cancel",
        "close",
        "open",
        "undo",
        "menu",  # /menu now serves as both setup and view (no separate /menu_view)
        "help",
        # /chimein has no commands; it's a background listener wired to
        # discourse_channel + mood (set via /menu).
    }
    missing = expected - names
    assert not missing, f"missing commands: {missing}\nregistered: {sorted(names)}"
    await bot.close()


def test_models_terminal_statuses_consistent() -> None:
    from models import ORDER_STATUS_EMOJI, ORDER_STATUS_LABEL, TERMINAL_STATUSES, OrderStatus

    for status in OrderStatus:
        assert status in ORDER_STATUS_EMOJI
        assert status in ORDER_STATUS_LABEL
    # The plan defines these three as terminal.
    assert {OrderStatus.SERVED, OrderStatus.BURNT, OrderStatus.SENT_BACK} == TERMINAL_STATUSES
