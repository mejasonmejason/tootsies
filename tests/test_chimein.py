"""Tests for the chime-in feature.

Focused on the pure / mockable surface:
  - _parse_chimein_score (defensive JSON parser, tolerant of fence + drift)
  - buffer maxlen + constant sanity
  - mood-based cadence tuning (chill is stricter than yaps)
  - the gate sequence in _maybe_chime_in_one (mood, hours, cooldown, daily cap,
    vibe, threshold) using a stub DB + stub Claude.

Chime-in has no slash commands of its own (it rides on the discourse_channel
+ mood settings configured via /menu), so there's no command-registration
surface to test here.
"""

from __future__ import annotations

from collections import defaultdict
from collections import deque as _dq
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest

from claude_client import _parse_chimein_score
from cogs.chimein import (
    BUFFER_MAX,
    BUFFER_MIN_FOR_SCORE,
    HOURS_END_ET_NEXT_DAY,
    HOURS_START_ET,
    MOOD_TUNING,
    SKIP_VIBES,
    ChimeIn,
)
from models import MoodMode, ScheduleState
from utils.dedup import is_duplicate_of_recent

# ---- _parse_chimein_score ----------------------------------------------------


def test_parse_chimein_score_happy_path() -> None:
    text = '{"score": 0.78, "vibe": "debate", "hook": "kendrick vs drake"}'
    score, vibe, hook = _parse_chimein_score(text)
    assert score == pytest.approx(0.78)
    assert vibe == "debate"
    assert hook == "kendrick vs drake"


def test_parse_chimein_score_strips_code_fence() -> None:
    text = '```json\n{"score": 0.5, "vibe": "question", "hook": "asking about pizza"}\n```'
    score, vibe, hook = _parse_chimein_score(text)
    assert score == pytest.approx(0.5)
    assert vibe == "question"
    assert hook == "asking about pizza"


def test_parse_chimein_score_with_preamble() -> None:
    """Claude sometimes prefaces with prose. We grab the first {...} block."""
    text = (
        'Based on the buffer, here is my score:\n'
        '{"score": 0.9, "vibe": "hot_take", "hook": "spicy take on the bulls"}'
    )
    score, vibe, hook = _parse_chimein_score(text)
    assert score == pytest.approx(0.9)
    assert vibe == "hot_take"


def test_parse_chimein_score_clamps_out_of_range() -> None:
    over = _parse_chimein_score('{"score": 1.5, "vibe": "debate", "hook": "x"}')
    assert over[0] == 1.0
    under = _parse_chimein_score('{"score": -0.2, "vibe": "debate", "hook": "x"}')
    assert under[0] == 0.0


def test_parse_chimein_score_unknown_vibe_falls_back_to_other() -> None:
    text = '{"score": 0.7, "vibe": "philosophical", "hook": "x"}'
    score, vibe, hook = _parse_chimein_score(text)
    assert score == pytest.approx(0.7)  # score is still respected
    assert vibe == "other"  # unknown vibe coerced


def test_parse_chimein_score_invalid_score_returns_zero_fallback() -> None:
    text = '{"score": "high", "vibe": "debate", "hook": "x"}'
    assert _parse_chimein_score(text) == (0.0, "other", "")


def test_parse_chimein_score_missing_score_returns_zero_fallback() -> None:
    text = '{"vibe": "debate", "hook": "x"}'
    assert _parse_chimein_score(text) == (0.0, "other", "")


def test_parse_chimein_score_empty_input_returns_zero_fallback() -> None:
    assert _parse_chimein_score("") == (0.0, "other", "")


def test_parse_chimein_score_no_json_block_returns_zero_fallback() -> None:
    assert _parse_chimein_score("yeah that's a 7 out of 10 imo") == (0.0, "other", "")


def test_parse_chimein_score_malformed_json_returns_zero_fallback() -> None:
    assert _parse_chimein_score('{"score": 0.5, "vibe":}') == (0.0, "other", "")


def test_parse_chimein_score_non_string_vibe_coerced_to_other() -> None:
    text = '{"score": 0.7, "vibe": 5, "hook": "x"}'
    _, vibe, _hook = _parse_chimein_score(text)
    assert vibe == "other"


def test_parse_chimein_score_non_string_hook_coerced_to_empty() -> None:
    text = '{"score": 0.7, "vibe": "debate", "hook": 42}'
    _, _vibe, hook = _parse_chimein_score(text)
    assert hook == ""


# ---- ChimeIn cog: helpers --------------------------------------------------


def _make_cog() -> ChimeIn:
    """Build a ChimeIn cog without starting the background task loop."""
    bot = MagicMock()
    bot.wait_until_ready = AsyncMock()
    cog = ChimeIn.__new__(ChimeIn)
    # Skip __init__ (which starts the tick loop). Wire up just the attrs we test.
    cog.bot = bot
    cog._buffers = defaultdict(lambda: _dq(maxlen=BUFFER_MAX))
    cog._new_since_eval = defaultdict(int)
    cog._listen_channels = {}
    cog._last_react_at = {}
    cog._react_count = {}
    return cog


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


def _stub_schedule(mood: MoodMode = MoodMode.CHILL) -> ScheduleState:
    return ScheduleState(
        guild_id=1, mood=mood,
        last_changed_by=None, last_changed_at=None,
        posts_today=0, last_post_at=None,
    )


def _stub_db(
    *,
    last_chimein_at: datetime | None = None,
    count_today: int = 0,
    mood: MoodMode = MoodMode.CHILL,
) -> Any:
    db = MagicMock()
    db.last_chimein_at = AsyncMock(return_value=last_chimein_at)
    db.chimein_count_today = AsyncMock(return_value=count_today)
    db.record_chimein = AsyncMock()
    db.add_discourse = AsyncMock()
    db.get_schedule = AsyncMock(return_value=_stub_schedule(mood))
    db.recent_discourse_all = AsyncMock(return_value=[])
    return db


def _stub_claude(
    *,
    score: float = 0.9,
    vibe: str = "debate",
    hook: str = "x",
    post_text: str = "real take",
) -> Any:
    claude = MagicMock()
    claude.chimein_score = AsyncMock(return_value=(score, vibe, hook))
    claude.chimein_post = AsyncMock(return_value=post_text)
    return claude


def _force_et_hour(monkeypatch: pytest.MonkeyPatch, hour: int) -> None:
    """Pin cogs.chimein.datetime.now() to a fixed hour in ET.

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

    monkeypatch.setattr("cogs.chimein.datetime", _FakeDT)


# ---- ChimeIn cog: gate sequence ----------------------------------------------


@pytest.mark.asyncio
async def test_gate_skips_when_mood_off(monkeypatch: pytest.MonkeyPatch) -> None:
    """The simplest gate: if mood is off, chime-in never even checks the buffer."""
    cog = _make_cog()
    cog.bot.db = _stub_db(mood=MoodMode.OFF)
    claude = _stub_claude()
    cog.bot.claude = claude
    _force_et_hour(monkeypatch, 12)
    await cog._maybe_chime_in_one(1, 2)
    claude.chimein_score.assert_not_called()


@pytest.mark.asyncio
async def test_gate_skips_outside_hours_window(monkeypatch: pytest.MonkeyPatch) -> None:
    """At 4am ET (between 2am and 9am) no chime-in should fire."""
    cog = _make_cog()
    cog.bot.db = _stub_db()
    claude = _stub_claude()
    cog.bot.claude = claude
    _force_et_hour(monkeypatch, 4)  # a banned hour: > 2am, < 9am
    await cog._maybe_chime_in_one(1, 2)
    # Should never have hit Claude, the hours_gate stopped us first.
    claude.chimein_score.assert_not_called()


@pytest.mark.asyncio
async def test_post_cooldown_blocks_post_but_still_scores(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Chill 60min cooldown: a 30min-old post blocks POSTING, but we still score
    so a reaction can fill the gap (reactions ride their own separate cooldown)."""
    cog = _make_cog()
    claude = _stub_claude()
    cog.bot.claude = claude
    cog._buffers[(1, 2)].extend([_stub_message() for _ in range(BUFFER_MIN_FOR_SCORE)])
    _force_et_hour(monkeypatch, 12)  # ensure hours_gate passes
    # last_chimein_at must be relative to the FAKE now (the cog calls the patched
    # datetime.now), so compute it from the same source.
    from cogs.chimein import datetime as patched_datetime
    cog.bot.db = _stub_db(
        mood=MoodMode.CHILL,
        last_chimein_at=patched_datetime.now(UTC) - timedelta(minutes=30),
    )
    await cog._maybe_chime_in_one(1, 2)
    claude.chimein_score.assert_called_once()  # scored so it could react
    claude.chimein_post.assert_not_called()    # but post is on cooldown


@pytest.mark.asyncio
async def test_yaps_passes_cooldown_that_chill_would_block(monkeypatch: pytest.MonkeyPatch) -> None:
    """Same 30min-old chime-in: chill blocks (60min cooldown), yaps doesn't (20min)."""
    cog = _make_cog()
    claude = _stub_claude()
    cog.bot.claude = claude
    cog._buffers[(1, 2)].extend([_stub_message() for _ in range(BUFFER_MIN_FOR_SCORE)])
    _force_et_hour(monkeypatch, 12)
    from cogs.chimein import datetime as patched_datetime
    cog.bot.db = _stub_db(
        mood=MoodMode.YAPS,
        last_chimein_at=patched_datetime.now(UTC) - timedelta(minutes=30),
    )
    await cog._maybe_chime_in_one(1, 2)
    # 30min > yaps cooldown (20min), so we got past it and called the scorer.
    claude.chimein_score.assert_called_once()


@pytest.mark.asyncio
async def test_chill_daily_cap_lower_than_yaps(monkeypatch: pytest.MonkeyPatch) -> None:
    """count=6 today: chill (cap 5) blocks the post, yaps (cap 10) doesn't. Both
    still score, chill so it can react, yaps so it can post."""
    cog = _make_cog()
    claude = _stub_claude()
    cog.bot.claude = claude
    cog._buffers[(1, 2)].extend([_stub_message() for _ in range(BUFFER_MIN_FOR_SCORE)])
    _force_et_hour(monkeypatch, 12)
    cog.bot.db = _stub_db(mood=MoodMode.CHILL, count_today=6)
    await cog._maybe_chime_in_one(1, 2)
    claude.chimein_score.assert_called_once()  # scores so it can react
    claude.chimein_post.assert_not_called()    # chill cap of 5 blocks the post

    cog2 = _make_cog()
    claude2 = _stub_claude()
    cog2.bot.claude = claude2
    cog2._buffers[(1, 2)].extend([_stub_message() for _ in range(BUFFER_MIN_FOR_SCORE)])
    cog2.bot.db = _stub_db(mood=MoodMode.YAPS, count_today=6)
    await cog2._maybe_chime_in_one(1, 2)
    claude2.chimein_score.assert_called_once()  # 6 < yaps cap of 10


@pytest.mark.asyncio
async def test_gate_skips_on_skip_vibe(monkeypatch: pytest.MonkeyPatch) -> None:
    """Even a high score gets dropped if vibe is in SKIP_VIBES."""
    cog = _make_cog()
    cog.bot.db = _stub_db()
    claude = _stub_claude(score=0.95, vibe="vulnerable")
    cog.bot.claude = claude
    cog._buffers[(1, 2)].extend([_stub_message() for _ in range(BUFFER_MIN_FOR_SCORE)])
    _force_et_hour(monkeypatch, 12)
    await cog._maybe_chime_in_one(1, 2)
    claude.chimein_score.assert_called_once()
    claude.chimein_post.assert_not_called()


@pytest.mark.asyncio
async def test_chill_threshold_higher_than_yaps(monkeypatch: pytest.MonkeyPatch) -> None:
    """A score of 0.7: chill (threshold 0.8) blocks at threshold_gate, yaps (0.6) doesn't.

    We can't easily reach the send-message step without faking the whole Discord
    channel resolution, so we capture every `emit()` call instead and assert the
    threshold_gate decision shows up for chill but not for yaps.
    """
    emitted_chill: list[tuple[str, dict[str, Any]]] = []
    monkeypatch.setattr(
        "cogs.chimein.emit",
        lambda ev, **f: emitted_chill.append((ev, f)),
    )

    cog = _make_cog()
    cog.bot.db = _stub_db(mood=MoodMode.CHILL)
    claude = _stub_claude(score=0.7, vibe="debate")
    cog.bot.claude = claude
    cog._buffers[(1, 2)].extend([_stub_message() for _ in range(BUFFER_MIN_FOR_SCORE)])
    _force_et_hour(monkeypatch, 12)
    await cog._maybe_chime_in_one(1, 2)
    assert any(
        ev == "chimein_evaluated" and f.get("decision") == "threshold_gate"
        for ev, f in emitted_chill
    ), f"chill should have hit threshold_gate at score 0.7, got {emitted_chill}"

    emitted_yaps: list[tuple[str, dict[str, Any]]] = []
    monkeypatch.setattr(
        "cogs.chimein.emit",
        lambda ev, **f: emitted_yaps.append((ev, f)),
    )

    cog2 = _make_cog()
    cog2.bot.db = _stub_db(mood=MoodMode.YAPS)
    claude2 = _stub_claude(score=0.7, vibe="debate")
    cog2.bot.claude = claude2
    cog2._buffers[(1, 2)].extend([_stub_message() for _ in range(BUFFER_MIN_FOR_SCORE)])
    await cog2._maybe_chime_in_one(1, 2)
    assert not any(
        ev == "chimein_evaluated" and f.get("decision") == "threshold_gate"
        for ev, f in emitted_yaps
    ), f"yaps should NOT have hit threshold_gate at score 0.7, got {emitted_yaps}"


# ---- near-miss reactions ------------------------------------------------------


def _reactable_message() -> Any:
    """A buffered message whose channel grants Toots add_reactions perms."""
    msg = _stub_message()
    channel = MagicMock(spec=discord.TextChannel)
    channel.id = 2
    channel.permissions_for = MagicMock(return_value=SimpleNamespace(
        view_channel=True, read_message_history=True, add_reactions=True,
    ))
    msg.channel = channel
    guild = MagicMock(spec=discord.Guild)
    guild.id = 1
    guild.me = MagicMock(spec=discord.Member)
    msg.guild = guild
    msg.id = 4242
    msg.reactions = []
    msg.add_reaction = AsyncMock()
    return msg


def _fill_buffer_ending_with(cog: ChimeIn, target: Any) -> None:
    """Seed (1,2) buffer with enough messages to score, `target` last (react target)."""
    cog._buffers[(1, 2)].extend(
        [_stub_message() for _ in range(BUFFER_MIN_FOR_SCORE - 1)] + [target]
    )


@pytest.mark.asyncio
async def test_near_miss_reacts_instead_of_threshold_gate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Score in [0.45, threshold): react to the latest message, decision='reacted'."""
    emitted: list[tuple[str, dict[str, Any]]] = []
    monkeypatch.setattr("cogs.chimein.emit", lambda ev, **f: emitted.append((ev, f)))

    cog = _make_cog()
    cog.bot.db = _stub_db(mood=MoodMode.CHILL)  # post threshold 0.8
    cog.bot.claude = _stub_claude(score=0.6, vibe="debate")  # near-miss
    target = _reactable_message()
    _fill_buffer_ending_with(cog, target)
    _force_et_hour(monkeypatch, 12)

    await cog._maybe_chime_in_one(1, 2)

    target.add_reaction.assert_awaited_once()
    assert any(
        ev == "chimein_evaluated" and f.get("decision") == "reacted"
        for ev, f in emitted
    ), f"expected a 'reacted' decision, got {emitted}"
    assert (1, 2) in cog._last_react_at


@pytest.mark.asyncio
async def test_below_react_floor_stays_silent(monkeypatch: pytest.MonkeyPatch) -> None:
    """Score under REACT_THRESHOLD: no reaction, falls through to threshold_gate."""
    emitted: list[tuple[str, dict[str, Any]]] = []
    monkeypatch.setattr("cogs.chimein.emit", lambda ev, **f: emitted.append((ev, f)))

    cog = _make_cog()
    cog.bot.db = _stub_db(mood=MoodMode.CHILL)
    cog.bot.claude = _stub_claude(score=0.3, vibe="debate")  # below floor
    target = _reactable_message()
    _fill_buffer_ending_with(cog, target)
    _force_et_hour(monkeypatch, 12)

    await cog._maybe_chime_in_one(1, 2)

    target.add_reaction.assert_not_awaited()
    assert any(
        ev == "chimein_evaluated" and f.get("decision") == "threshold_gate"
        for ev, f in emitted
    )


@pytest.mark.asyncio
async def test_react_respects_cooldown(monkeypatch: pytest.MonkeyPatch) -> None:
    """A recent reaction in this channel blocks another within REACT_COOLDOWN."""
    monkeypatch.setattr("cogs.chimein.emit", lambda ev, **f: None)

    cog = _make_cog()
    cog.bot.db = _stub_db(mood=MoodMode.CHILL)
    cog.bot.claude = _stub_claude(score=0.6, vibe="debate")
    cog._last_react_at[(1, 2)] = datetime.now(UTC)  # just reacted
    target = _reactable_message()
    _fill_buffer_ending_with(cog, target)
    _force_et_hour(monkeypatch, 12)

    await cog._maybe_chime_in_one(1, 2)

    target.add_reaction.assert_not_awaited()


@pytest.mark.asyncio
async def test_post_cooldown_still_allows_reaction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Headline fix: posting on cooldown still lets a near-miss get a reaction."""
    emitted: list[tuple[str, dict[str, Any]]] = []
    monkeypatch.setattr("cogs.chimein.emit", lambda ev, **f: emitted.append((ev, f)))

    cog = _make_cog()
    claude = _stub_claude(score=0.6, vibe="debate")  # under chill 0.8 post bar
    cog.bot.claude = claude
    _force_et_hour(monkeypatch, 12)
    from cogs.chimein import datetime as patched_datetime
    cog.bot.db = _stub_db(
        mood=MoodMode.CHILL,
        last_chimein_at=patched_datetime.now(UTC) - timedelta(minutes=30),  # on cooldown
    )
    target = _reactable_message()
    _fill_buffer_ending_with(cog, target)

    await cog._maybe_chime_in_one(1, 2)

    target.add_reaction.assert_awaited_once()
    claude.chimein_post.assert_not_called()
    assert any(
        ev == "chimein_evaluated" and f.get("decision") == "reacted"
        for ev, f in emitted
    )


@pytest.mark.asyncio
async def test_skips_scoring_when_post_and_react_both_blocked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cost-saving: post on cooldown AND reaction on its own cooldown => no score."""
    emitted: list[tuple[str, dict[str, Any]]] = []
    monkeypatch.setattr("cogs.chimein.emit", lambda ev, **f: emitted.append((ev, f)))

    cog = _make_cog()
    claude = _stub_claude()
    cog.bot.claude = claude
    _force_et_hour(monkeypatch, 12)
    from cogs.chimein import datetime as patched_datetime
    cog.bot.db = _stub_db(
        mood=MoodMode.CHILL,
        last_chimein_at=patched_datetime.now(UTC) - timedelta(minutes=30),
    )
    cog._last_react_at[(1, 2)] = patched_datetime.now(UTC)  # react also on cooldown
    cog._buffers[(1, 2)].extend([_stub_message() for _ in range(BUFFER_MIN_FOR_SCORE)])

    await cog._maybe_chime_in_one(1, 2)

    claude.chimein_score.assert_not_called()
    assert any(
        ev == "chimein_evaluated" and f.get("decision") == "cooldown_gate"
        for ev, f in emitted
    )


@pytest.mark.asyncio
async def test_react_respects_daily_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    """Reactions stop once REACT_DAILY_CAP is hit for the day, even off cooldown."""
    monkeypatch.setattr("cogs.chimein.emit", lambda ev, **f: None)

    cog = _make_cog()
    cog.bot.db = _stub_db(mood=MoodMode.CHILL)
    cog.bot.claude = _stub_claude(score=0.6, vibe="debate")
    _force_et_hour(monkeypatch, 12)
    from cogs.chimein import REACT_DAILY_CAP
    from cogs.chimein import datetime as patched_datetime
    cog._react_count[(1, 2)] = (patched_datetime.now(UTC).date(), REACT_DAILY_CAP)
    target = _reactable_message()
    _fill_buffer_ending_with(cog, target)

    await cog._maybe_chime_in_one(1, 2)

    target.add_reaction.assert_not_awaited()


def test_skip_vibes_subset_of_known_vibes() -> None:
    """Sanity: every vibe we skip is one the parser will actually emit."""
    from claude_client import _CHIMEIN_VIBES
    assert SKIP_VIBES.issubset(_CHIMEIN_VIBES)


def test_hours_window_constants_sane() -> None:
    """If someone fat-fingers the hours constants, fail loudly."""
    assert 0 <= HOURS_START_ET < 24
    # HOURS_END_ET_NEXT_DAY uses the 24+ convention to mean "next morning".
    assert HOURS_END_ET_NEXT_DAY > HOURS_START_ET
    assert HOURS_END_ET_NEXT_DAY <= 30  # at most 6am next day, anything beyond is a typo


def test_mood_tuning_chill_more_reserved_than_yaps() -> None:
    """The chill knobs should always be at least as conservative as yaps."""
    chill = MOOD_TUNING[MoodMode.CHILL]
    yaps = MOOD_TUNING[MoodMode.YAPS]
    assert chill.threshold > yaps.threshold  # higher bar to chime in
    assert chill.daily_cap < yaps.daily_cap  # fewer chime-ins per day
    assert chill.cooldown > yaps.cooldown    # longer between chime-ins
    # Every non-OFF mood has a tuning row.
    assert {MoodMode.CHILL, MoodMode.YAPS} <= MOOD_TUNING.keys()
    # OFF is intentionally absent (the mood gate stops it before tuning is read).
    assert MoodMode.OFF not in MOOD_TUNING


def test_mood_tuning_sane_ranges() -> None:
    for mood, tuning in MOOD_TUNING.items():
        assert 0.0 < tuning.threshold < 1.0, mood
        assert 1 <= tuning.daily_cap <= 50, mood
        assert timedelta(minutes=1) <= tuning.cooldown, mood


# ---- buffer maxlen enforced ---------------------------------------------------


def test_buffer_respects_maxlen() -> None:
    """We never want the in-memory buffer to grow unbounded per channel."""
    cog = _make_cog()
    key = (1, 2)
    for _ in range(BUFFER_MAX + 50):
        cog._buffers[key].append(_stub_message())
    assert len(cog._buffers[key]) == BUFFER_MAX


# ---- multi-channel listen routing -------------------------------------------


def test_on_message_buffers_all_configured_channels() -> None:
    """Messages in ANY configured discourse channel should be buffered."""
    cog = _make_cog()
    cog._listen_channels = {1: {100, 200}}

    msg_a = _stub_message("hello from channel A")
    msg_a.guild = SimpleNamespace(id=1)
    msg_a.channel = SimpleNamespace(id=100)

    msg_b = _stub_message("hello from channel B")
    msg_b.guild = SimpleNamespace(id=1)
    msg_b.channel = SimpleNamespace(id=200)

    msg_other = _stub_message("not a discourse channel")
    msg_other.guild = SimpleNamespace(id=1)
    msg_other.channel = SimpleNamespace(id=999)

    for msg in [msg_a, msg_b, msg_other]:
        listen = cog._listen_channels.get(msg.guild.id)
        if listen and msg.channel.id in listen:
            key = (msg.guild.id, msg.channel.id)
            cog._buffers[key].append(msg)

    assert len(cog._buffers[(1, 100)]) == 1
    assert len(cog._buffers[(1, 200)]) == 1
    assert (1, 999) not in cog._buffers


@pytest.mark.asyncio
async def test_refresh_listen_channels_multi() -> None:
    """_refresh_listen_channels should populate sets from get_discourse_channels."""
    cog = _make_cog()
    cog.bot.guilds = [MagicMock(id=1), MagicMock(id=2)]
    cog.bot.db = MagicMock()
    cog.bot.db.get_discourse_channels = AsyncMock(
        side_effect=lambda gid: [100, 200] if gid == 1 else [],
    )
    await cog._refresh_listen_channels()
    assert cog._listen_channels == {1: {100, 200}}
    assert 2 not in cog._listen_channels


@pytest.mark.asyncio
async def test_stale_buffer_cleaned_on_channel_removal() -> None:
    """If a channel is removed from discourse config, its buffer should be dropped."""
    cog = _make_cog()
    cog._listen_channels = {1: {100}}
    cog._buffers[(1, 100)].append(_stub_message())
    cog._buffers[(1, 999)].append(_stub_message())
    cog._new_since_eval[(1, 100)] = 2
    cog._new_since_eval[(1, 999)] = 3

    cog.bot.db = _stub_db()
    cog.bot.claude = _stub_claude()

    await cog._maybe_chime_in_all()
    assert (1, 999) not in cog._buffers
    assert (1, 999) not in cog._new_since_eval


# ---- dedup gate (shared by discourse + chimein) --------------------------------


def test_dedup_catches_exact_repeat() -> None:
    recent = ["Spider-Noir drops tomorrow on Prime. Nic Cage doing a whole TV series"]
    assert is_duplicate_of_recent(
        "Spider-Noir drops tomorrow on Prime. Nic Cage doing a whole TV series", recent,
    )


def test_dedup_catches_near_repeat() -> None:
    recent = ["spider-noir drops tomorrow on prime, nic cage is wild"]
    assert is_duplicate_of_recent(
        "spider-noir drops tomorrow on prime. nic cage is wild.", recent,
    )


def test_dedup_allows_different_topic() -> None:
    recent = ["Spider-Noir drops tomorrow on Prime"]
    assert not is_duplicate_of_recent(
        "knicks finals tickets going for 8k courtside, that's a mortgage payment", recent,
    )


def test_dedup_ignores_urls_and_mentions() -> None:
    recent = ["check this out https://fxtwitter.com/foo <@123456>"]
    assert not is_duplicate_of_recent("completely different take", recent)


def test_dedup_empty_line() -> None:
    assert not is_duplicate_of_recent("", ["some recent post"])


def test_dedup_empty_recent() -> None:
    assert not is_duplicate_of_recent("any post", [])
