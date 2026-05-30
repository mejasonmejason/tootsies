"""/ask factual-grounding classifier + forced web_search.

Left to itself the model web_searched only ~7% of /ask calls and answered from
stale memory. classify_ask_grounding decides whether a question is factual; if
so, /ask FORCES a web_search (thinking off, since forced tool_choice can't run
with adaptive thinking). Banter / opinion / self / abstract / math skip the
force and keep thinking + memory recall.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from claude_client import _ASK_WEB_SEARCH_MAX_USES, ClaudeClient, ClaudeResult


def _result(text: str, urls: list[str] | None = None) -> ClaudeResult:
    return ClaudeResult(
        text=text, stop_reason="end_turn", input_tokens=1, output_tokens=1,
        web_search_urls=urls or [],
    )


@pytest.mark.asyncio
async def test_classify_ask_grounding_search_vs_skip() -> None:
    client = ClaudeClient(api_key="test")
    with patch.object(client, "_call", AsyncMock(return_value=_result("SEARCH"))):
        assert await client.classify_ask_grounding("how many #1s does drake have") is True
    with patch.object(client, "_call", AsyncMock(return_value=_result("SKIP"))):
        assert await client.classify_ask_grounding("wyd") is False


@pytest.mark.asyncio
async def test_classify_ask_grounding_empty_and_fail_open() -> None:
    client = ClaudeClient(api_key="test")
    # Empty question never reaches the model and never forces.
    assert await client.classify_ask_grounding("   ") is False
    # Haiku outage fails open to False so a /ask can't be forced (and slowed) by error.
    with patch.object(client, "_call", AsyncMock(side_effect=RuntimeError("haiku down"))):
        assert await client.classify_ask_grounding("who won mvp") is False


@pytest.mark.asyncio
async def test_ask_forces_web_search_for_factual_question() -> None:
    """Factual question: forces web_search, thinking off, and drops search_memory
    (it would fight the forced tool_choice in the client-tool loop)."""
    client = ClaudeClient(api_key="test")
    generate = AsyncMock(return_value=_result("grounded answer", urls=["https://x"]))

    async def memsearch(q: str) -> str:
        return "a note"

    with patch.object(client, "classify_ask_grounding", AsyncMock(return_value=True)), \
            patch.object(client, "_call", generate):
        out = await client.ask("who won mvp", use_web=True, memory_search=memsearch)

    assert out == "grounded answer"
    kw = generate.call_args.kwargs
    assert kw["tool_choice"] == {"type": "tool", "name": "web_search"}
    assert kw["thinking_enabled"] is False
    names = [t.get("name") for t in (kw["tools"] or [])]
    assert "web_search" in names
    assert "search_memory" not in names  # excluded on the forced path
    web = next(t for t in kw["tools"] if t.get("name") == "web_search")
    assert web["max_uses"] == _ASK_WEB_SEARCH_MAX_USES  # capped, no spiral


@pytest.mark.asyncio
async def test_ask_skips_force_for_banter_keeps_thinking_and_memory() -> None:
    """Non-factual: no force, adaptive thinking stays on, memory recall offered."""
    client = ClaudeClient(api_key="test")
    generate = AsyncMock(return_value=_result("posted up. pour you something?"))

    async def memsearch(q: str) -> str:
        return "a note"

    with patch.object(client, "classify_ask_grounding", AsyncMock(return_value=False)), \
            patch.object(client, "_call", generate):
        await client.ask("wyd", use_web=True, memory_search=memsearch)

    kw = generate.call_args.kwargs
    assert kw["tool_choice"] is None
    assert kw["thinking_enabled"] is True
    names = [t.get("name") for t in (kw["tools"] or [])]
    assert "web_search" in names
    assert "search_memory" in names
