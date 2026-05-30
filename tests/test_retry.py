"""Tests for transient-failure retry in ClaudeClient (#94)."""

from unittest.mock import AsyncMock, MagicMock, patch

import anthropic
import httpx
import pytest

from claude_client import _MAX_API_RETRIES, ClaudeClient
from config import Config


@pytest.fixture
def client() -> ClaudeClient:
    return ClaudeClient(Config.from_env())


def _resp(text: str = "ok"):
    block = MagicMock()
    block.type = "text"
    block.text = text
    resp = MagicMock()
    resp.content = [block]
    resp.usage.input_tokens = 10
    resp.usage.output_tokens = 5
    resp.stop_reason = "end_turn"
    return resp


def _api_error(status: int) -> anthropic.APIStatusError:
    request = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    response = httpx.Response(status, request=request)
    return anthropic.APIStatusError("boom", response=response, body=None)


async def test_retries_on_529_then_succeeds(client):
    """A single 529 (overloaded) is retried and the eventual success returns."""
    good = _resp()
    client._client.messages.create = AsyncMock(side_effect=[_api_error(529), good])
    with patch("claude_client.asyncio.sleep", new=AsyncMock()) as sleep:
        result = await client._create_with_retry(
            purpose="t", model="m", system="s", messages=[], max_tokens=10
        )
    assert result is good
    assert client._client.messages.create.await_count == 2
    sleep.assert_awaited_once()  # backed off once before the retry


async def test_non_retryable_status_raises_immediately(client):
    """A 400 is not retried; it raises on the first attempt."""
    client._client.messages.create = AsyncMock(side_effect=_api_error(400))
    with patch("claude_client.asyncio.sleep", new=AsyncMock()) as sleep:
        with pytest.raises(anthropic.APIStatusError):
            await client._create_with_retry(
                purpose="t", model="m", system="s", messages=[], max_tokens=10
            )
    assert client._client.messages.create.await_count == 1
    sleep.assert_not_awaited()


async def test_retries_exhausted_emits_failure_event(client, capsys):
    """Persistent 529s exhaust retries, emit a claude_api ok=False event, re-raise."""
    client._client.messages.create = AsyncMock(side_effect=_api_error(529))
    with patch("claude_client.asyncio.sleep", new=AsyncMock()):
        with pytest.raises(anthropic.APIStatusError):
            await client._create_with_retry(
                purpose="t", model="m", system="s", messages=[], max_tokens=10
            )
    # initial attempt + _MAX_API_RETRIES retries
    assert client._client.messages.create.await_count == _MAX_API_RETRIES + 1
    out = capsys.readouterr().out
    assert "EVENT " in out
    assert '"ok": false' in out
    assert '"event": "claude_api"' in out


async def test_classify_market_intent_emits_error_on_failure(client, capsys):
    """Silent-drop is gone: a failure logs an observable error event."""
    client._client.messages.create = AsyncMock(side_effect=RuntimeError("kaboom"))
    result = await client.classify_market_intent(user_msg="who wins the election")
    assert result == {"is_market": False}
    out = capsys.readouterr().out
    assert '"event": "error"' in out
    assert '"source": "classify_market_intent"' in out
