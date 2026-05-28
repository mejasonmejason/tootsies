"""Per-guild abuse counting + silencing, persisted in Postgres.

State lives in the `abuse_violations` table (see db.py). Survives Railway
deploys, which is the point: a user silenced at 11pm should still be
silenced at midnight after main redeploys.

Detection itself runs through ClaudeClient.classify_abuse (Haiku). This
module is the bookkeeping layer that calls into db.DB.

Thresholds:
  - WARN_AT        : canned warning quip, no Claude call for the answer
  - ABUSE_THRESHOLD: user marked silenced; mod-only `/silence lift` unlocks
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from utils.events import emit

if TYPE_CHECKING:
    from db import DB

ABUSE_THRESHOLD = 3
WARN_AT = 2


async def record_violation(db: DB, guild_id: int, user_id: int) -> int:
    """Increment violation count; silence at ABUSE_THRESHOLD. Returns new count."""
    count, just_silenced = await db.record_abuse_violation(
        guild_id, user_id, ABUSE_THRESHOLD,
    )
    if just_silenced:
        emit("abuse_silenced", guild_id=guild_id, user_id=user_id, violations=count)
    elif count == WARN_AT:
        emit("abuse_warned", guild_id=guild_id, user_id=user_id, violations=count)
    return count


async def is_silenced(db: DB, guild_id: int, user_id: int) -> bool:
    return await db.is_user_silenced(guild_id, user_id)
