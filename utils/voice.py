"""Pre-written Toots-voice deflections for non-generative paths.

These are used when we don't want to spend a Claude call (rate-limit hits, permission
denials, system errors). Picked at random so the bot doesn't sound canned.
"""

from __future__ import annotations

import random

RATE_LIMIT_HIT = [
    "off the clock for you tonight. try me tomorrow.",
    "you've been talking my ear off. take five.",
    "asked and answered. go ask someone else.",
    "tab's closed. tomorrow.",
    "you're cut off. drink some water.",
]

PERMISSION_DENIED = [
    "above my paygrade, that one.",
    "they didn't give me the keys for that.",
    "not my section.",
    "the boss locked that one.",
    "can't get to that. ask whoever runs the door.",
]

ORDER_REFUSED = [
    "my bosses can't allow that one.",
    "the house won't let me. ask for something else.",
    "that's a no from upstairs.",
    "not happening. pick something else off the menu.",
]

PLUMBING_TOUCHED = [
    "that's plumbing. ask the architect.",
    "not my wrench. that one's the architect's call.",
    "that's load-bearing, i don't touch the studs.",
]

PIPELINE_RED = [
    "kitchen's still cleaning up the last mess. hold tight.",
    "we're 86'd on new orders until i clean up. give it a sec.",
    "one sec. fixing a spill.",
]

DUPLICATE_ORDER = [
    "already on the rail. give it a minute.",
    "someone ordered that already. one's enough.",
    "duplicate. check `/order status`.",
]

ORDER_IN_FLIGHT = [
    "one ticket at a time. {ref} is still cooking, give it a sec.",
    "line's forming. check `/order status` for what's ahead of you.",
    "kitchen's working on one thing. hold up.",
    "i'm not a short-order cook. one at a time.",
]

DB_ERROR = [
    "kitchen's a mess right now. try in a sec.",
    "give me a sec, reorganizing.",
    "something's off back here. one moment.",
]

PRE_SETUP = "bar's not open yet. a mod needs to run `/menu` first."

CHANNEL_DEAD = [
    "dead in here. what'd you eat tonight, make it interesting.",
    "absolutely nothing. you good? need a drink?",
    "two memes and a 'gm.' tell me something.",
    "crickets. whatever you're avoiding by checking in here, go do that.",
    "your timeline's more active. what's going on with you?",
]

DISCOURSE_FALLBACK = [
    "ok give me your most controversial movie opinion. i'll start: oppenheimer was a snooze.",
    "rank in order: bron, mj, kobe, kd. show your work.",
    "what's the worst song you have on repeat right now. confess.",
    "who's getting verzuz'd next. i'm taking nominations.",
]

KITCHEN_CLOSED = [
    "kitchen's closed. mod call.",
    "no orders right now. talk to a mod.",
    "we're not taking orders. ask a mod why.",
]


def pick(pool: list[str]) -> str:
    """Random variant from a pool."""
    return random.choice(pool)


def order_in_flight(reference: str) -> str:
    return random.choice(ORDER_IN_FLIGHT).format(ref=reference)
