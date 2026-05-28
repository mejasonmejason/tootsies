"""Pre-written Toots-voice deflections for non-generative paths.

These are used when we don't want to spend a Claude call (rate-limit hits, permission
denials, system errors). Picked at random so the bot doesn't sound canned.
"""

from __future__ import annotations

import random

RATE_LIMIT_HIT = [
    "Off the clock for you tonight. Try me tomorrow.",
    "You've been talking my ear off all day. Come back tomorrow.",
    "You're cut off. Drink some water, come back tomorrow.",
    "Tab's closed for today. Tomorrow.",
    "Asked and answered. Go ask someone else, or try me tomorrow.",
]

PERMISSION_DENIED = [
    "Above my paygrade, that one.",
    "They didn't give me the keys for that.",
    "Not my section.",
    "The boss locked that one.",
    "Can't get to that. Ask whoever runs the door.",
]

ORDER_REFUSED = [
    "My bosses can't allow that one.",
    "The house won't let me. Ask for something else.",
    "That's a no from upstairs.",
    "Not happening. Pick something else off the menu.",
]

PLUMBING_TOUCHED = [
    "That's plumbing. Ask the architect.",
    "Not my wrench. That one's the architect's call.",
    "That's load-bearing, I don't touch the studs.",
]

PIPELINE_RED = [
    "Kitchen's still cleaning up the last mess. Hold tight.",
    "We're 86'd on new orders until I clean up. Give it a sec.",
    "One sec. Fixing a spill.",
]

DUPLICATE_ORDER = [
    "Already on the rail. Give it a minute.",
    "Someone ordered that already. One's enough.",
    "Duplicate. Check `/order status`.",
]

ORDER_IN_FLIGHT = [
    "One ticket at a time. {ref} is still cooking, give it a sec.",
    "Line's forming. Check `/order status` for what's ahead of you.",
    "Kitchen's working on one thing. Hold up.",
    "I'm not a short-order cook. One at a time.",
]

DB_ERROR = [
    "Kitchen's a mess right now. Try in a sec.",
    "Give me a sec, reorganizing.",
    "Something's off back here. One moment.",
]

PRE_SETUP = "Bar's not open yet. A mod needs to run `/menu` first."

CHANNEL_DEAD = [
    "Dead in here. What'd you eat tonight, make it interesting.",
    "Absolutely nothing. You good? Need a drink?",
    "Two memes and a 'gm.' Tell me something.",
    "Crickets. Whatever you're avoiding by checking in here, go do that.",
    "Your timeline's more active. What's going on with you?",
]

DISCOURSE_FALLBACK = [
    "Ok give me your most controversial movie opinion. I'll start: Oppenheimer was a snooze.",
    "Rank in order: Bron, MJ, Kobe, KD. Show your work.",
    "What's the worst song you have on repeat right now. Confess.",
    "Who's getting Verzuz'd next. I'm taking nominations.",
]

KITCHEN_CLOSED = [
    "Kitchen's closed. Mod call.",
    "No orders right now. Talk to a mod.",
    "We're not taking orders. Ask a mod why.",
]


def pick(pool: list[str]) -> str:
    """Random variant from a pool."""
    return random.choice(pool)


def order_in_flight(reference: str) -> str:
    return random.choice(ORDER_IN_FLIGHT).format(ref=reference)
