"""Similarity-based dedup for bot-generated text.

Used by both discourse and chimein to prevent the bot from posting the
same take twice. Compares normalized text via SequenceMatcher.
"""

from __future__ import annotations

import re
from difflib import SequenceMatcher

SIMILARITY_THRESHOLD = 0.6

_MENTION_RE = re.compile(r"<@!?\d+>")
_URL_RE = re.compile(r"https?://\S+")


def _normalize(text: str) -> str:
    text = _MENTION_RE.sub("", text)
    text = _URL_RE.sub("", text)
    return " ".join(text.lower().split())


def is_duplicate_of_recent(line: str, recent_topics: list[str]) -> bool:
    """Return True if `line` is too similar to any string in `recent_topics`."""
    norm_line = _normalize(line)
    if not norm_line:
        return False
    for topic in recent_topics:
        norm_topic = _normalize(topic)
        if not norm_topic:
            continue
        if SequenceMatcher(None, norm_line, norm_topic).ratio() >= SIMILARITY_THRESHOLD:
            return True
    return False
