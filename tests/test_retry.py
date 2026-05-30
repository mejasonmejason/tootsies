"""Tests for transient-failure retry in ClaudeClient (#94)."""

from __future__ import annotations

import logging
from unittest.mock import AsyncMock, MagicMock, patch

import anthropic
import httpx
import pytest

from claude_client import _MAX_API_RETRIES, ClaudeClient


@pytest.fixture
def client() -> ClaudeClient:
    return ClaudeClient()


def _resp() -> MagicMock:
    """A minimal messages.create-shaped response object."""
    resp = MagicMock()
    resp.usage.input_tokens = 10
    resp.usage.output_tokens = 5
    resp.stop_reason = "end_turn"
    return resp


def _api_error(status: int) -> anthropic.APIStatusError:
    request = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    response = httpx.Response(status, request=request)
    return anthropic.APIStatusError("boom", response=response, body=None)


async def test_retries_on_529_then_succeeds(client: ClaudeClient) -> None:
    """A single 529 (overloaded) is retried and the eventual success returns."""
    good = _resp()
    client.client.messages.create = AsyncMock(side_effect=[_api_error(529), good])
    with patch("claude_client.asyncio.sleep", new=AsyncMock()) as sleep:
        result = await client._create_with_retry(purpose="t", model="m", max_tokens=10)
    assert result is good
    assert client.client.messages.create.await_count == 2
    sleep.assert_awaited_once()  # backed off once before the single retry


async def test_non_retryable_status_raises_immediately(client: ClaudeClient) -> None:
    """A 400 is not retried; it raises on the first attempt, no backoff."""
    client.client.messages.create = AsyncMock(side_effect=_api_error(400))
    with (
        patch("claude_client.asyncio.sleep", new=AsyncMock()) as sleep,
        pytest.raises(anthropic.APIStatusError),
    ):
        await client._create_with_retry(purpose="t", model="m", max_tokens=10)
    assert client.client.messages.create.await_count == 1
    sleep.assert_not_awaited()


async def test_retries_exhausted_then_reraises(client: ClaudeClient) -> None:
    """Persistent 529s exhaust the retry budget and the last error propagates."""
    client.client.messages.create = AsyncMock(side_effect=_api_error(529))
    with (
        patch("claude_client.asyncio.sleep", new=AsyncMock()) as sleep,
        pytest.raises(anthropic.APIStatusError),
    ):
        await client._create_with_retry(purpose="t", model="m", max_tokens=10)
    # one initial attempt + _MAX_API_RETRIES retries, backing off before each retry
    assert client.client.messages.create.await_count == _MAX_API_RETRIES + 1
    assert sleep.await_count == _MAX_API_RETRIES


async def test_call_emits_failure_event_after_exhaustion(
    client: ClaudeClient, caplog: pytest.LogCaptureFixture
) -> None:
    """When retries are exhausted inside _call, the claude_api ok=False event
    still fires so the failure is visible in dashboards, then it re-raises."""
    caplog.set_level(logging.INFO, logger="tootsies.events")
    client.client.messages.create = AsyncMock(side_effect=_api_error(529))
    with (
        patch("claude_client.asyncio.sleep", new=AsyncMock()),
        pytest.raises(anthropic.APIStatusError),
    ):
        await client._call(model="m", user_message="hi", purpose="ask")
    msgs = [r.getMessage() for r in caplog.records]
    assert any(
        '"event":"claude_api"' in m and '"ok":false' in m for m in msgs
    ), msgs


async def test_classify_market_intent_emits_error_on_failure(
    client: ClaudeClient, caplog: pytest.LogCaptureFixture
) -> None:
    """Silent-drop is gone: a failure emits an observable error event and the
    method still returns None (the safe 'no market context' fallback)."""
    caplog.set_level(logging.INFO, logger="tootsies.events")
    client.client.messages.create = AsyncMock(side_effect=RuntimeError("kaboom"))
    result = await client.classify_market_intent(query="who wins the election")
    assert result is None
    msgs = [r.getMessage() for r in caplog.records]
    assert any(
        '"event":"error"' in m and '"source":"classify_market_intent"' in m
        for m in msgs
    ), msgs
