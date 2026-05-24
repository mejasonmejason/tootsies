"""GitHub REST client for filing /order issues + polling status.

Thin wrapper around aiohttp. Token + repo are passed in at construction so we can unit-test
with fakes.
"""

from __future__ import annotations

import logging
from typing import Any

import aiohttp

log = logging.getLogger(__name__)

API = "https://api.github.com"


class GitHubClient:
    def __init__(self, token: str, repo: str) -> None:
        self.token = token
        self.repo = repo  # "owner/name"
        self._session: aiohttp.ClientSession | None = None

    async def _sess(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                headers={
                    "Authorization": f"Bearer {self.token}",
                    "Accept": "application/vnd.github+json",
                    "X-GitHub-Api-Version": "2022-11-28",
                    "User-Agent": "tootsies-bot",
                }
            )
        return self._session

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()

    async def create_issue(
        self, title: str, body: str, labels: list[str] | None = None
    ) -> dict[str, Any]:
        sess = await self._sess()
        async with sess.post(
            f"{API}/repos/{self.repo}/issues",
            json={"title": title, "body": body, "labels": labels or []},
        ) as r:
            r.raise_for_status()
            return await r.json()

    async def get_issue(self, number: int) -> dict[str, Any]:
        sess = await self._sess()
        async with sess.get(f"{API}/repos/{self.repo}/issues/{number}") as r:
            r.raise_for_status()
            return await r.json()

    async def close_issue(self, number: int) -> None:
        sess = await self._sess()
        async with sess.patch(
            f"{API}/repos/{self.repo}/issues/{number}", json={"state": "closed"}
        ) as r:
            r.raise_for_status()

    async def comment(self, number: int, body: str) -> None:
        sess = await self._sess()
        async with sess.post(
            f"{API}/repos/{self.repo}/issues/{number}/comments", json={"body": body}
        ) as r:
            r.raise_for_status()


def issue_body_for_order(request_text: str, summary: str, requester_tag: str) -> str:
    """Compose the /order issue body that triggers claude-code-action."""
    return (
        "@claude please implement this feature for the Tootsies bot. "
        "Stay within the constitution defined in `constitution.py` and the persona in `persona.py`. "
        "Run ruff + mypy + pytest before opening the PR. "
        "When the PR is ready, mark it ready-for-review and let CI auto-merge if green.\n\n"
        f"**Requested by:** {requester_tag}\n"
        f"**Summary:** {summary}\n\n"
        f"**Original request:**\n> {request_text}\n"
    )
