"""In-memory per-session abuse tracker for user silencing.

After ABUSE_THRESHOLD violations from a (guild_id, user_id) pair, Toots
stops responding to that user for the rest of the bot session (resets on restart).

Thresholds:
  - WARN_AT        : issue a canned warning, skip Claude call
  - ABUSE_THRESHOLD: silence the user entirely, emit abuse_silenced event

Detection uses simple regex patterns targeting explicit sexual harassment and
self-harm directives. Deliberately narrow to minimize false positives; Claude's
constitution already handles milder rudeness.
"""

from __future__ import annotations

import re
from collections import defaultdict

from utils.events import emit

ABUSE_THRESHOLD = 3
WARN_AT = 2

_ABUSE_PATTERNS: list[re.Pattern[str]] = [
    # self-harm directives aimed at the bot
    re.compile(r"\bkill\s+yourself\b", re.IGNORECASE),
    re.compile(r"\bkys\b", re.IGNORECASE),
    # explicit sexual demands / harassment
    re.compile(r"\b(suck|blow)\s+(my|this)\s+dick\b", re.IGNORECASE),
    re.compile(r"\btake\s+(this|my)\s+dick\b", re.IGNORECASE),
    re.compile(r"\bpull\s+your\s+pants\s+down\b", re.IGNORECASE),
    re.compile(r"\b(fuck|rape)\s+you\b", re.IGNORECASE),
    re.compile(r"\bmy\s+(slut|whore)\b", re.IGNORECASE),
]

# {(guild_id, user_id): violation_count}
_violations: dict[tuple[int, int], int] = defaultdict(int)
_silenced: set[tuple[int, int]] = set()


def is_abusive(text: str) -> bool:
    """Return True if text matches any hardcoded abuse pattern."""
    return any(p.search(text) for p in _ABUSE_PATTERNS)


def record_violation(guild_id: int, user_id: int) -> int:
    """Increment violation count; silence user at ABUSE_THRESHOLD. Returns new count."""
    key = (guild_id, user_id)
    _violations[key] += 1
    count = _violations[key]
    if count == ABUSE_THRESHOLD:
        # Emit exactly once when crossing the threshold.
        _silenced.add(key)
        emit("abuse_silenced", guild_id=guild_id, user_id=user_id, violations=count)
    elif count > ABUSE_THRESHOLD:
        _silenced.add(key)  # keep silenced if somehow called again
    elif count == WARN_AT:
        emit("abuse_warned", guild_id=guild_id, user_id=user_id, violations=count)
    return count


def is_silenced(guild_id: int, user_id: int) -> bool:
    """Return True if this user has been silenced due to repeated abuse."""
    return (guild_id, user_id) in _silenced


def get_violations(guild_id: int, user_id: int) -> int:
    """Current violation count for (guild_id, user_id)."""
    return _violations[(guild_id, user_id)]


def _reset(guild_id: int, user_id: int) -> None:
    """Remove a user's violation state. Used in tests."""
    key = (guild_id, user_id)
    _violations.pop(key, None)
    _silenced.discard(key)
