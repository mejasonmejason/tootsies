"""In-memory per-session abuse tracker for user silencing.

After ABUSE_THRESHOLD violations from a (guild_id, user_id) pair, Toots
stops responding to that user for the rest of the bot session (resets on restart).

Thresholds:
  - WARN_AT        : issue a canned warning, skip Claude call
  - ABUSE_THRESHOLD: silence the user entirely, emit abuse_silenced event

The detection itself runs through ClaudeClient.classify_abuse (Haiku);
this module only owns the counting + silenced-set state. Keeps the
classifier free to evolve without churning the bookkeeping layer.
"""

from __future__ import annotations

from collections import defaultdict

from utils.events import emit

ABUSE_THRESHOLD = 3
WARN_AT = 2

# {(guild_id, user_id): violation_count}
_violations: dict[tuple[int, int], int] = defaultdict(int)
_silenced: set[tuple[int, int]] = set()


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
