"""Dataclasses for DB rows. Pure transport — no ORM behavior."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum


class OrderStatus(StrEnum):
    PREPPING = "prepping"          # 🟡 Claude is drafting
    ON_THE_STOVE = "on_the_stove"  # 🍳 CI running
    NEEDS_TASTE = "needs_taste"    # 👀 owner review needed
    PLATING = "plating"            # 🚀 Railway deploying
    SERVED = "served"              # ✅ live
    BURNT = "burnt"                # 🔥 failed
    SENT_BACK = "sent_back"        # 🚫 rejected by pre-flight or Claude


ORDER_STATUS_EMOJI = {
    OrderStatus.PREPPING: "🟡",
    OrderStatus.ON_THE_STOVE: "🍳",
    OrderStatus.NEEDS_TASTE: "👀",
    OrderStatus.PLATING: "🚀",
    OrderStatus.SERVED: "✅",
    OrderStatus.BURNT: "🔥",
    OrderStatus.SENT_BACK: "🚫",
}

ORDER_STATUS_LABEL = {
    OrderStatus.PREPPING: "Prepping",
    OrderStatus.ON_THE_STOVE: "On the stove",
    OrderStatus.NEEDS_TASTE: "Needs a taste test",
    OrderStatus.PLATING: "Plating",
    OrderStatus.SERVED: "Served",
    OrderStatus.BURNT: "Burnt",
    OrderStatus.SENT_BACK: "Sent back",
}

# Terminal states — order is done one way or another.
TERMINAL_STATUSES = {OrderStatus.SERVED, OrderStatus.BURNT, OrderStatus.SENT_BACK}


class MoodMode(StrEnum):
    CHILL = "chill"   # 2 posts/day
    YAPS = "yaps"     # 4 posts/day
    OFF = "off"


@dataclass
class Server:
    guild_id: int
    configured: bool
    configured_at: datetime | None


@dataclass
class Order:
    id: int
    issue_number: int | None
    pr_number: int | None
    requester_id: int
    guild_id: int
    request_text: str
    summary: str
    status: OrderStatus
    created_at: datetime
    updated_at: datetime
    error_log: str | None


@dataclass
class ScheduleState:
    """Per-guild state for the scheduled discourse poster (the `discourse_schedule` table)."""

    guild_id: int
    mode: MoodMode
    last_changed_by: int | None
    last_changed_at: datetime | None
    posts_today: int
    last_post_at: datetime | None
