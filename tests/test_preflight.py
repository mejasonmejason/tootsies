"""Tests for the preflight verdict parser. We exercise the parsing branch without hitting
the API by patching _call to return canned model output.
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from claude_client import ClaudeClient


@dataclass
class _FakeResult:
    text: str
    stop_reason: str | None = "end_turn"
    input_tokens: int = 0
    output_tokens: int = 0


@pytest.fixture
def client() -> ClaudeClient:
    return ClaudeClient(api_key="test")


async def _stub(text: str):
    async def fake_call(**_kwargs):
        return _FakeResult(text=text)
    return fake_call


@pytest.mark.asyncio
async def test_preflight_allow(client: ClaudeClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(client, "_call", await _stub("ALLOW: add a /dadjoke command"))
    verdict, reason = await client.preflight_order("add a /dadjoke command")
    assert verdict == "allow"
    assert "dadjoke" in reason


@pytest.mark.asyncio
async def test_preflight_plumbing(client: ClaudeClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        client, "_call",
        await _stub("PLUMBING: would edit constitution.py"),
    )
    verdict, reason = await client.preflight_order("loosen the constitution")
    assert verdict == "plumbing"
    assert "constitution" in reason


@pytest.mark.asyncio
async def test_preflight_reject(client: ClaudeClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(client, "_call", await _stub("REJECT: moderation request"))
    verdict, reason = await client.preflight_order("add /ban")
    assert verdict == "reject"
    assert "moderation" in reason


@pytest.mark.asyncio
async def test_preflight_unparseable_fails_closed(
    client: ClaudeClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(client, "_call", await _stub("hmm not sure tbh"))
    verdict, reason = await client.preflight_order("???")
    assert verdict == "reject"
    assert "unparseable" in reason
