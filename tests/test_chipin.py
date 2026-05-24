"""Tests for the chip-in feature.

Focused on the pure / mockable surface:
  - _parse_chipin_score (defensive JSON parser, tolerant of fence + drift)
  - the cog's listen-set cache + buffer behavior
  - the gate sequence in _maybe_chip_in_one (hours, cooldown, daily cap,
    vibe, threshold, empty generation) using a stub DB + stub Claude.

The Discord plumbing itself (slash command invocation, channel send) isn't
exercised here, that's covered by test_commands.test_all_expected_commands_registered
which load the cog into a real tree.
"""

from __future__ import annotations

from collections import deque
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from claude_client import _parse_chipin_score
from cogs.chipin import (
    BUFFER_MIN_FOR_SCORE,
    COOLDOWN,
    DAILY_CAP,
    HOURS_END_ET_NEXT_DAY,
    HOURS_START_ET,
    SKIP_VIBES,
    THRESHOLD,
    ChipIn,
)

# ---- _parse_chipin_score ----------------------------------------------------


def test_parse_chipin_score_happy_path() -> None:
    text = '{"score": 0.78, "vibe": "debate", "hook": "kendrick vs drake"}'
    score, vibe, hook = _parse_chipin_score(text)
    assert score == pytest.approx(0.78)
    assert vibe == "debate"
    assert hook == "kendrick vs drake"


def test_parse_chipin_score_strips_code_fence() -> None:
    text = '```json\n{"score": 0.5, "vibe": "question", "hook": "asking about pizza"}\n```'
    score, vibe, hook = _parse_chipin_score(text)
    assert score == pytest.approx(0.5)
    assert vibe == "question"
    assert hook == "asking about pizza"


def test_parse_chipin_score_with_preamble() -> None:
    """Claude sometimes prefaces with prose. We grab the first {...} block."""
    text = (
        'Based on the buffer, here is my score:\n'
        '{"score": 0.9, "vibe": "hot_take", "hook": "spicy take on the bulls"}'
    )
    score, vibe, hook = _parse_chipin_score(text)
    assert score == pytest.approx(0.9)
    assert vibe == "hot_take"


def test_parse_chipin_score_clamps_out_of_range() -> None:
    over = _parse_chipin_score('{"score": 1.5, "vibe": "debate", "hook": "x"}')
    assert over[0] == 1.0
    under = _parse_chipin_score('{"score": -0.2, "vibe": "debate", "hook": "x"}')
    assert under[0] == 0.0


def test_parse_chipin_score_unknown_vibe_falls_back_to_other() -> None:
    text = '{"score": 0.7, "vibe": "philosophical", "hook": "x"}'
    score, vibe, hook = _parse_chipin_score(text)
    assert score == pytest.approx(0.7)  # score is still respected
    assert vibe == "other"  # unknown vibe coerced


def test_parse_chipin_score_invalid_score_returns_zero_fallback() -> None:
    text = '{"score": "high", "vibe": "debate", "hook": "x"}'
    assert _parse_chipin_score(text) == (0.0, "other", "")


def test_parse_chipin_score_missing_score_returns_zero_fallback() -> None:
    text = '{"vibe": "debate", "hook": "x"}'
    assert _parse_chipin_score(text) == (0.0, "other", "")


def test_parse_chipin_score_empty_input_returns_zero_fallback() -> None:
    assert _parse_chipin_score("") == (0.0, "other", "")


def test_parse_chipin_score_no_json_block_returns_zero_fallback() -> None:
    assert _parse_chipin_score("yeah that's a 7 out of 10 imo") == (0.0, "other", "")


def test_parse_chipin_score_malformed_json_returns_zero_fallback() -> None:
    assert _parse_chipin_score('{"score": 0.5, "vibe":}') == (0.0, "other", "")


def test_parse_chipin_score_non_string_vibe_coerced_to_other() -> None:
    text = '{"score": 0.7, "vibe": 5, "hook": "x"}'
    _, vibe, _hook = _parse_chipin_score(text)
    assert vibe == "other"


def test_parse_chipin_score_non_string_hook_coerced_to_empty() -> None:
    text = '{"score": 0.7, "vibe": "debate", "hook": 42}'
    _, _vibe, hook = _parse_chipin_score(text)
    assert hook == ""


# ---- ChipIn cog: listen cache + buffer --------------------------------------


def _make_cog() -> ChipIn:
    """Build a ChipIn cog without starting the background task loop."""
    bot = MagicMock()
    bot.wait_until_ready = AsyncMock()
    cog = ChipIn.__new__(ChipIn)
    # Skip __init__ (which starts the tick loop). Wire up just the attrs we test.
    from collections import defaultdict
    from collections import deque as _dq

    from cogs.chipin import BUFFER_MAX
    cog.bot = bot
    cog._buffers = defaultdict(lambda: _dq(maxlen=BUFFER_MAX))
    cog._new_since_eval = defaultdict(int)
    cog._cached_listen_set = None
    return cog


def test_listen_set_cache_starts_empty() -> None:
    cog = _make_cog()
    assert cog._listen_set_cache() == set()


def test_invalidate_listen_cache_resets_to_none() -> None:
    cog = _make_cog()
    cog._cached_listen_set = {(1, 2), (3, 4)}
    cog._invalidate_listen_cache()
    assert cog._cached_listen_set is None
    assert cog._listen_set_cache() == set()


# ---- ChipIn cog: gate sequence ----------------------------------------------


def _stub_message(content: str = "hello", author_id: int = 99) -> Any:
    msg = MagicMock()
    msg.content = content
    msg.author = SimpleNamespace(
        id=author_id, bot=False, display_name=f"user{author_id}",
    )
    msg.attachments = []
    msg.embeds = []
    msg.created_at = datetime.now(UTC)
    return msg


def _stub_db(
    *,
    last_chipin_at: datetime | None = None,
    count_today: int = 0,
) -> Any:
    db = MagicMock()
    db.last_chipin_at = AsyncMock(return_value=last_chipin_at)
    db.chipin_count_today = AsyncMock(return_value=count_today)
    db.record_chipin = AsyncMock()
    db.all_chipin_channels = AsyncMock(return_value=[])
    return db


def _stub_claude(
    *,
    score: float = 0.9,
    vibe: str = "debate",
    hook: str = "x",
    post_text: str = "real take",
) -> Any:
    claude = MagicMock()
    claude.chipin_score = AsyncMock(return_value=(score, vibe, hook))
    claude.chipin_post = AsyncMock(return_value=post_text)
    return claude


def _force_et_hour(monkeypatch: pytest.MonkeyPatch, hour: int) -> None:
    """Pin cogs.chipin.datetime.now() to a fixed hour in ET.

    Most gate tests want the hours_gate to PASS so they can exercise the
    next gate down. Default to noon ET (definitely inside the 9am-2am window).
    """
    from datetime import datetime as _dt
    from zoneinfo import ZoneInfo
    fake_now = _dt(2026, 5, 24, hour, 0, tzinfo=ZoneInfo("America/New_York"))

    class _FakeDT(_dt):
        @classmethod
        def now(cls, tz: Any = None) -> Any:
            if tz is not None:
                return fake_now.astimezone(tz)
            return fake_now

    monkeypatch.setattr("cogs.chipin.datetime", _FakeDT)


@pytest.mark.asyncio
async def test_gate_skips_outside_hours_window(monkeypatch: pytest.MonkeyPatch) -> None:
    """At 4am ET (between 2am and 9am) no chip-in should fire."""
    cog = _make_cog()
    cog.bot.db = _stub_db()
    claude = _stub_claude()
    cog.bot.claude = claude
    _force_et_hour(monkeypatch, 4)  # a banned hour: > 2am, < 9am
    await cog._maybe_chip_in_one(1, 2)
    # Should never have hit Claude, the hours_gate stopped us first.
    claude.chipin_score.assert_not_called()


@pytest.mark.asyncio
async def test_gate_skips_when_under_cooldown(monkeypatch: pytest.MonkeyPatch) -> None:
    cog = _make_cog()
    claude = _stub_claude()
    cog.bot.claude = claude
    _force_et_hour(monkeypatch, 12)  # ensure hours_gate passes
    # last_chipin_at must be relative to the FAKE now (the cog calls the patched
    # datetime.now), so compute it from the same source.
    from cogs.chipin import datetime as patched_datetime
    cog.bot.db = _stub_db(
        last_chipin_at=patched_datetime.now(UTC) - timedelta(minutes=5),
    )
    await cog._maybe_chip_in_one(1, 2)
    claude.chipin_score.assert_not_called()


@pytest.mark.asyncio
async def test_gate_skips_when_daily_cap_reached(monkeypatch: pytest.MonkeyPatch) -> None:
    cog = _make_cog()
    cog.bot.db = _stub_db(count_today=DAILY_CAP)
    claude = _stub_claude()
    cog.bot.claude = claude
    _force_et_hour(monkeypatch, 12)
    await cog._maybe_chip_in_one(1, 2)
    claude.chipin_score.assert_not_called()


@pytest.mark.asyncio
async def test_gate_skips_on_skip_vibe(monkeypatch: pytest.MonkeyPatch) -> None:
    """Even a high score gets dropped if vibe is in SKIP_VIBES."""
    cog = _make_cog()
    cog.bot.db = _stub_db()
    claude = _stub_claude(score=0.95, vibe="vulnerable")
    cog.bot.claude = claude
    cog._buffers[(1, 2)].extend([_stub_message() for _ in range(BUFFER_MIN_FOR_SCORE)])
    _force_et_hour(monkeypatch, 12)
    await cog._maybe_chip_in_one(1, 2)
    claude.chipin_score.assert_called_once()
    claude.chipin_post.assert_not_called()


@pytest.mark.asyncio
async def test_gate_skips_below_threshold(monkeypatch: pytest.MonkeyPatch) -> None:
    """A non-skip vibe with score below THRESHOLD doesn't fire."""
    cog = _make_cog()
    cog.bot.db = _stub_db()
    claude = _stub_claude(score=THRESHOLD - 0.1, vibe="debate")
    cog.bot.claude = claude
    cog._buffers[(1, 2)].extend([_stub_message() for _ in range(BUFFER_MIN_FOR_SCORE)])
    _force_et_hour(monkeypatch, 12)
    await cog._maybe_chip_in_one(1, 2)
    claude.chipin_score.assert_called_once()
    claude.chipin_post.assert_not_called()


def test_skip_vibes_subset_of_known_vibes() -> None:
    """Sanity: every vibe we skip is one the parser will actually emit."""
    from claude_client import _CHIPIN_VIBES
    assert SKIP_VIBES.issubset(_CHIPIN_VIBES)


def test_hours_window_constants_sane() -> None:
    """If someone fat-fingers the hours constants, fail loudly."""
    assert 0 <= HOURS_START_ET < 24
    # HOURS_END_ET_NEXT_DAY uses the 24+ convention to mean "next morning".
    assert HOURS_END_ET_NEXT_DAY > HOURS_START_ET
    assert HOURS_END_ET_NEXT_DAY <= 30  # at most 6am next day, anything beyond is a typo


def test_cooldown_and_cap_constants_sane() -> None:
    assert timedelta(minutes=1) <= COOLDOWN
    assert 1 <= DAILY_CAP <= 50
    assert 0.0 < THRESHOLD < 1.0


# ---- buffer maxlen enforced ---------------------------------------------------


def test_buffer_respects_maxlen() -> None:
    """We never want the in-memory buffer to grow unbounded per channel."""
    from cogs.chipin import BUFFER_MAX
    cog = _make_cog()
    key = (1, 2)
    for _ in range(BUFFER_MAX + 50):
        cog._buffers[key].append(_stub_message())
    assert len(cog._buffers[key]) == BUFFER_MAX
    assert isinstance(cog._buffers[key], deque)
