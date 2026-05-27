"""Ensure responses fit within Discord's 2000-character message limit."""

from __future__ import annotations

DISCORD_MAX = 2000


def truncate(text: str, limit: int = DISCORD_MAX) -> str:
    """Cut *text* at the last clean line boundary that fits within *limit*."""
    if len(text) <= limit:
        return text
    cut = text.rfind("\n", 0, limit)
    if cut <= 0:
        cut = text.rfind(" ", 0, limit)
    if cut <= 0:
        cut = limit
    return text[:cut].rstrip()
