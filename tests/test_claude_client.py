"""Unit tests for claude_client, request shape, time context, prompt assembly."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from claude_client import HAIKU, SONNET, ClaudeClient, _time_context  # noqa: F401

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


# ---- public method routing --------------------------------------------------------


@pytest.mark.asyncio
async def test_ask_uses_haiku_with_web_search_when_use_web() -> None:
    client = ClaudeClient(api_key="test")
    fake = AsyncMock(return_value=MagicMock(text="answer"))
    with patch.object(client, "_call", fake):
        await client.ask("q", channel_context="chatter", use_web=True)
    kwargs = fake.call_args.kwargs
    assert kwargs["model"] == HAIKU
    assert kwargs["purpose"] == "ask"
    assert kwargs["tools"] is not None
    assert "chatter" in kwargs["user_message"]


@pytest.mark.asyncio
async def test_ask_passes_image_urls_through() -> None:
    client = ClaudeClient(api_key="test")
    fake = AsyncMock(return_value=MagicMock(text="answer"))
    with patch.object(client, "_call", fake):
        await client.ask("q", image_urls=["https://cdn/img.png"])
    assert fake.call_args.kwargs["image_urls"] == ["https://cdn/img.png"]


@pytest.mark.asyncio
async def test_recap_uses_haiku_with_web_search() -> None:
    client = ClaudeClient(api_key="test")
    fake = AsyncMock(return_value=MagicMock(text="recap"))
    with patch.object(client, "_call", fake):
        await client.recap("general", "msg blob")
    kwargs = fake.call_args.kwargs
    assert kwargs["model"] == HAIKU
    assert kwargs["purpose"] == "recap"
    assert kwargs["tools"] is not None  # web_search always available


@pytest.mark.asyncio
async def test_discourse_uses_sonnet() -> None:
    """/discourse needs more judgment so it routes to Sonnet, not Haiku."""
    client = ClaudeClient(api_key="test")
    fake = AsyncMock(return_value=MagicMock(text="post"))
    with patch.object(client, "_call", fake):
        await client.discourse("hiphop", "sources blob")
    assert fake.call_args.kwargs["model"] == SONNET


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
async def test_mood_post_uses_haiku() -> None:
    client = ClaudeClient(api_key="test")
    fake = AsyncMock(return_value=MagicMock(text="ambient"))
    with patch.object(client, "_call", fake):
        await client.mood_post()
    assert fake.call_args.kwargs["model"] == HAIKU
    assert fake.call_args.kwargs["purpose"] == "mood_scheduled"


@pytest.mark.asyncio
async def test_deflect_uses_haiku_with_low_max_tokens() -> None:
    client = ClaudeClient(api_key="test")
    fake = AsyncMock(return_value=MagicMock(text="quip"))
    with patch.object(client, "_call", fake):
        await client.deflect("rate limit hit")
    kwargs = fake.call_args.kwargs
    assert kwargs["model"] == HAIKU
    assert kwargs["max_tokens"] == 80
    assert kwargs["purpose"] == "deflect"


@pytest.mark.asyncio
async def test_preflight_uses_sonnet() -> None:
    client = ClaudeClient(api_key="test")
    fake = AsyncMock(return_value=MagicMock(text="ALLOW: ok"))
    with patch.object(client, "_call", fake):
        await client.preflight_order("add /foo")
    assert fake.call_args.kwargs["model"] == SONNET
    assert fake.call_args.kwargs["purpose"] == "order_preflight"
