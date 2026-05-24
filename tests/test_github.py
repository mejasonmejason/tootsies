"""Tests for utils.github — the issue body composer + HTTP client error paths."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import aiohttp
import pytest

from utils.github import GitHubClient, issue_body_for_order

# ---- issue_body_for_order ---------------------------------------------------------


def test_issue_body_for_order_addresses_claude() -> None:
    """The body must tag @claude so the claude-code-action workflow fires."""
    body = issue_body_for_order(
        request_text="add a /dadjoke command",
        summary="add /dadjoke",
        requester_tag="<@123>",
    )
    assert "@claude" in body


def test_issue_body_for_order_includes_requester_and_summary() -> None:
    body = issue_body_for_order(
        request_text="add a /dadjoke command",
        summary="add /dadjoke",
        requester_tag="<@123>",
    )
    assert "<@123>" in body
    assert "add /dadjoke" in body
    assert "add a /dadjoke command" in body


def test_issue_body_for_order_references_constitution_and_persona() -> None:
    """The body should remind Claude of the guardrails on every order."""
    body = issue_body_for_order("x", "x", "<@1>")
    assert "constitution.py" in body
    assert "persona.py" in body


def test_issue_body_for_order_references_ci_checks() -> None:
    """The body tells Claude to run ruff/mypy/pytest, matching what ci.yml runs."""
    body = issue_body_for_order("x", "x", "<@1>")
    lower = body.lower()
    assert "ruff" in lower
    assert "mypy" in lower
    assert "pytest" in lower


# ---- HTTP client behavior --------------------------------------------------------


class _FakeResponse:
    def __init__(self, status: int, payload: dict | None = None) -> None:
        self.status = status
        self._payload = payload or {}

    async def __aenter__(self) -> _FakeResponse:
        return self

    async def __aexit__(self, *_args: object) -> None:
        return None

    async def json(self) -> dict:
        return self._payload

    def raise_for_status(self) -> None:
        if self.status >= 400:
            raise aiohttp.ClientResponseError(
                request_info=MagicMock(),
                history=(),
                status=self.status,
                message=f"http {self.status}",
            )


@pytest.mark.asyncio
async def test_create_issue_returns_response_json() -> None:
    client = GitHubClient(token="t", repo="owner/repo")
    fake_session = MagicMock()
    fake_session.post = MagicMock(return_value=_FakeResponse(
        status=201, payload={"number": 42, "html_url": "https://gh/issues/42"},
    ))
    # Replace the lazy session getter with our fake.
    with patch.object(client, "_sess", AsyncMock(return_value=fake_session)):
        out = await client.create_issue(
            title="x", body="y", labels=["order", "claude"],
        )
    assert out["number"] == 42
    fake_session.post.assert_called_once()


@pytest.mark.asyncio
async def test_create_issue_raises_on_non_2xx() -> None:
    client = GitHubClient(token="t", repo="owner/repo")
    fake_session = MagicMock()
    fake_session.post = MagicMock(return_value=_FakeResponse(status=422))
    with (
        patch.object(client, "_sess", AsyncMock(return_value=fake_session)),
        pytest.raises(aiohttp.ClientResponseError),
    ):
        await client.create_issue(title="x", body="y")


@pytest.mark.asyncio
async def test_close_issue_sends_state_closed() -> None:
    client = GitHubClient(token="t", repo="owner/repo")
    fake_session = MagicMock()
    fake_session.patch = MagicMock(return_value=_FakeResponse(status=200))
    with patch.object(client, "_sess", AsyncMock(return_value=fake_session)):
        await client.close_issue(42)
    # Verify the PATCH body included {"state": "closed"}.
    call = fake_session.patch.call_args
    assert call.kwargs["json"] == {"state": "closed"}


@pytest.mark.asyncio
async def test_close_idempotent_on_repeated_session_grab() -> None:
    """_sess should reuse the same aiohttp.ClientSession across calls."""
    client = GitHubClient(token="t", repo="owner/repo")
    s1 = await client._sess()
    s2 = await client._sess()
    assert s1 is s2
    await client.close()
