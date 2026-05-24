"""Railway GraphQL API client — used by /undo.

We talk to the public GraphQL endpoint at backboard.railway.app. Behavior:
1. Fetch the N most-recent SUCCESS deployments for the configured service.
2. Skip the currently-active one (Railway injects RAILWAY_DEPLOYMENT_ID into the runtime).
3. Redeploy the next-most-recent SUCCESS — `usePreviousImageTag` reuses the Docker image
   instead of rebuilding, which is the fastest correct rollback.

The Railway API surface changes occasionally. If a future schema change breaks this, the
client raises RailwayError with the raw response, so the /undo command can surface a clear
message to mods rather than fail silently.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Any

import aiohttp

log = logging.getLogger(__name__)

API_URL = "https://backboard.railway.app/graphql/v2"


@dataclass(frozen=True)
class Deployment:
    id: str
    status: str
    created_at: str


class RailwayError(RuntimeError):
    """Raised when the Railway API rejects a request or returns malformed data."""


class RailwayClient:
    def __init__(self, token: str, service_id: str) -> None:
        self.token = token
        self.service_id = service_id

    async def _gql(self, query: str, variables: dict[str, Any] | None = None) -> dict[str, Any]:
        async with aiohttp.ClientSession() as sess, sess.post(
            API_URL,
            json={"query": query, "variables": variables or {}},
            headers={
                "Authorization": f"Bearer {self.token}",
                "Content-Type": "application/json",
            },
            timeout=aiohttp.ClientTimeout(total=20),
        ) as r:
            text = await r.text()
            try:
                data = await r.json(content_type=None)
            except aiohttp.ContentTypeError as exc:
                raise RailwayError(f"non-json response (http {r.status}): {text[:300]}") from exc
            if r.status >= 400:
                raise RailwayError(f"http {r.status}: {data}")
            if data.get("errors"):
                raise RailwayError(f"graphql errors: {data['errors']}")
            payload = data.get("data")
            if payload is None:
                raise RailwayError(f"missing data in response: {data}")
            return payload

    async def recent_successful_deployments(self, limit: int = 20) -> list[Deployment]:
        """Most recent SUCCESS deployments for this service, newest first."""
        query = """
        query Deployments($serviceId: String!, $first: Int!) {
          deployments(input: { serviceId: $serviceId, status: SUCCESS }, first: $first) {
            edges {
              node {
                id
                status
                createdAt
              }
            }
          }
        }
        """
        data = await self._gql(
            query, {"serviceId": self.service_id, "first": limit}
        )
        edges = (data.get("deployments") or {}).get("edges") or []
        return [
            Deployment(
                id=e["node"]["id"],
                status=e["node"]["status"],
                created_at=e["node"]["createdAt"],
            )
            for e in edges
            if e.get("node")
        ]

    async def redeploy(self, deployment_id: str) -> str:
        """Re-run a specific deployment using its previous image. Returns the new deployment id."""
        query = """
        mutation Redeploy($id: String!) {
          deploymentRedeploy(id: $id, usePreviousImageTag: true) {
            id
          }
        }
        """
        data = await self._gql(query, {"id": deployment_id})
        node = data.get("deploymentRedeploy")
        if not node or not node.get("id"):
            raise RailwayError(f"redeploy response missing id: {data}")
        return str(node["id"])

    async def rollback_to_previous(self) -> tuple[Deployment, str]:
        """End-to-end rollback. Returns (target_deployment, new_deployment_id).

        Raises RailwayError if no rollback candidate exists or the redeploy is rejected.
        """
        current = os.environ.get("RAILWAY_DEPLOYMENT_ID")
        successes = await self.recent_successful_deployments(limit=20)
        if not successes:
            raise RailwayError("no successful deployments found for this service")

        target = next((d for d in successes if d.id != current), None)
        if target is None:
            raise RailwayError(
                "only one successful deployment exists — nothing to roll back to"
            )

        new_id = await self.redeploy(target.id)
        log.info("railway rollback: service=%s target=%s new=%s",
                 self.service_id, target.id, new_id)
        return target, new_id
