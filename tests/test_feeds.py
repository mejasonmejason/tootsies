"""Tests for utils.feeds, media extraction, image-url harvesting, dead-channel logic."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import discord
import pytest

from utils.feeds import (
    _classify_url,
    channel_dead_diagnostic,
    extract_media,
    format_for_prompt,
    hot_urls,
    is_channel_dead,
    recent_image_urls,
    resolve_reactors,
)


def _fake_msg(
    content: str = "hi",
    display_name: str = "regular",
    attachments: list[object] | None = None,
    embeds: list[object] | None = None,
    reactions: list[object] | None = None,
    created_at: object | None = None,
    msg_id: int | None = None,
) -> MagicMock:
    """Build a fake discord.Message with sane defaults.

    `created_at` defaults to "now" so reaction-weighted sorting falls back to
    recency tie-breakers consistently in tests that don't care about timestamps.
    """
    from datetime import UTC, datetime

    msg = MagicMock(spec=discord.Message)
    msg.content = content
    msg.author = SimpleNamespace(display_name=display_name, bot=False)
    msg.attachments = attachments or []
    msg.embeds = embeds or []
    msg.reactions = reactions or []
    msg.created_at = created_at or datetime.now(UTC)
    if msg_id is not None:
        msg.id = msg_id
    return msg


def _fake_reaction(count: int) -> object:
    return SimpleNamespace(count=count)


def _fake_reaction_with_users(
    emoji: object, names: list[str], count: int | None = None
) -> object:
    """A reaction whose .users() async-yields members with the given display names.

    `count` defaults to len(names); pass a larger count to simulate more reactors
    than the users() page returns (exercises the "+N more" path).
    """

    async def users(limit: int | None = None):
        for n in names[: limit if limit is not None else len(names)]:
            yield SimpleNamespace(display_name=n, name=n)

    rxn = SimpleNamespace(emoji=emoji, count=count if count is not None else len(names))
    rxn.users = users
    return rxn


def _fake_attachment(filename: str, content_type: str, size: int, url: str) -> object:
    return SimpleNamespace(filename=filename, content_type=content_type, size=size, url=url)


def _fake_embed(
    title: str | None = None,
    description: str | None = None,
    url: str | None = None,
    image_url: str | None = None,
    thumbnail_url: str | None = None,
    video_url: str | None = None,
) -> object:
    embed = SimpleNamespace(title=title, description=description, url=url)
    embed.image = SimpleNamespace(url=image_url) if image_url else None
    embed.thumbnail = SimpleNamespace(url=thumbnail_url) if thumbnail_url else None
    embed.video = SimpleNamespace(url=video_url) if video_url else None
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


def test_extract_media_uses_thumbnail_when_no_image() -> None:
    embed = _fake_embed(thumbnail_url="https://cdn/thumb.jpg")
    refs = extract_media(_fake_msg(embeds=[embed]))
    assert any(r.image_url == "https://cdn/thumb.jpg" for r in refs)


def test_extract_media_includes_both_image_and_thumbnail_when_both_exist() -> None:
    """Tier 1 stills boost: TikTok/Twitter video embeds often expose two
    different frames via image and thumbnail. Take both so vision sees more."""
    embed = _fake_embed(
        image_url="https://cdn/frame-a.jpg",
        thumbnail_url="https://cdn/frame-b.jpg",
    )
    refs = extract_media(_fake_msg(embeds=[embed]))
    image_urls = [r.image_url for r in refs if r.image_url is not None]
    assert "https://cdn/frame-a.jpg" in image_urls
    assert "https://cdn/frame-b.jpg" in image_urls


def test_extract_media_dedups_identical_image_and_thumbnail() -> None:
    """Some embeds set image and thumbnail to the same URL; only emit one ref."""
    embed = _fake_embed(
        image_url="https://cdn/same.jpg",
        thumbnail_url="https://cdn/same.jpg",
    )
    refs = extract_media(_fake_msg(embeds=[embed]))
    image_urls = [r.image_url for r in refs if r.image_url is not None]
    assert image_urls == ["https://cdn/same.jpg"]


def test_extract_media_surfaces_embed_video_url_as_text_reference() -> None:
    """We can't process video frames but knowing there's a video at all helps
    Claude form a better take ('this is a video post' vs 'this is just a still')."""
    embed = _fake_embed(
        title="some clip",
        image_url="https://cdn/cover.jpg",
        video_url="https://cdn/clip.mp4",
    )
    refs = extract_media(_fake_msg(embeds=[embed]))
    assert any(r.kind == "video" and "clip.mp4" in r.label for r in refs)


# ---- recent_image_urls -----------------------------------------------------------


def test_recent_image_urls_prefers_reacted_messages_over_recency() -> None:
    """A reacted-to image beats the most recent one with no reactions. This is the
    correctness fix: if the room engaged with it, it's more relevant to recap/ask.
    """
    from datetime import UTC, datetime, timedelta

    now = datetime.now(UTC)
    old_reacted = _fake_msg(
        attachments=[_fake_attachment("hit.png", "image/png", 1000, "url-HIT")],
        reactions=[_fake_reaction(7)],
        created_at=now - timedelta(hours=20),
    )
    newest_silent = _fake_msg(
        attachments=[_fake_attachment("meh.png", "image/png", 1000, "url-MEH")],
        created_at=now,
    )
    out = recent_image_urls([old_reacted, newest_silent], limit=2)
    assert out == ["url-HIT", "url-MEH"]


def test_recent_image_urls_falls_back_to_recency_with_no_reactions() -> None:
    """Tie-break: when nothing's reacted-to, the original newest-first behavior holds."""
    from datetime import UTC, datetime, timedelta

    now = datetime.now(UTC)
    older = _fake_msg(
        attachments=[_fake_attachment("a.png", "image/png", 1000, "url-A")],
        created_at=now - timedelta(hours=3),
    )
    middle = _fake_msg(
        attachments=[_fake_attachment("b.png", "image/png", 1000, "url-B")],
        created_at=now - timedelta(hours=1),
    )
    newest = _fake_msg(
        attachments=[_fake_attachment("c.png", "image/png", 1000, "url-C")],
        created_at=now,
    )
    out = recent_image_urls([older, middle, newest], limit=2)
    assert out == ["url-C", "url-B"]


def test_recent_image_urls_ranks_within_reacted_bucket_by_count() -> None:
    """Higher reaction count wins within the reacted-to bucket."""
    from datetime import UTC, datetime, timedelta

    now = datetime.now(UTC)
    medium = _fake_msg(
        attachments=[_fake_attachment("m.png", "image/png", 1000, "url-MED")],
        reactions=[_fake_reaction(3)],
        created_at=now,
    )
    viral = _fake_msg(
        attachments=[_fake_attachment("v.png", "image/png", 1000, "url-VIRAL")],
        reactions=[_fake_reaction(20)],
        created_at=now - timedelta(hours=10),
    )
    out = recent_image_urls([medium, viral], limit=2)
    assert out == ["url-VIRAL", "url-MED"]


def test_recent_image_urls_empty_when_no_images() -> None:
    assert recent_image_urls([_fake_msg(), _fake_msg(content="lol")]) == []


def test_recent_image_urls_skips_video_only_messages() -> None:
    vid = _fake_msg(attachments=[_fake_attachment("c.mp4", "video/mp4", 1000, "url-V")])
    img = _fake_msg(attachments=[_fake_attachment("i.png", "image/png", 1000, "url-I")])
    out = recent_image_urls([vid, img], limit=5)
    assert out == ["url-I"]


# ---- hot_urls --------------------------------------------------------------------


def test_hot_urls_extracts_from_message_content() -> None:
    msg = _fake_msg(content="check this https://x.com/foo/123 fire", display_name="gaza")
    out = hot_urls([msg])
    assert out == [("https://x.com/foo/123", 0, "gaza", "X/Twitter")]


def test_hot_urls_ranks_by_reaction_count() -> None:
    silent = _fake_msg(content="https://a.com", display_name="alice")
    hyped = _fake_msg(
        content="https://b.com", display_name="bob", reactions=[_fake_reaction(15)],
    )
    out = hot_urls([silent, hyped])
    assert out[0] == ("https://b.com", 15, "bob", "web")
    assert out[1] == ("https://a.com", 0, "alice", "web")


def test_hot_urls_strips_trailing_punctuation() -> None:
    """URLs at end of sentences shouldn't carry the period/comma along."""
    msg = _fake_msg(content="big take here: https://x.com/foo, agree?")
    out = hot_urls([msg])
    assert out[0][0] == "https://x.com/foo"


def test_hot_urls_dedups_repeated_urls() -> None:
    msg1 = _fake_msg(content="https://same.com")
    msg2 = _fake_msg(content="https://same.com again")
    out = hot_urls([msg1, msg2])
    assert len(out) == 1


def test_hot_urls_respects_limit() -> None:
    msgs: list[discord.Message] = [
        _fake_msg(content=f"https://site-{i}.com") for i in range(20)
    ]
    out = hot_urls(msgs, limit=5)
    assert len(out) == 5


def test_hot_urls_ignores_messages_with_no_urls() -> None:
    msg1 = _fake_msg(content="just a comment")
    msg2 = _fake_msg(content="real link https://hot.com")
    out = hot_urls([msg1, msg2])
    assert len(out) == 1
    assert out[0][0] == "https://hot.com"


# ---- _classify_url ---------------------------------------------------------------


def test_classify_url_tiktok_canonical_and_fixers() -> None:
    """tnktok / vxtiktok / vm.tiktok variants all classify as TikTok."""
    assert _classify_url("https://tiktok.com/@user/video/123") == "TikTok"
    assert _classify_url("https://tnktok.com/t/ZP8pgmfD6/") == "TikTok"
    assert _classify_url("https://vxtiktok.com/@user/video/123") == "TikTok"
    assert _classify_url("https://vm.tiktok.com/abc") == "TikTok"


def test_classify_url_twitter_x_and_fixers() -> None:
    """Twitter, x.com, fxtwitter, vxtwitter, fixupx all classify as X/Twitter."""
    assert _classify_url("https://twitter.com/foo/status/1") == "X/Twitter"
    assert _classify_url("https://x.com/foo/status/1") == "X/Twitter"
    assert _classify_url("https://fxtwitter.com/foo/status/1") == "X/Twitter"
    assert _classify_url("https://vxtwitter.com/foo/status/1") == "X/Twitter"
    assert _classify_url("https://fixupx.com/foo/status/1") == "X/Twitter"


def test_classify_url_instagram_and_fixers() -> None:
    assert _classify_url("https://instagram.com/p/abc") == "Instagram"
    assert _classify_url("https://ddinstagram.com/p/abc") == "Instagram"


def test_classify_url_other_known_sources() -> None:
    assert _classify_url("https://bsky.app/profile/x") == "Bluesky"
    assert _classify_url("https://youtube.com/watch?v=abc") == "YouTube"
    assert _classify_url("https://youtu.be/abc") == "YouTube"
    assert _classify_url("https://reddit.com/r/foo/") == "Reddit"
    assert _classify_url("https://open.spotify.com/track/abc") == "Spotify"
    assert _classify_url("https://twitch.tv/streamer") == "Twitch"
    assert _classify_url("https://tenor.com/view/funny-gif") == "Tenor"


def test_classify_url_unknown_falls_back_to_web() -> None:
    assert _classify_url("https://some-random-blog.com/post/123") == "web"
    assert _classify_url("https://github.com/foo/bar") == "web"


def test_classify_url_case_insensitive() -> None:
    assert _classify_url("https://TikTok.com/x") == "TikTok"
    assert _classify_url("https://YOUTUBE.com/x") == "YouTube"


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


def test_format_for_prompt_strips_html_tags_from_message_content() -> None:
    """Claude cite tags that leak into Discord history must not re-enter prompts."""
    msg = _fake_msg(
        content='Thunder lead 2-1. <cite index="1-1">Game 4 tips off at 8 p.m.</cite>',
        display_name="Tootsie's",
    )
    rendered = format_for_prompt([msg])
    assert "<cite" not in rendered
    assert "</cite>" not in rendered
    assert "Game 4 tips off at 8 p.m." in rendered


def test_format_for_prompt_decodes_html_entities_in_message_content() -> None:
    msg = _fake_msg(content="it&#39;s &amp; that&#39;s &lt;ok&gt;", display_name="user")
    rendered = format_for_prompt([msg])
    assert "it's & that's" in rendered
    assert "&amp;" not in rendered
    assert "&#39;" not in rendered


def test_extract_media_strips_html_from_embed_title_and_description() -> None:
    embed = _fake_embed(
        title='<b>Lakers Win</b>',
        description='Score: &lt;105&gt;-110 OT <em>overtime thriller</em>',
        url="https://x.com/a/b",
    )
    refs = extract_media(_fake_msg(embeds=[embed]))
    embed_ref = next(r for r in refs if r.kind == "embed")
    assert "<b>" not in embed_ref.label
    assert "</b>" not in embed_ref.label
    assert "<em>" not in embed_ref.label
    assert "Lakers Win" in embed_ref.label
    assert "<105>" in embed_ref.label  # entity decoded


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


# ---- resolve_reactors ------------------------------------------------------------


@pytest.mark.asyncio
async def test_resolve_reactors_names_who_reacted_per_emoji() -> None:
    """Maps message id -> "emoji name, name; emoji name" reactor summary."""
    msg = _fake_msg(
        content="hot take",
        msg_id=42,
        reactions=[
            _fake_reaction_with_users("🔥", ["alice", "bob"]),
            _fake_reaction_with_users("👀", ["carol"]),
        ],
    )
    out = await resolve_reactors([msg])
    assert out == {42: "🔥 alice, bob; 👀 carol"}


@pytest.mark.asyncio
async def test_resolve_reactors_custom_emoji_renders_as_colon_name() -> None:
    """Custom guild emoji (Emoji/PartialEmoji) collapse to :name: not <:name:id>."""
    custom = SimpleNamespace(name="pepehands", id=999)
    msg = _fake_msg(msg_id=7, reactions=[_fake_reaction_with_users(custom, ["dana"])])
    out = await resolve_reactors([msg])
    assert out == {7: ":pepehands: dana"}


@pytest.mark.asyncio
async def test_resolve_reactors_summarizes_overflow_with_plus_more() -> None:
    """When the count exceeds the names we page in, show '+N more'."""
    msg = _fake_msg(
        msg_id=1,
        reactions=[_fake_reaction_with_users("🔥", ["alice", "bob"], count=10)],
    )
    out = await resolve_reactors([msg], max_users_per_emoji=2)
    assert out == {1: "🔥 alice, bob +8 more"}


@pytest.mark.asyncio
async def test_resolve_reactors_bounds_to_top_reacted_messages() -> None:
    """Only the top `max_messages` by total reaction count get the API lookup."""
    big = _fake_msg(msg_id=1, reactions=[_fake_reaction_with_users("🔥", ["a", "b", "c"])])
    small = _fake_msg(msg_id=2, reactions=[_fake_reaction_with_users("👍", ["d"])])
    out = await resolve_reactors([small, big], max_messages=1)
    assert set(out) == {1}


@pytest.mark.asyncio
async def test_resolve_reactors_skips_messages_with_no_reactions() -> None:
    plain = _fake_msg(msg_id=5)
    out = await resolve_reactors([plain])
    assert out == {}


@pytest.mark.asyncio
async def test_resolve_reactors_falls_back_silently_on_api_error() -> None:
    """A users() API failure on one message drops that message, never raises."""

    async def boom(limit: int | None = None):
        raise discord.HTTPException(MagicMock(), "rate limited")
        yield  # pragma: no cover - makes this an async generator

    bad_rxn = SimpleNamespace(emoji="🔥", count=3)
    bad_rxn.users = boom
    msg = _fake_msg(msg_id=9, reactions=[bad_rxn])
    out = await resolve_reactors([msg])
    assert out == {}


def test_format_for_prompt_renders_reactor_names_when_provided() -> None:
    """reactors mapping wins over the bare count: names WHO reacted with WHAT."""
    msg = _fake_msg(content="big take", display_name="jordan", msg_id=100)
    rendered = format_for_prompt(
        [msg], include_reactions=True, reactors={100: "🔥 alice, bob"},
    )
    assert "[reactions: 🔥 alice, bob]" in rendered
    assert "jordan: big take" in rendered


def test_format_for_prompt_reactor_mapping_falls_back_to_count() -> None:
    """A message absent from the reactors map still shows the aggregate count."""
    named = _fake_msg(content="hot", msg_id=1, reactions=[_fake_reaction(2)])
    plain = _fake_msg(content="meh", msg_id=2, reactions=[_fake_reaction(5)])
    rendered = format_for_prompt(
        [named, plain], include_reactions=True, reactors={1: "🔥 alice, bob"},
    )
    assert "[reactions: 🔥 alice, bob]" in rendered
    assert "[5 reactions]" in rendered
