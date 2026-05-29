"""Similarity-based dedup for bot-generated text.

Used by both discourse and chimein to prevent the bot from posting the
same take twice. Two signals, either one trips the gate:

  1. SAME SOURCE LINK: if the new post's trailing URL matches one a recent
     post already cited, it's a dup regardless of phrasing. This catches the
     "two differently-worded takes on the same story, identical link"
     case that text-similarity misses (two channels both grabbing the
     hottest tweet of the morning, e.g. issue: Drake chart post landing
     in both screening-room and main-stage).
  2. SIMILAR TEXT: normalized SequenceMatcher ratio over the threshold.
"""

from __future__ import annotations

import re
from difflib import SequenceMatcher

SIMILARITY_THRESHOLD = 0.6

_MENTION_RE = re.compile(r"<@!?\d+>")
_URL_RE = re.compile(r"https?://\S+")

# Embed-fixer / mirror hosts that resolve to the same underlying post. Folded
# to a canonical host so "fxtwitter.com/x/status/1" and "twitter.com/x/status/1"
# dedup against each other (the bot and the feeds use these interchangeably).
_HOST_ALIASES = {
    "fxtwitter.com": "twitter.com",
    "vxtwitter.com": "twitter.com",
    "fixupx.com": "x.com",
    "x.com": "twitter.com",
    "fixvx.com": "twitter.com",
    "tnktok.com": "tiktok.com",
    "vxtiktok.com": "tiktok.com",
    "www.tiktok.com": "tiktok.com",
    "www.twitter.com": "twitter.com",
}


def _normalize(text: str) -> str:
    text = _MENTION_RE.sub("", text)
    text = _URL_RE.sub("", text)
    return " ".join(text.lower().split())


def _canonical_url(url: str) -> str:
    """Lowercase, drop scheme / query / fragment / trailing junk, fold host aliases."""
    u = url.strip().rstrip(").,;!?\"'>")
    u = re.sub(r"^https?://", "", u, flags=re.IGNORECASE)
    u = u.split("?", 1)[0].split("#", 1)[0]
    u = u.lower().rstrip("/")
    host, _, rest = u.partition("/")
    host = _HOST_ALIASES.get(host, host)
    return f"{host}/{rest}" if rest else host


def _urls(text: str) -> set[str]:
    return {_canonical_url(u) for u in _URL_RE.findall(text)}


def is_duplicate_of_recent(line: str, recent_topics: list[str]) -> bool:
    """Return True if `line` repeats a recent post's link or is too similar in text."""
    line_urls = _urls(line)
    norm_line = _normalize(line)
    for topic in recent_topics:
        if line_urls and line_urls & _urls(topic):
            return True
        if not norm_line:
            continue
        norm_topic = _normalize(topic)
        if not norm_topic:
            continue
        if SequenceMatcher(None, norm_line, norm_topic).ratio() >= SIMILARITY_THRESHOLD:
            return True
    return False
