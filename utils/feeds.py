"""Channel-history readers used by /ask, /recap, /discourse."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import discord

from utils.permissions import can_read

# Image size cap. Claude's vision API rejects huge images, and even within the
# limit, big files burn tokens fast. 5 MB matches Anthropic's documented ceiling.
_VISION_MAX_BYTES = 5 * 1024 * 1024


def _truncate(text: str, n: int) -> str:
    return text if len(text) <= n else text[: n - 1] + "…"


@dataclass
class MediaRef:
    """One piece of media attached to a Discord message.

    `image_url` is set only when the media is something Claude's vision can load
    (image attachment under the size cap, or an embed image / Tenor preview).
    For non-loadable media (video files, generic attachments, embed text only),
    `image_url` stays None and `label` is the prompt-friendly description.
    """

    kind: str  # "image" | "video" | "file" | "embed"
    label: str
    image_url: str | None = None


def extract_media(msg: discord.Message) -> list[MediaRef]:
    """Pull embed text, attachments, and embedded images out of a message."""
    refs: list[MediaRef] = []

    # Embeds — Discord auto-unfurls links into embeds (X posts, articles, GIFs).
    for embed in msg.embeds:
        text_parts: list[str] = []
        if embed.title:
            text_parts.append(_truncate(embed.title, 120))
        if embed.description:
            text_parts.append(_truncate(embed.description, 200))
        if text_parts:
            url_suffix = f" ({embed.url})" if embed.url else ""
            refs.append(MediaRef(kind="embed", label=" / ".join(text_parts) + url_suffix))
        # Tenor / GIPHY surface their preview via embed.image.url.
        if embed.image and embed.image.url:
            refs.append(MediaRef(
                kind="image", label="embed image", image_url=embed.image.url,
            ))
        elif embed.thumbnail and embed.thumbnail.url:
            refs.append(MediaRef(
                kind="image", label="embed thumbnail", image_url=embed.thumbnail.url,
            ))

    # Direct attachments — uploaded files.
    for att in msg.attachments:
        ct = att.content_type or ""
        if ct.startswith("image/"):
            # Oversized images still get a ref (so Toots knows an image was posted)
            # but no image_url — too big for the vision API.
            if att.size <= _VISION_MAX_BYTES:
                refs.append(MediaRef(
                    kind="image", label=f"image: {att.filename}", image_url=att.url,
                ))
            else:
                refs.append(MediaRef(kind="image", label=f"image (too large): {att.filename}"))
        elif ct.startswith("video/"):
            refs.append(MediaRef(kind="video", label=f"video: {att.filename}"))
        elif ct.startswith("audio/"):
            refs.append(MediaRef(kind="file", label=f"audio: {att.filename}"))
        else:
            refs.append(MediaRef(kind="file", label=f"file: {att.filename}"))

    return refs


def recent_image_urls(messages: list[discord.Message], limit: int = 3) -> list[str]:
    """Image URLs ranked by relevance (reaction count) then recency.

    Smart-cap so we don't fan out vision blocks on every call. Vision tokens
    are pricier than text and Anthropic charges a fixed overhead per image,
    so we cap aggressively.

    Ranking: messages with reactions come first (highest count first),
    then messages with no reactions filled in most-recent first. The reasoning:
    if the room reacted to it, it's almost certainly more relevant to /recap
    or /ask than something nobody engaged with — even if older.
    """
    candidates: list[tuple[int, datetime, str]] = []
    for msg in messages:
        reaction_count = sum(r.count for r in msg.reactions) if msg.reactions else 0
        for ref in extract_media(msg):
            if ref.image_url is not None:
                candidates.append((reaction_count, msg.created_at, ref.image_url))
                break  # one image per message; avoids one viral message hogging the cap
    # Sort: reactions DESC, then recency DESC.
    candidates.sort(key=lambda item: (-item[0], -item[1].timestamp()))
    return [url for _, _, url in candidates[:limit]]


# Cheap URL detector for picking out bare links in message text. Discord auto-
# unfurls many links into rich embeds (which we capture separately in
# extract_media), but plenty of sites either block the unfurl or just produce
# a stripped embed; the bare URL is the only signal we have.
_URL_RE = re.compile(r"https?://\S+", re.IGNORECASE)


def hot_urls(
    messages: list[discord.Message], limit: int = 8,
) -> list[tuple[str, int, str]]:
    """URLs from message content, ranked by reactions then recency.

    Returns (url, reaction_count, posting_author_display_name) tuples. Used to
    surface "here are the links the room actually engaged with" in /recap
    prompts so Toots can decide which ones to actually open.
    """
    out: list[tuple[int, datetime, str, str]] = []
    seen: set[str] = set()
    for msg in messages:
        if not msg.content:
            continue
        reaction_count = sum(r.count for r in msg.reactions) if msg.reactions else 0
        author_name = getattr(msg.author, "display_name", "?")
        for url in _URL_RE.findall(msg.content):
            # Strip trailing punctuation that's almost always not part of the URL.
            url = url.rstrip(").,;:!?'\"")
            if url in seen:
                continue
            seen.add(url)
            out.append((reaction_count, msg.created_at, url, author_name))
    out.sort(key=lambda item: (-item[0], -item[1].timestamp()))
    return [(url, rxn, name) for rxn, _, url, name in out[:limit]]


async def recent_messages(
    channel: discord.TextChannel | discord.Thread,
    me: discord.Member,
    limit: int = 30,
    within: timedelta | None = None,
    include_bots: bool = False,
) -> list[discord.Message]:
    """Pull recent messages, skipping empty content.

    `include_bots` defaults False because for /ask the context is "what are humans
    chatting about?". For /recap we want to summarize EVERYTHING that happened, and
    for /discourse feed-channel reads the bot/webhook posts ARE the content, so the
    caller passes True there.
    """
    if not can_read(channel, me):
        return []
    cutoff = datetime.now(UTC) - within if within else None
    msgs: list[discord.Message] = []
    async for msg in channel.history(limit=limit * 2):
        if msg.author.bot and not include_bots:
            continue
        # An image-only message has no content but still carries info worth reading.
        # Keep messages with attachments or embeds even when the text body is empty.
        if not msg.content.strip() and not msg.attachments and not msg.embeds:
            continue
        if cutoff and msg.created_at < cutoff:
            break
        msgs.append(msg)
        if len(msgs) >= limit:
            break
    msgs.reverse()
    return msgs


def format_for_prompt(messages: list[discord.Message], include_reactions: bool = False) -> str:
    """Render a message list for inclusion in a Claude prompt.

    Uses display names (no IDs) and truncates content. Media (embeds, attachments,
    GIFs) is inlined as `[<kind>: <label>]` tags so Claude sees what was posted
    even when we don't fan out a separate vision block for the image itself.
    Reactions optionally appended so /recap can weight popular messages.
    """
    if not messages:
        return "(no recent messages)"
    lines: list[str] = []
    for m in messages:
        name = m.author.display_name
        body = _truncate(m.content.replace("\n", " "), 200)
        line = f"{name}: {body}" if body else f"{name}:"
        media = extract_media(m)
        if media:
            line += " " + " ".join(f"[{r.kind}: {r.label}]" for r in media)
        if include_reactions and m.reactions:
            counts = sum(r.count for r in m.reactions)
            if counts:
                line += f"  [{counts} reactions]"
        lines.append(line)
    return "\n".join(lines)


def is_channel_dead(messages: list[discord.Message]) -> bool:
    """Hard floor only: deflect when the channel is *literally* empty over the period.

    Previously this required 3+ messages of >5 chars each, which filtered out reactions,
    short replies, link drops, and one-word chat. In a real channel that's everyone.
    The Claude /recap prompt already instructs Toots to quip when the content is thin,
    so we trust her judgment for anything non-zero.
    """
    return not messages


def channel_dead_diagnostic(
    channel: discord.TextChannel | discord.Thread,
    me: discord.Member,
    messages: list[discord.Message],
) -> dict[str, object]:
    """Why did the channel look dead? Returns structured fields for events + bot-logs.

    Always called only when `is_channel_dead(messages)` is True, so we know
    `messages` is empty. The interesting question is whether that's because the
    period was empty or because the bot couldn't read.
    """
    perms = channel.permissions_for(me) if isinstance(
        channel, discord.TextChannel | discord.Thread
    ) else None
    return {
        "channel_id": channel.id,
        "channel_name": channel.name,
        "can_view": bool(perms and perms.view_channel),
        "can_read_history": bool(perms and perms.read_message_history),
        "total_messages": len(messages),
        "reason": (
            "no_permission" if perms and not perms.read_message_history
            else "no_messages"
        ),
    }
