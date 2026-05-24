"""Tests for utils.feeds — media extraction, image-url harvesting, dead-channel logic."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import discord
import pytest

from utils.feeds import (
    channel_dead_diagnostic,
    extract_media,
    format_for_prompt,
    is_channel_dead,
    recent_image_urls,
)


def _fake_msg(
    content: str = "hi",
    display_name: str = "regular",
    attachments: list[object] | None = None,
    embeds: list[object] | None = None,
    reactions: list[object] | None = None,
) -> MagicMock:
    msg = MagicMock(spec=discord.Message)
    msg.content = content
    msg.author = SimpleNamespace(display_name=display_name, bot=False)
    msg.attachments = attachments or []
    msg.embeds = embeds or []
    msg.reactions = reactions or []
    return msg


def _fake_attachment(filename: str, content_type: str, size: int, url: str) -> object:
    return SimpleNamespace(filename=filename, content_type=content_type, size=size, url=url)


def _fake_embed(
    title: str | None = None,
    description: str | None = None,
    url: str | None = None,
    image_url: str | None = None,
    thumbnail_url: str | None = None,
) -> object:
    embed = SimpleNamespace(title=title, description=description, url=url)
    embed.image = SimpleNamespace(url=image_url) if image_url else None
    embed.thumbnail = SimpleNamespace(url=thumbnail_url) if thumbnail_url else None
    return embed


# ---- extract_media ---------------------------------------------------------------


def test_extract_media_returns_empty_for_plain_text() -> None:
    assert extract_media(_fake_msg()) == []


def test_extract_media_extracts_image_attachment_with_url() -> None:
    att = _fake_attachment("meme.png", "image/png", 100_000, "https://cdn/m.png")
    refs = extract_media(_fake_msg(attachments=[att]))
    assert len(refs) == 1
    assert refs[0].kind == "image"
    assert refs[0].image_url == "https://cdn/m.png"
    assert "meme.png" in refs[0].label


def test_extract_media_oversized_image_has_no_vision_url_but_still_referenced() -> None:
    """Images over Anthropic's vision cap (~5 MB) stay as refs (so Toots knows
    they exist) but don't get a loadable image_url, since vision would reject them.
    """
    big = _fake_attachment("huge.png", "image/png", 50 * 1024 * 1024, "https://cdn/b.png")
    refs = extract_media(_fake_msg(attachments=[big]))
    assert len(refs) == 1
    assert refs[0].kind == "image"
    assert refs[0].image_url is None
    assert "too large" in refs[0].label


def test_extract_media_video_attachment_no_vision_url() -> None:
    att = _fake_attachment("clip.mp4", "video/mp4", 1_000_000, "https://cdn/c.mp4")
    refs = extract_media(_fake_msg(attachments=[att]))
    assert len(refs) == 1
    assert refs[0].kind == "video"
    assert refs[0].image_url is None


def test_extract_media_embed_text_renders_with_url() -> None:
    embed = _fake_embed(title="Lakers Win", description="105-110 OT", url="https://x.com/a/b")
    refs = extract_media(_fake_msg(embeds=[embed]))
    assert any(r.kind == "embed" for r in refs)
    embed_ref = next(r for r in refs if r.kind == "embed")
    assert "Lakers Win" in embed_ref.label
    assert "105-110 OT" in embed_ref.label
    assert "x.com/a/b" in embed_ref.label


def test_extract_media_tenor_embed_yields_image_url() -> None:
    """Tenor and GIPHY GIFs come through as embed.image.url."""
    embed = _fake_embed(
        title="dancing gif",
        url="https://tenor.com/foo",
        image_url="https://media.tenor.com/x.gif",
    )
    refs = extract_media(_fake_msg(embeds=[embed]))
    image_refs = [r for r in refs if r.image_url is not None]
    assert image_refs, "tenor preview should be vision-loadable"
    assert image_refs[0].image_url == "https://media.tenor.com/x.gif"


def test_extract_media_falls_back_to_thumbnail_when_no_image() -> None:
    embed = _fake_embed(thumbnail_url="https://cdn/thumb.jpg")
    refs = extract_media(_fake_msg(embeds=[embed]))
    assert any(r.image_url == "https://cdn/thumb.jpg" for r in refs)


# ---- recent_image_urls -----------------------------------------------------------


def test_recent_image_urls_walks_newest_first_and_caps() -> None:
    """`messages` is oldest-first (per recent_messages docstring); we walk reverse."""
    older = _fake_msg(attachments=[_fake_attachment("a.png", "image/png", 1000, "url-A")])
    middle = _fake_msg(attachments=[_fake_attachment("b.png", "image/png", 1000, "url-B")])
    newest = _fake_msg(attachments=[_fake_attachment("c.png", "image/png", 1000, "url-C")])
    out = recent_image_urls([older, middle, newest], limit=2)
    assert out == ["url-C", "url-B"]


def test_recent_image_urls_empty_when_no_images() -> None:
    assert recent_image_urls([_fake_msg(), _fake_msg(content="lol")]) == []


def test_recent_image_urls_skips_video_only_messages() -> None:
    vid = _fake_msg(attachments=[_fake_attachment("c.mp4", "video/mp4", 1000, "url-V")])
    img = _fake_msg(attachments=[_fake_attachment("i.png", "image/png", 1000, "url-I")])
    out = recent_image_urls([vid, img], limit=5)
    assert out == ["url-I"]


# ---- format_for_prompt -----------------------------------------------------------


def test_format_for_prompt_inlines_media_labels() -> None:
    att = _fake_attachment("meme.png", "image/png", 100, "https://cdn/m.png")
    msg = _fake_msg(content="lookit", attachments=[att])
    rendered = format_for_prompt([msg])
    assert "lookit" in rendered
    assert "[image:" in rendered
    assert "meme.png" in rendered


def test_format_for_prompt_handles_empty_content_with_attachments() -> None:
    att = _fake_attachment("just-image.png", "image/png", 100, "https://cdn/i.png")
    msg = _fake_msg(content="", attachments=[att])
    rendered = format_for_prompt([msg])
    assert "regular:" in rendered
    assert "[image:" in rendered


def test_format_for_prompt_handles_empty_message_list() -> None:
    assert format_for_prompt([]) == "(no recent messages)"


# ---- dead-channel helpers --------------------------------------------------------


def test_is_channel_dead_only_when_literally_empty() -> None:
    assert is_channel_dead([]) is True
    assert is_channel_dead([_fake_msg(content="yo")]) is False
    assert is_channel_dead([_fake_msg(content="🔥")]) is False  # short messages count


def test_channel_dead_diagnostic_reports_permission_state() -> None:
    channel = MagicMock(spec=discord.TextChannel)
    channel.id = 123
    channel.name = "general"
    perms = SimpleNamespace(view_channel=True, read_message_history=False)
    channel.permissions_for.return_value = perms
    me = MagicMock(spec=discord.Member)
    diag = channel_dead_diagnostic(channel, me, [])
    assert diag["reason"] == "no_permission"
    assert diag["can_view"] is True
    assert diag["can_read_history"] is False


def test_channel_dead_diagnostic_reports_no_messages() -> None:
    channel = MagicMock(spec=discord.TextChannel)
    channel.id = 1
    channel.name = "quiet"
    perms = SimpleNamespace(view_channel=True, read_message_history=True)
    channel.permissions_for.return_value = perms
    me = MagicMock(spec=discord.Member)
    diag = channel_dead_diagnostic(channel, me, [])
    assert diag["reason"] == "no_messages"
    assert diag["total_messages"] == 0


# ---- recent_messages -------------------------------------------------------------


def _fake_channel_with_history(messages: list, *, can_read_perm: bool = True) -> MagicMock:
    """Build a channel whose .history() async-yields the given messages newest-first."""
    channel = MagicMock(spec=discord.TextChannel)
    perms = SimpleNamespace(view_channel=True, read_message_history=can_read_perm)
    channel.permissions_for = MagicMock(return_value=perms)

    async def fake_history(limit: int = 100):
        for msg in messages[:limit]:
            yield msg

    channel.history = fake_history
    return channel


@pytest.mark.asyncio
async def test_recent_messages_returns_empty_when_bot_cant_read() -> None:
    """Permission gate: no read_message_history -> empty list, no fetch attempted."""
    from utils.feeds import recent_messages

    msgs = [_fake_msg(content="should not appear")]
    channel = _fake_channel_with_history(msgs, can_read_perm=False)
    me = MagicMock(spec=discord.Member)
    out = await recent_messages(channel, me, limit=10)
    assert out == []


@pytest.mark.asyncio
async def test_recent_messages_returns_oldest_first() -> None:
    """Channel.history yields newest-first; we reverse so callers see oldest-first."""
    from utils.feeds import recent_messages

    m1 = _fake_msg(content="first")  # oldest in conversation
    m2 = _fake_msg(content="second")
    m3 = _fake_msg(content="third")  # newest
    # channel.history yields newest first by Discord convention
    channel = _fake_channel_with_history([m3, m2, m1])
    me = MagicMock(spec=discord.Member)
    out = await recent_messages(channel, me, limit=10)
    contents = [m.content for m in out]
    assert contents == ["first", "second", "third"]


@pytest.mark.asyncio
async def test_recent_messages_skips_bots_by_default() -> None:
    from utils.feeds import recent_messages

    human = _fake_msg(content="hi", display_name="alice")
    bot_msg = _fake_msg(content="beep", display_name="botty")
    bot_msg.author.bot = True
    channel = _fake_channel_with_history([bot_msg, human])
    me = MagicMock(spec=discord.Member)
    out = await recent_messages(channel, me, limit=10)
    assert [m.author.display_name for m in out] == ["alice"]


@pytest.mark.asyncio
async def test_recent_messages_includes_bots_when_requested() -> None:
    """/recap and /discourse feed reads need bot messages (webhooks, feed bots)."""
    from utils.feeds import recent_messages

    human = _fake_msg(content="hi", display_name="alice")
    bot_msg = _fake_msg(content="news drop", display_name="botty")
    bot_msg.author.bot = True
    channel = _fake_channel_with_history([bot_msg, human])
    me = MagicMock(spec=discord.Member)
    out = await recent_messages(channel, me, limit=10, include_bots=True)
    assert [m.author.display_name for m in out] == ["alice", "botty"]


@pytest.mark.asyncio
async def test_recent_messages_keeps_image_only_messages() -> None:
    """A message with no text but an image attachment is still worth surfacing."""
    from utils.feeds import recent_messages

    image_only = _fake_msg(
        content="",
        attachments=[_fake_attachment("meme.png", "image/png", 1000, "https://cdn/m.png")],
    )
    channel = _fake_channel_with_history([image_only])
    me = MagicMock(spec=discord.Member)
    out = await recent_messages(channel, me, limit=10)
    assert len(out) == 1


@pytest.mark.asyncio
async def test_recent_messages_skips_empty_messages_with_no_media() -> None:
    from utils.feeds import recent_messages

    empty = _fake_msg(content="")
    real = _fake_msg(content="hi")
    channel = _fake_channel_with_history([real, empty])
    me = MagicMock(spec=discord.Member)
    out = await recent_messages(channel, me, limit=10)
    assert [m.content for m in out] == ["hi"]


@pytest.mark.asyncio
async def test_recent_messages_respects_within_cutoff() -> None:
    """When `within` is passed, stop walking history once we cross that boundary."""
    from datetime import UTC, datetime, timedelta

    from utils.feeds import recent_messages

    now = datetime.now(UTC)
    fresh = _fake_msg(content="fresh")
    fresh.created_at = now - timedelta(minutes=10)
    stale = _fake_msg(content="stale")
    stale.created_at = now - timedelta(hours=5)
    channel = _fake_channel_with_history([fresh, stale])
    me = MagicMock(spec=discord.Member)
    out = await recent_messages(channel, me, limit=10, within=timedelta(hours=1))
    assert [m.content for m in out] == ["fresh"]
