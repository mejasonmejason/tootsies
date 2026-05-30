"""Unit tests for claude_client, request shape, time context, prompt assembly."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from claude_client import (  # noqa: F401
    _MAX_TOOL_ITERS,
    HAIKU,
    MAX_TOKENS_DEFLECT,
    SEARCH_MEMORY_TOOL,
    SONNET,
    ClaudeClient,
    ClaudeResult,
    _time_context,
)


def _tool_use_block(name: str, tool_id: str, tool_input: dict[str, Any]) -> Any:
    block = MagicMock()
    block.type = "tool_use"
    block.name = name
    block.id = tool_id
    block.input = tool_input
    return block

# ---- _time_context ----------------------------------------------------------------


def test_time_context_includes_utc_and_pt_and_weekday() -> None:
    ctx = _time_context()
    # Format: [ctx, current time: 2026-05-24 09:00 UTC, 2026-05-24 05:00 EDT, weekday: Sunday]
    assert "UTC" in ctx
    assert re.search(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}", ctx) is not None
    assert "weekday:" in ctx
    # Spelled-out weekday (Monday/Tuesday/...) so Claude doesn't have to compute it
    assert any(day in ctx for day in (
        "Monday", "Tuesday", "Wednesday", "Thursday",
        "Friday", "Saturday", "Sunday",
    ))


def test_time_context_is_brief() -> None:
    """Cost guard: this prefix runs on EVERY Claude call. Keep it small."""
    assert len(_time_context()) < 200


# ---- _call request shape ----------------------------------------------------------


@dataclass
class _FakeResponse:
    """Mimics the Anthropic SDK response shape that _call introspects."""
    content: list[Any]
    stop_reason: str = "end_turn"
    usage: Any = None

    def __post_init__(self) -> None:
        if self.usage is None:
            self.usage = MagicMock(input_tokens=10, output_tokens=20)


def _text_block(text: str) -> Any:
    block = MagicMock()
    block.type = "text"
    block.text = text
    return block


def _client_with_fake_anthropic(response_text: str = "ok") -> tuple[ClaudeClient, MagicMock]:
    client = ClaudeClient(api_key="test")
    fake_resp = _FakeResponse(content=[_text_block(response_text)])
    client.client.messages.create = AsyncMock(return_value=fake_resp)  # type: ignore[method-assign]
    return client, client.client.messages.create  # type: ignore[return-value]


@pytest.mark.asyncio
async def test_call_sends_text_content_without_images() -> None:
    client, create = _client_with_fake_anthropic("hi")
    result = await client._call(model=HAIKU, user_message="hello world", purpose="ask")
    assert result.text == "hi"
    kwargs = create.call_args.kwargs
    # content should be a plain string when no images are passed
    assert isinstance(kwargs["messages"][0]["content"], str)
    assert "hello world" in kwargs["messages"][0]["content"]


@pytest.mark.asyncio
async def test_call_prepends_time_context_to_user_message() -> None:
    client, create = _client_with_fake_anthropic()
    await client._call(model=HAIKU, user_message="the question", purpose="ask")
    content = create.call_args.kwargs["messages"][0]["content"]
    assert content.startswith("[ctx")
    assert "the question" in content


@pytest.mark.asyncio
async def test_call_builds_vision_blocks_when_image_urls_passed() -> None:
    client, create = _client_with_fake_anthropic()
    await client._call(
        model=HAIKU,
        user_message="describe this",
        purpose="ask",
        image_urls=["https://cdn/a.png", "https://cdn/b.png"],
    )
    content = create.call_args.kwargs["messages"][0]["content"]
    assert isinstance(content, list)
    text_blocks = [b for b in content if b["type"] == "text"]
    image_blocks = [b for b in content if b["type"] == "image"]
    assert len(text_blocks) == 1
    assert len(image_blocks) == 2
    assert image_blocks[0]["source"]["url"] == "https://cdn/a.png"
    assert image_blocks[1]["source"]["url"] == "https://cdn/b.png"


@pytest.mark.asyncio
async def test_call_caps_image_blocks_at_ten() -> None:
    """Hard cost guard inside _call. Even if caller passes 50 URLs, only 10 go through."""
    client, create = _client_with_fake_anthropic()
    urls = [f"https://cdn/img-{i}.png" for i in range(50)]
    await client._call(
        model=HAIKU, user_message="x", purpose="ask", image_urls=urls,
    )
    content = create.call_args.kwargs["messages"][0]["content"]
    image_blocks = [b for b in content if b["type"] == "image"]
    assert len(image_blocks) == 10


@pytest.mark.asyncio
async def test_call_passes_tools_through_when_provided() -> None:
    client, create = _client_with_fake_anthropic()
    tools = [{"type": "web_search_20250305", "name": "web_search"}]
    await client._call(model=HAIKU, user_message="x", tools=tools, purpose="ask")
    assert create.call_args.kwargs["tools"] == tools


@pytest.mark.asyncio
async def test_call_attaches_system_prompt_with_cache_control() -> None:
    """The persona + constitution gets cache_control so repeat calls hit the cache."""
    client, create = _client_with_fake_anthropic()
    await client._call(model=HAIKU, user_message="x", purpose="ask")
    system = create.call_args.kwargs["system"]
    assert isinstance(system, list)
    assert system[0].get("cache_control") == {"type": "ephemeral"}
    # The actual persona text should be in there
    assert "Toots" in system[0]["text"]


@pytest.mark.asyncio
async def test_call_emits_claude_api_event_on_success(
    caplog: pytest.LogCaptureFixture,
) -> None:
    import logging
    caplog.set_level(logging.INFO, logger="tootsies.events")
    client, _ = _client_with_fake_anthropic()
    await client._call(model=HAIKU, user_message="x", purpose="ask")
    events = [r.getMessage() for r in caplog.records if r.name == "tootsies.events"]
    assert any("claude_api" in m and "\"purpose\":\"ask\"" in m for m in events)


@pytest.mark.asyncio
async def test_call_emits_claude_api_event_on_failure_and_reraises(
    caplog: pytest.LogCaptureFixture,
) -> None:
    import logging
    caplog.set_level(logging.INFO, logger="tootsies.events")
    client = ClaudeClient(api_key="test")
    client.client.messages.create = AsyncMock(side_effect=RuntimeError("api down"))  # type: ignore[method-assign]
    with pytest.raises(RuntimeError, match="api down"):
        await client._call(model=HAIKU, user_message="x", purpose="ask")
    events = [r.getMessage() for r in caplog.records if r.name == "tootsies.events"]
    assert any("claude_api" in m and "\"ok\":false" in m for m in events)


# ---- adaptive thinking ------------------------------------------------------------


@pytest.mark.asyncio
async def test_call_omits_thinking_by_default() -> None:
    client, create = _client_with_fake_anthropic()
    await client._call(model=SONNET, user_message="x", purpose="ask")
    kwargs = create.call_args.kwargs
    assert "thinking" not in kwargs
    assert "output_config" not in kwargs


@pytest.mark.asyncio
async def test_call_passes_adaptive_thinking_when_enabled() -> None:
    """Adaptive thinking + medium effort is the leak fix from the music-post bug."""
    client, create = _client_with_fake_anthropic()
    await client._call(
        model=SONNET, user_message="x", purpose="music_post", thinking_enabled=True,
    )
    kwargs = create.call_args.kwargs
    assert kwargs["thinking"] == {"type": "adaptive"}
    assert kwargs["output_config"] == {"effort": "medium"}


@pytest.mark.asyncio
async def test_call_floors_max_tokens_at_4096_when_thinking_enabled() -> None:
    """Thinking tokens count toward max_tokens; tweet-length caps (150/400) starve it."""
    client, create = _client_with_fake_anthropic()
    await client._call(
        model=SONNET, user_message="x", purpose="post",
        max_tokens=150, thinking_enabled=True,
    )
    assert create.call_args.kwargs["max_tokens"] == 4096


@pytest.mark.asyncio
async def test_call_preserves_caller_max_tokens_when_already_above_floor() -> None:
    client, create = _client_with_fake_anthropic()
    await client._call(
        model=SONNET, user_message="x", purpose="ask",
        max_tokens=8192, thinking_enabled=True,
    )
    assert create.call_args.kwargs["max_tokens"] == 8192


# ---- public method routing --------------------------------------------------------


@pytest.mark.asyncio
async def test_ask_uses_sonnet_with_web_search_when_use_web() -> None:
    """Per the model-routing rule: anything user-facing runs on Sonnet."""
    client = ClaudeClient(api_key="test")
    fake = AsyncMock(return_value=MagicMock(text="answer"))
    with patch.object(client, "_call", fake):
        await client.ask("q", channel_context="chatter", use_web=True)
    kwargs = fake.call_args.kwargs
    assert kwargs["model"] == SONNET
    assert kwargs["purpose"] == "ask"
    assert kwargs["tools"] is not None
    assert "chatter" in kwargs["user_message"]


@pytest.mark.asyncio
async def test_ask_system_prompt_tells_model_to_prefer_verified_values() -> None:
    """The ask prompt must explicitly tell Claude that specific numbers /
    dates / counts in REAL-TIME SEARCH CONTEXT, MARKET CONTEXT, or enriched
    links override what training data remembers. Without this, the model
    will keep answering 'Drake has 13 #1s' when Perplexity returned 14."""
    client = ClaudeClient(api_key="test")
    fake = AsyncMock(return_value=MagicMock(text="answer"))
    with patch.object(client, "_call", fake):
        await client.ask("q")
    system_extra = fake.call_args.kwargs["system_extra"].lower()
    # The "override / wins / verified beats memory" intent, in any of the
    # phrasings the prompt could land on.
    assert "verified" in system_extra
    assert "stale" in system_extra or "override" in system_extra or "wins" in system_extra


@pytest.mark.asyncio
async def test_ask_passes_image_urls_through() -> None:
    client = ClaudeClient(api_key="test")
    fake = AsyncMock(return_value=MagicMock(text="answer"))
    with patch.object(client, "_call", fake):
        await client.ask("q", image_urls=["https://cdn/img.png"])
    assert fake.call_args.kwargs["image_urls"] == ["https://cdn/img.png"]


@pytest.mark.asyncio
async def test_ask_strips_hallucinated_urls() -> None:
    """Same guardrail shape as /discourse: invented URLs get stripped."""
    from claude_client import ClaudeResult
    from utils.link_enrich import EnrichedLink

    client = ClaudeClient(api_key="test")
    result = ClaudeResult(
        text="nah, dude's been done four times.\nhttps://hallucinated.example/x",
        stop_reason="end_turn", input_tokens=10, output_tokens=20,
        web_search_urls=[],
    )
    real_link = EnrichedLink(platform="twitter", url="https://real.example/a")
    fake = AsyncMock(return_value=result)
    with patch.object(client, "_call", fake):
        out = await client.ask("is drake done", enriched_links=[real_link])
    assert "hallucinated" not in out
    assert out == "nah, dude's been done four times."


@pytest.mark.asyncio
async def test_ask_keeps_url_from_enriched_links() -> None:
    from claude_client import ClaudeResult
    from utils.link_enrich import EnrichedLink

    client = ClaudeClient(api_key="test")
    result = ClaudeResult(
        text="that's the one.\nhttps://real.example/a",
        stop_reason="end_turn", input_tokens=10, output_tokens=20,
        web_search_urls=[],
    )
    real_link = EnrichedLink(platform="twitter", url="https://real.example/a")
    fake = AsyncMock(return_value=result)
    with patch.object(client, "_call", fake):
        out = await client.ask("what's this", enriched_links=[real_link])
    assert "https://real.example/a" in out


@pytest.mark.asyncio
async def test_recap_uses_sonnet_with_web_search() -> None:
    client = ClaudeClient(api_key="test")
    fake = AsyncMock(return_value=MagicMock(text="recap"))
    with patch.object(client, "_call", fake):
        await client.recap("general", "msg blob")
    kwargs = fake.call_args.kwargs
    assert kwargs["model"] == SONNET
    assert kwargs["purpose"] == "recap"
    assert kwargs["tools"] is not None  # web_search always available


@pytest.mark.asyncio
@pytest.mark.asyncio
async def test_recap_strips_hallucinated_urls() -> None:
    """Recap goes to Discord like ask/discourse, so it gets the same guardrail."""
    from claude_client import ClaudeResult

    client = ClaudeClient(api_key="test")
    result = ClaudeResult(
        text="room cooked.\nhttps://hallucinated.example/x",
        stop_reason="end_turn", input_tokens=10, output_tokens=20,
        web_search_urls=[],
    )
    fake = AsyncMock(return_value=result)
    with patch.object(client, "_call", fake):
        out = await client.recap(
            "general", "msg blob",
            hot_urls=[("https://real.example/a", 5, "alice", "twitter")],
        )
    assert "hallucinated" not in out
    assert out == "room cooked."


@pytest.mark.asyncio
async def test_recap_keeps_hot_url_when_model_quotes_it() -> None:
    """A URL the room actually posted should survive the recap guardrail."""
    from claude_client import ClaudeResult

    client = ClaudeClient(api_key="test")
    result = ClaudeResult(
        text="that drop's gonna age.\nhttps://real.example/a",
        stop_reason="end_turn", input_tokens=10, output_tokens=20,
        web_search_urls=[],
    )
    fake = AsyncMock(return_value=result)
    with patch.object(client, "_call", fake):
        out = await client.recap(
            "general", "msg blob",
            hot_urls=[("https://real.example/a", 5, "alice", "twitter")],
        )
    assert "https://real.example/a" in out


@pytest.mark.asyncio
async def test_ask_enables_thinking_when_use_web_is_true() -> None:
    """With web_search on, inter-tool-call reasoning must go in thinking blocks
    (not text blocks) so it can't leak into the user-visible reply."""
    client = ClaudeClient(api_key="test")
    fake = AsyncMock(return_value=MagicMock(text="answer"))
    with patch.object(client, "_call", fake):
        await client.ask("q", use_web=True)
    assert fake.call_args.kwargs["thinking_enabled"] is True


@pytest.mark.asyncio
async def test_ask_omits_thinking_when_no_web_search() -> None:
    """No tools = no inter-tool narration to leak. Don't pay for thinking tokens."""
    client = ClaudeClient(api_key="test")
    fake = AsyncMock(return_value=MagicMock(text="answer"))
    with patch.object(client, "_call", fake):
        await client.ask("q", use_web=False)
    assert fake.call_args.kwargs["thinking_enabled"] is False


@pytest.mark.asyncio
async def test_recap_does_not_enable_thinking() -> None:
    """Recap stays on the cheap path: opted out of thinking by design."""
    client = ClaudeClient(api_key="test")
    fake = AsyncMock(return_value=MagicMock(text="recap"))
    with patch.object(client, "_call", fake):
        await client.recap("general", "msg blob")
    assert fake.call_args.kwargs.get("thinking_enabled", False) is False


@pytest.mark.asyncio
async def test_discourse_uses_sonnet() -> None:
    """/discourse needs more judgment so it routes to Sonnet, not Haiku."""
    client = ClaudeClient(api_key="test")
    fake = AsyncMock(return_value=MagicMock(text="post"))
    with patch.object(client, "_call", fake):
        await client.discourse("hiphop", "sources blob")
    assert fake.call_args.kwargs["model"] == SONNET


@pytest.mark.asyncio
async def test_discourse_channel_topic_steers_prompt() -> None:
    """A channel description (topic) becomes a hard theme constraint in the prompt."""
    client = ClaudeClient(api_key="test")
    fake = AsyncMock(return_value=MagicMock(text="post"))
    with patch.object(client, "_call", fake):
        await client.discourse(
            None, "sources blob", channel_topic="movies, tv, and film talk only",
        )
    system_extra = fake.call_args.kwargs["system_extra"]
    user_msg = fake.call_args.kwargs["user_message"]
    assert "CHANNEL THEME" in system_extra
    assert "movies, tv, and film talk only" in system_extra
    assert "movies, tv, and film talk only" in user_msg


@pytest.mark.asyncio
async def test_discourse_explicit_category_suppresses_channel_theme() -> None:
    """An explicit /discourse category: wins; the channel theme block stays out."""
    client = ClaudeClient(api_key="test")
    fake = AsyncMock(return_value=MagicMock(text="post"))
    with patch.object(client, "_call", fake):
        await client.discourse(
            "hiphop", "sources blob", channel_topic="movies, tv, and film talk only",
        )
    system_extra = fake.call_args.kwargs["system_extra"]
    assert "CHANNEL THEME" not in system_extra


@pytest.mark.asyncio
async def test_discourse_enables_thinking() -> None:
    client = ClaudeClient(api_key="test")
    fake = AsyncMock(return_value=MagicMock(text="post"))
    with patch.object(client, "_call", fake):
        await client.discourse("hiphop", "sources blob")
    assert fake.call_args.kwargs["thinking_enabled"] is True


@pytest.mark.asyncio
async def test_music_post_enables_thinking() -> None:
    client = ClaudeClient(api_key="test")
    fake = AsyncMock(return_value=MagicMock(text="post"))
    with patch.object(client, "_call", fake):
        await client.music_post("sources blob")
    assert fake.call_args.kwargs["thinking_enabled"] is True


@pytest.mark.asyncio
async def test_chimein_post_enables_thinking() -> None:
    client = ClaudeClient(api_key="test")
    fake = AsyncMock(return_value=MagicMock(text="reply"))
    with patch.object(client, "_call", fake):
        await client.chimein_post("buffer blob", hook="lakers convo")
    assert fake.call_args.kwargs["thinking_enabled"] is True


@pytest.mark.asyncio
async def test_discourse_purpose_reflects_manual_vs_scheduled() -> None:
    client = ClaudeClient(api_key="test")
    fake = AsyncMock(return_value=MagicMock(text="post"))
    with patch.object(client, "_call", fake):
        await client.discourse("hiphop", "blob", must_post=True)
        assert fake.call_args.kwargs["purpose"] == "discourse_manual"
        await client.discourse("hiphop", "blob", must_post=False)
        assert fake.call_args.kwargs["purpose"] == "discourse_scheduled"


@pytest.mark.asyncio
async def test_discourse_accepts_none_category() -> None:
    """When category is None (read the room), the user message should say 'Read the room.'"""
    client = ClaudeClient(api_key="test")
    fake = AsyncMock(return_value=MagicMock(text="post"))
    with patch.object(client, "_call", fake):
        await client.discourse(None, "sources blob")
    user_msg = fake.call_args.kwargs["user_message"]
    assert "Read the room" in user_msg
    assert "Category:" not in user_msg


@pytest.mark.asyncio
async def test_discourse_with_category_passes_it() -> None:
    client = ClaudeClient(api_key="test")
    fake = AsyncMock(return_value=MagicMock(text="post"))
    with patch.object(client, "_call", fake):
        await client.discourse("sports", "sources blob")
    user_msg = fake.call_args.kwargs["user_message"]
    assert "Category: sports" in user_msg


@pytest.mark.asyncio
async def test_discourse_strips_hallucinated_urls() -> None:
    """Any URL in the output that isn't in feed/Perplexity/web_search sources gets stripped."""
    from claude_client import ClaudeResult

    client = ClaudeClient(api_key="test")
    result = ClaudeResult(
        text="hot take.\nhttps://hallucinated.example/x",
        stop_reason="end_turn", input_tokens=10, output_tokens=20,
        web_search_urls=[],
    )
    fake = AsyncMock(return_value=result)
    with patch.object(client, "_call", fake):
        out = await client.discourse(
            "sports", "sources blob",
            hot_urls=[("https://real.example/a", 5, "alice", "twitter")],
        )
    assert "hallucinated" not in out
    assert out == "hot take."


@pytest.mark.asyncio
async def test_discourse_keeps_url_from_allowlist() -> None:
    from claude_client import ClaudeResult

    client = ClaudeClient(api_key="test")
    result = ClaudeResult(
        text="hot take.\nhttps://real.example/a",
        stop_reason="end_turn", input_tokens=10, output_tokens=20,
        web_search_urls=[],
    )
    fake = AsyncMock(return_value=result)
    with patch.object(client, "_call", fake):
        out = await client.discourse(
            "sports", "sources blob",
            hot_urls=[("https://real.example/a", 5, "alice", "twitter")],
        )
    assert "https://real.example/a" in out


@pytest.mark.asyncio
async def test_discourse_perplexity_citation_urls_allowed() -> None:
    """URLs surfaced in the Perplexity SOURCES block are part of the allowlist."""
    from claude_client import ClaudeResult

    client = ClaudeClient(api_key="test")
    result = ClaudeResult(
        text="take.\nhttps://news.example/article",
        stop_reason="end_turn", input_tokens=10, output_tokens=20,
        web_search_urls=[],
    )
    pplx_context = (
        "REAL-TIME SEARCH CONTEXT ...:\nsome content\n\n"
        "SOURCES:\n  [1] https://news.example/article\n  [2] https://other.example/b"
    )
    fake = AsyncMock(return_value=result)
    with patch.object(client, "_call", fake):
        out = await client.discourse(
            "pop", "sources blob",
            perplexity_context=pplx_context,
        )
    assert "https://news.example/article" in out


@pytest.mark.asyncio
async def test_discourse_web_search_urls_allowed() -> None:
    """URLs returned by web_search (server-side tool) are part of the allowlist."""
    from claude_client import ClaudeResult

    client = ClaudeClient(api_key="test")
    result = ClaudeResult(
        text="take.\nhttps://espn.example/score",
        stop_reason="end_turn", input_tokens=10, output_tokens=20,
        web_search_urls=["https://espn.example/score"],
    )
    fake = AsyncMock(return_value=result)
    with patch.object(client, "_call", fake):
        out = await client.discourse("nba", "sources blob")
    assert "https://espn.example/score" in out


@pytest.mark.asyncio
async def test_discourse_dedups_url_already_in_destination_channel() -> None:
    """URL already visible in the destination channel buffer gets stripped."""
    from claude_client import ClaudeResult

    client = ClaudeClient(api_key="test")
    result = ClaudeResult(
        text="huge moment.\nhttps://twitter.example/already",
        stop_reason="end_turn", input_tokens=10, output_tokens=20,
        web_search_urls=[],
    )
    fake = AsyncMock(return_value=result)
    with patch.object(client, "_call", fake):
        out = await client.discourse(
            "nba", "sources blob",
            hot_urls=[("https://twitter.example/already", 5, "alice", "twitter")],
            recently_seen_urls=["https://twitter.example/already"],
        )
    assert "twitter.example" not in out
    assert out == "huge moment."


@pytest.mark.asyncio
async def test_ask_dedups_url_from_question() -> None:
    """If the user's question contains the URL, Toots's answer doesn't repaste it."""
    from claude_client import ClaudeResult
    from utils.link_enrich import EnrichedLink

    client = ClaudeClient(api_key="test")
    result = ClaudeResult(
        text="lakers up 3-1, lebron with 35/12/9.\nhttps://twitter.example/score",
        stop_reason="end_turn", input_tokens=10, output_tokens=20,
        web_search_urls=[],
    )
    enriched = EnrichedLink(platform="twitter", url="https://twitter.example/score")
    fake = AsyncMock(return_value=result)
    with patch.object(client, "_call", fake):
        out = await client.ask(
            "whats this https://twitter.example/score",
            enriched_links=[enriched],
            recently_seen_urls=["https://twitter.example/score"],
        )
    assert "twitter.example" not in out
    assert "lakers up 3-1" in out


@pytest.mark.asyncio
async def test_deflect_uses_haiku_with_low_max_tokens() -> None:
    """Deflect is the exception: one-liner canned-ish quip, no judgment,
    60-token cap, runs as a fast fallback. Sonnet is overkill here."""
    client = ClaudeClient(api_key="test")
    fake = AsyncMock(return_value=MagicMock(text="quip"))
    with patch.object(client, "_call", fake):
        await client.deflect("rate limit hit")
    kwargs = fake.call_args.kwargs
    assert kwargs["model"] == HAIKU
    assert kwargs["max_tokens"] == MAX_TOKENS_DEFLECT
    assert kwargs["purpose"] == "deflect"


@pytest.mark.asyncio
async def test_classify_abuse_uses_haiku_with_tiny_budget() -> None:
    """Abuse classifier is a binary judge: Haiku with a 4-token cap."""
    client = ClaudeClient(api_key="test")
    fake = AsyncMock(return_value=MagicMock(text="ABUSE"))
    with patch.object(client, "_call", fake):
        out = await client.classify_abuse("kill yourself bot")
    assert out is True
    kwargs = fake.call_args.kwargs
    assert kwargs["model"] == HAIKU
    assert kwargs["max_tokens"] == 4
    assert kwargs["purpose"] == "classify_abuse"


@pytest.mark.asyncio
async def test_classify_abuse_returns_false_on_ok_label() -> None:
    client = ClaudeClient(api_key="test")
    fake = AsyncMock(return_value=MagicMock(text="OK"))
    with patch.object(client, "_call", fake):
        assert await client.classify_abuse("you're dumb bot") is False


@pytest.mark.asyncio
async def test_classify_abuse_fails_open_on_exception() -> None:
    """A Haiku outage can't accidentally silence users (fail-open returns False)."""
    client = ClaudeClient(api_key="test")
    fake = AsyncMock(side_effect=RuntimeError("haiku down"))
    with patch.object(client, "_call", fake):
        assert await client.classify_abuse("anything") is False


@pytest.mark.asyncio
async def test_classify_abuse_empty_returns_false_without_api_call() -> None:
    """Don't burn a Haiku call on empty/whitespace text."""
    client = ClaudeClient(api_key="test")
    fake = AsyncMock()
    with patch.object(client, "_call", fake):
        assert await client.classify_abuse("") is False
        assert await client.classify_abuse("   ") is False
    fake.assert_not_called()


@pytest.mark.asyncio
async def test_preflight_uses_sonnet() -> None:
    client = ClaudeClient(api_key="test")
    fake = AsyncMock(return_value=MagicMock(text="ALLOW: ok"))
    with patch.object(client, "_call", fake):
        await client.preflight_order("add /foo")
    assert fake.call_args.kwargs["model"] == SONNET
    assert fake.call_args.kwargs["purpose"] == "order_preflight"


# ---- classify_market_intent ----------------------------------------------------


from claude_client import _parse_market_intent  # noqa: E402


def test_parse_market_intent_sports():
    out = _parse_market_intent(
        '{"intent": "sports", "league": "nba", "search_terms": "OKC Spurs"}'
    )
    assert out is not None
    assert out["intent"] == "sports"
    assert out["league"] == "NBA"
    assert out["search_terms"] == "OKC Spurs"


def test_parse_market_intent_prediction_market():
    out = _parse_market_intent(
        '{"intent": "prediction_market", "league": null, "search_terms": "drake album"}'
    )
    assert out is not None
    assert out["intent"] == "prediction_market"
    assert "league" not in out
    assert out["search_terms"] == "drake album"


def test_parse_market_intent_none_returns_none():
    assert _parse_market_intent(
        '{"intent": "none", "league": null, "search_terms": ""}'
    ) is None


def test_parse_market_intent_with_code_fences():
    out = _parse_market_intent(
        '```json\n{"intent": "sports", "league": "NFL", "search_terms": "chiefs"}\n```'
    )
    assert out is not None
    assert out["league"] == "NFL"


def test_parse_market_intent_unparseable():
    assert _parse_market_intent("not json at all") is None
    assert _parse_market_intent("") is None
    assert _parse_market_intent('{"intent": "weird_value"}') is None


@pytest.mark.asyncio
async def test_classify_market_intent_uses_haiku():
    client = ClaudeClient(api_key="test")
    fake = AsyncMock(return_value=MagicMock(
        text='{"intent": "sports", "league": "NBA", "search_terms": "OKC Spurs"}',
    ))
    with patch.object(client, "_call", fake):
        result = await client.classify_market_intent("OKC vs Spurs tonight")
    assert result == {"intent": "sports", "league": "NBA", "search_terms": "OKC Spurs"}
    assert fake.call_args.kwargs["model"] == HAIKU
    assert fake.call_args.kwargs["purpose"] == "market_intent"


@pytest.mark.asyncio
async def test_classify_market_intent_empty_query_returns_none_without_call():
    client = ClaudeClient(api_key="test")
    fake = AsyncMock()
    with patch.object(client, "_call", fake):
        result = await client.classify_market_intent("")
    assert result is None
    fake.assert_not_called()


@pytest.mark.asyncio
async def test_classify_market_intent_api_failure_returns_none():
    client = ClaudeClient(api_key="test")
    fake = AsyncMock(side_effect=RuntimeError("api down"))
    with patch.object(client, "_call", fake):
        result = await client.classify_market_intent("anything")
    assert result is None


# ---- pick_kalshi_series --------------------------------------------------------


@pytest.mark.asyncio
async def test_pick_kalshi_series_uses_haiku():
    client = ClaudeClient(api_key="test")
    fake = AsyncMock(return_value=MagicMock(text="KXBILLBOARD"))
    candidates = [
        {"ticker": "KXBILLBOARD", "title": "Billboard Hot 100"},
        {"ticker": "KXSPOTIFYD", "title": "Daily US Spotify chart"},
    ]
    with patch.object(client, "_call", fake):
        result = await client.pick_kalshi_series("drake hot 100", candidates)
    assert result == "KXBILLBOARD"
    assert fake.call_args.kwargs["model"] == HAIKU
    assert fake.call_args.kwargs["purpose"] == "kalshi_pick"


@pytest.mark.asyncio
async def test_pick_kalshi_series_single_candidate_skips_call():
    """One candidate -> short-circuit, no Haiku call needed."""
    client = ClaudeClient(api_key="test")
    fake = AsyncMock()
    with patch.object(client, "_call", fake):
        result = await client.pick_kalshi_series(
            "anything", [{"ticker": "KXONLY", "title": "Only option"}],
        )
    assert result == "KXONLY"
    fake.assert_not_called()


@pytest.mark.asyncio
async def test_pick_kalshi_series_empty_candidates_returns_none():
    client = ClaudeClient(api_key="test")
    fake = AsyncMock()
    with patch.object(client, "_call", fake):
        result = await client.pick_kalshi_series("anything", [])
    assert result is None
    fake.assert_not_called()


@pytest.mark.asyncio
async def test_pick_kalshi_series_none_reply_returns_none():
    """Haiku says NONE -> caller falls through to Polymarket-only."""
    client = ClaudeClient(api_key="test")
    fake = AsyncMock(return_value=MagicMock(text="NONE"))
    candidates = [
        {"ticker": "KXFOO", "title": "Foo"},
        {"ticker": "KXBAR", "title": "Bar"},
    ]
    with patch.object(client, "_call", fake):
        result = await client.pick_kalshi_series("what's for dinner", candidates)
    assert result is None


@pytest.mark.asyncio
async def test_pick_kalshi_series_matches_ticker_inside_reply():
    """Haiku sometimes adds whitespace/quotes; we match the ticker substring."""
    client = ClaudeClient(api_key="test")
    fake = AsyncMock(return_value=MagicMock(text="  KXBILLBOARD\n"))
    candidates = [
        {"ticker": "KXBILLBOARD", "title": "Billboard Hot 100"},
        {"ticker": "KXSPOTIFYD", "title": "Spotify"},
    ]
    with patch.object(client, "_call", fake):
        result = await client.pick_kalshi_series("drake", candidates)
    assert result == "KXBILLBOARD"


@pytest.mark.asyncio
async def test_pick_kalshi_series_prefers_longest_match():
    """Tickers share prefixes (KXBTC is a substring of KXBTCD). When Haiku
    returns the longer/more-specific ticker, we must resolve to that one,
    not the shorter prefix that also substring-matches."""
    client = ClaudeClient(api_key="test")
    fake = AsyncMock(return_value=MagicMock(text="KXBTCD"))
    candidates = [
        {"ticker": "KXBTC", "title": "Bitcoin range"},
        {"ticker": "KXBTCD", "title": "Bitcoin above/below"},
    ]
    with patch.object(client, "_call", fake):
        result = await client.pick_kalshi_series("btc above 100k", candidates)
    assert result == "KXBTCD"


@pytest.mark.asyncio
async def test_pick_kalshi_series_unknown_reply_returns_none():
    """Haiku returns a ticker not in the candidates -> safer to skip."""
    client = ClaudeClient(api_key="test")
    fake = AsyncMock(return_value=MagicMock(text="KXNOTREAL"))
    candidates = [
        {"ticker": "KXFOO", "title": "Foo"},
        {"ticker": "KXBAR", "title": "Bar"},
    ]
    with patch.object(client, "_call", fake):
        result = await client.pick_kalshi_series("anything", candidates)
    assert result is None


@pytest.mark.asyncio
async def test_pick_kalshi_series_api_failure_returns_none():
    client = ClaudeClient(api_key="test")
    fake = AsyncMock(side_effect=RuntimeError("haiku down"))
    candidates = [
        {"ticker": "KXFOO", "title": "Foo"},
        {"ticker": "KXBAR", "title": "Bar"},
    ]
    with patch.object(client, "_call", fake):
        result = await client.pick_kalshi_series("anything", candidates)
    assert result is None


# ---- pick_kalshi_market (stage 2) ---------------------------------------------


@pytest.mark.asyncio
async def test_pick_kalshi_market_uses_haiku():
    """Stage 2 picks a specific market from the series's live events fetch."""
    client = ClaudeClient(api_key="test")
    fake = AsyncMock(return_value=MagicMock(text="KXBILLBOARD-DEC-DRAKE"))
    candidates = [
        {"ticker": "KXBILLBOARD-DEC-DRAKE", "title": "Drake #1 on Dec 8"},
        {"ticker": "KXBILLBOARD-DEC-WEEKND", "title": "Weeknd #1 on Dec 8"},
    ]
    with patch.object(client, "_call", fake):
        result = await client.pick_kalshi_market("drake hot 100", candidates)
    assert result == "KXBILLBOARD-DEC-DRAKE"
    assert fake.call_args.kwargs["model"] == HAIKU
    assert fake.call_args.kwargs["purpose"] == "kalshi_market_pick"


@pytest.mark.asyncio
async def test_pick_kalshi_market_single_candidate_short_circuits():
    client = ClaudeClient(api_key="test")
    fake = AsyncMock()
    with patch.object(client, "_call", fake):
        result = await client.pick_kalshi_market(
            "anything",
            [{"ticker": "KXONLY", "title": "Only market"}],
        )
    assert result == "KXONLY"
    fake.assert_not_called()


@pytest.mark.asyncio
async def test_pick_kalshi_market_empty_candidates_returns_none():
    client = ClaudeClient(api_key="test")
    fake = AsyncMock()
    with patch.object(client, "_call", fake):
        result = await client.pick_kalshi_market("anything", [])
    assert result is None
    fake.assert_not_called()


@pytest.mark.asyncio
async def test_pick_kalshi_market_none_means_show_whole_series():
    """When Haiku says no specific market matches, caller falls back to
    showing all markets in the series; we just return None here."""
    client = ClaudeClient(api_key="test")
    fake = AsyncMock(return_value=MagicMock(text="NONE"))
    candidates = [
        {"ticker": "KX-A", "title": "A"},
        {"ticker": "KX-B", "title": "B"},
    ]
    with patch.object(client, "_call", fake):
        result = await client.pick_kalshi_market("broad query", candidates)
    assert result is None


@pytest.mark.asyncio
async def test_pick_kalshi_market_prefers_longest_match():
    """Two market tickers under the same event share prefixes; pick the
    longest substring match in the reply."""
    client = ClaudeClient(api_key="test")
    fake = AsyncMock(return_value=MagicMock(text="KX-A-LONG-VARIANT"))
    candidates = [
        {"ticker": "KX-A", "title": "Short A"},
        {"ticker": "KX-A-LONG-VARIANT", "title": "Long variant of A"},
    ]
    with patch.object(client, "_call", fake):
        result = await client.pick_kalshi_market("variant query", candidates)
    assert result == "KX-A-LONG-VARIANT"


@pytest.mark.asyncio
async def test_pick_kalshi_market_unknown_reply_returns_none():
    client = ClaudeClient(api_key="test")
    fake = AsyncMock(return_value=MagicMock(text="KX-NOT-IN-LIST"))
    candidates = [
        {"ticker": "KX-A", "title": "A"},
        {"ticker": "KX-B", "title": "B"},
    ]
    with patch.object(client, "_call", fake):
        result = await client.pick_kalshi_market("anything", candidates)
    assert result is None


@pytest.mark.asyncio
async def test_pick_kalshi_market_api_failure_returns_none():
    client = ClaudeClient(api_key="test")
    fake = AsyncMock(side_effect=RuntimeError("haiku down"))
    candidates = [
        {"ticker": "KX-A", "title": "A"},
        {"ticker": "KX-B", "title": "B"},
    ]
    with patch.object(client, "_call", fake):
        result = await client.pick_kalshi_market("anything", candidates)
    assert result is None


# ---- _parse_discourse_score -------------------------------------------------------

from claude_client import _parse_discourse_score  # noqa: E402


def test_parse_discourse_score_happy_path():
    score, reason = _parse_discourse_score('{"score": 0.72, "reason": "has a take"}')
    assert score == pytest.approx(0.72)
    assert reason == "has a take"


def test_parse_discourse_score_strips_code_fence():
    score, reason = _parse_discourse_score(
        '```json\n{"score": 0.85, "reason": "spicy"}\n```'
    )
    assert score == pytest.approx(0.85)
    assert reason == "spicy"


def test_parse_discourse_score_with_preamble():
    score, reason = _parse_discourse_score(
        'Here is my assessment: {"score": 0.5, "reason": "bland"}'
    )
    assert score == pytest.approx(0.5)
    assert reason == "bland"


def test_parse_discourse_score_clamps_out_of_range():
    score, _ = _parse_discourse_score('{"score": 1.5, "reason": "off"}')
    assert score == pytest.approx(1.0)
    score2, _ = _parse_discourse_score('{"score": -0.3, "reason": "off"}')
    assert score2 == pytest.approx(0.0)


def test_parse_discourse_score_empty_returns_zero():
    assert _parse_discourse_score("") == (0.0, "")


def test_parse_discourse_score_no_json_returns_zero():
    assert _parse_discourse_score("no json here") == (0.0, "")


def test_parse_discourse_score_malformed_json_returns_zero():
    assert _parse_discourse_score("{bad json}") == (0.0, "")


def test_parse_discourse_score_missing_score_returns_zero():
    assert _parse_discourse_score('{"reason": "no score field"}') == (0.0, "")


def test_parse_discourse_score_missing_reason_defaults_empty():
    score, reason = _parse_discourse_score('{"score": 0.7}')
    assert score == pytest.approx(0.7)
    assert reason == ""


@pytest.mark.asyncio
async def test_discourse_score_uses_haiku():
    client = ClaudeClient(api_key="test")
    fake = AsyncMock(return_value=MagicMock(
        text='{"score": 0.8, "reason": "strong take"}',
    ))
    with patch.object(client, "_call", fake):
        score, reason = await client.discourse_score("some post text")
    assert score == pytest.approx(0.8)
    assert reason == "strong take"
    assert fake.call_args.kwargs["model"] == HAIKU
    assert fake.call_args.kwargs["purpose"] == "discourse_score"


# ---- client-side tool loop (search_memory) ----------------------------------


@pytest.mark.asyncio
async def test_call_runs_client_tool_loop():
    """Model emits a search_memory tool_use; _call runs the handler, feeds the
    result back, and returns the model's follow-up text."""
    client = ClaudeClient(api_key="test")
    first = _FakeResponse(
        content=[_tool_use_block("search_memory", "tu_1", {"query": "drake"})],
        stop_reason="tool_use",
    )
    second = _FakeResponse(
        content=[_text_block("you call every drake album a classic")],
        stop_reason="end_turn",
    )
    client.client.messages.create = AsyncMock(side_effect=[first, second])  # type: ignore[method-assign]

    seen: list[dict[str, Any]] = []

    async def handler(inp: dict[str, Any]) -> str:
        seen.append(inp)
        return "note: alex stans drake"

    result = await client._call(
        model=SONNET,
        user_message="what do i think of drake",
        tools=[SEARCH_MEMORY_TOOL],
        tool_handlers={"search_memory": handler},
        purpose="ask",
    )

    assert result.text == "you call every drake album a classic"
    assert seen == [{"query": "drake"}]
    assert client.client.messages.create.call_count == 2
    # The continuation call carries the assistant tool_use turn + our tool_result.
    second_messages = client.client.messages.create.call_args_list[1].kwargs["messages"]
    assert second_messages[-2]["role"] == "assistant"
    tr = second_messages[-1]
    assert tr["role"] == "user"
    assert tr["content"][0]["type"] == "tool_result"
    assert tr["content"][0]["tool_use_id"] == "tu_1"
    assert "alex stans drake" in tr["content"][0]["content"]
    # Token usage accumulates across both turns.
    assert result.input_tokens == 20  # 10 per turn * 2


@pytest.mark.asyncio
async def test_call_tool_loop_is_bounded():
    """A model that keeps asking to search is cut off after _MAX_TOOL_ITERS;
    we take whatever text the last turn carried instead of looping forever."""
    client = ClaudeClient(api_key="test")

    def always_searches() -> Any:
        return _FakeResponse(
            content=[
                _tool_use_block("search_memory", "x", {"query": "q"}),
                _text_block("partial answer"),
            ],
            stop_reason="tool_use",
        )

    client.client.messages.create = AsyncMock(  # type: ignore[method-assign]
        side_effect=[always_searches() for _ in range(_MAX_TOOL_ITERS + 5)]
    )

    async def handler(inp: dict[str, Any]) -> str:
        return "hit"

    result = await client._call(
        model=SONNET,
        user_message="q",
        tools=[SEARCH_MEMORY_TOOL],
        tool_handlers={"search_memory": handler},
        purpose="ask",
    )
    assert client.client.messages.create.call_count == _MAX_TOOL_ITERS + 1
    assert result.text == "partial answer"


@pytest.mark.asyncio
async def test_call_tool_handler_failure_is_soft():
    """A throwing handler must not crash the loop; the model gets a soft result."""
    client = ClaudeClient(api_key="test")
    first = _FakeResponse(
        content=[_tool_use_block("search_memory", "tu_1", {"query": "x"})],
        stop_reason="tool_use",
    )
    second = _FakeResponse(content=[_text_block("ok then")], stop_reason="end_turn")
    client.client.messages.create = AsyncMock(side_effect=[first, second])  # type: ignore[method-assign]

    async def handler(inp: dict[str, Any]) -> str:
        raise RuntimeError("db down")

    result = await client._call(
        model=SONNET, user_message="x",
        tools=[SEARCH_MEMORY_TOOL], tool_handlers={"search_memory": handler},
        purpose="ask",
    )
    assert result.text == "ok then"
    tr = client.client.messages.create.call_args_list[1].kwargs["messages"][-1]
    assert "failed" in tr["content"][0]["content"]


@pytest.mark.asyncio
async def test_ask_offers_search_memory_when_handler_present():
    client = ClaudeClient(api_key="test")
    captured: dict[str, Any] = {}

    async def fake_call(**kwargs: Any) -> ClaudeResult:
        captured.update(kwargs)
        return ClaudeResult(text="ans", stop_reason="end_turn", input_tokens=1, output_tokens=1)

    async def mem(q: str) -> str:
        return "hit"

    with patch.object(client, "_call", fake_call):
        await client.ask("what's up", use_web=True, memory_search=mem)

    tool_names = {t.get("name") for t in captured["tools"]}
    assert "search_memory" in tool_names
    assert "search_memory" in captured["tool_handlers"]
    assert "SEARCH_MEMORY TOOL" in captured["system_extra"]


@pytest.mark.asyncio
async def test_ask_no_search_memory_without_handler():
    client = ClaudeClient(api_key="test")
    captured: dict[str, Any] = {}

    async def fake_call(**kwargs: Any) -> ClaudeResult:
        captured.update(kwargs)
        return ClaudeResult(text="ans", stop_reason="end_turn", input_tokens=1, output_tokens=1)

    with patch.object(client, "_call", fake_call):
        await client.ask("what's up", use_web=True)

    assert captured["tool_handlers"] is None
    assert all(t.get("name") != "search_memory" for t in (captured.get("tools") or []))
    assert "SEARCH_MEMORY TOOL" not in captured["system_extra"]
