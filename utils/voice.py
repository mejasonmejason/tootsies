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
    "Kitchen's full. {count} on the rail already, max is {cap}. Wait for one to clear.",
    "{count}/{cap} cooking. Line's closed til something's done.",
    "Rail's stacked, {count} deep. Check `/order status` and try again in a bit.",
    "Hold up, {count} tickets up already. Cap's {cap}.",
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

ABUSE_WARNING = [
    "that's your warning. one more like that and I'm out.",
    "heads up. you've got one left before I stop engaging.",
    "keep it clean. next one and we're done.",
]

ABUSE_SILENCED = [
    "bar's closed for you. we're done.",
    "cut off. not engaging.",
    "done. take it somewhere else.",
]


# Reaction emoji Toots drops on a message instead of posting a full take, the
# lighter-touch "I'm here and I clocked that" move. Keyed by the chimein vibe
# that scored the buffer. vulnerable/catchup/other never reach here (SKIP_VIBES),
# so they're intentionally absent; _REACTION_FALLBACK covers any drift.
REACTION_EMOJI_BY_VIBE: dict[str, list[str]] = {
    "debate": ["👀", "🤔", "🙄"],
    "hot_take": ["🔥", "💀", "🧢"],
    "question": ["🤔", "👀"],
    "conversational": ["😭", "💀", "🙏"],
}
_REACTION_FALLBACK = ["👀", "🔥", "😭"]


def pick(pool: list[str]) -> str:
    """Random variant from a pool."""
    return random.choice(pool)


def pick_reaction(vibe: str) -> str:
    """An in-voice reaction emoji for a chimein vibe (random within the pool)."""
    return random.choice(REACTION_EMOJI_BY_VIBE.get(vibe, _REACTION_FALLBACK))


def order_in_flight(count: int, cap: int) -> str:
    return random.choice(ORDER_IN_FLIGHT).format(count=count, cap=cap)
