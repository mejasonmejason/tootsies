"""Additional tests for utils.railway — _gql error paths + low-level methods.

The existing tests/test_railway.py covers the high-level rollback_to_previous flow.
These tests target the underlying primitives.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from utils.railway import Deployment, RailwayClient, RailwayError


@pytest.fixture
def client() -> RailwayClient:
    return RailwayClient(token="test", service_id="svc-1")


# ---- recent_successful_deployments -----------------------------------------------


@pytest.mark.asyncio
async def test_recent_successful_deployments_parses_edges(client: RailwayClient) -> None:
    fake_data = {
        "deployments": {
            "edges": [
                {"node": {"id": "a", "status": "SUCCESS", "createdAt": "2026-05-24T10:00:00Z"}},
                {"node": {"id": "b", "status": "SUCCESS", "createdAt": "2026-05-24T09:00:00Z"}},
            ]
        }
    }
    with patch.object(client, "_gql", AsyncMock(return_value=fake_data)):
        deps = await client.recent_successful_deployments(limit=10)
    assert len(deps) == 2
    assert deps[0] == Deployment(id="a", status="SUCCESS", created_at="2026-05-24T10:00:00Z")


@pytest.mark.asyncio
async def test_recent_successful_deployments_handles_empty_edges(client: RailwayClient) -> None:
    with patch.object(client, "_gql", AsyncMock(return_value={"deployments": {"edges": []}})):
        assert await client.recent_successful_deployments() == []


@pytest.mark.asyncio
async def test_recent_successful_deployments_handles_missing_keys(client: RailwayClient) -> None:
    """Defensive: if the API shape changes and `deployments` is absent, return [] not crash."""
    with patch.object(client, "_gql", AsyncMock(return_value={})):
        assert await client.recent_successful_deployments() == []


@pytest.mark.asyncio
async def test_recent_successful_deployments_skips_edges_with_no_node(
    client: RailwayClient,
) -> None:
    fake_data = {
        "deployments": {
            "edges": [
                {"node": {"id": "a", "status": "SUCCESS", "createdAt": "t1"}},
                {},  # malformed
                {"node": {"id": "b", "status": "SUCCESS", "createdAt": "t2"}},
            ]
        }
    }
    with patch.object(client, "_gql", AsyncMock(return_value=fake_data)):
        deps = await client.recent_successful_deployments()
    assert [d.id for d in deps] == ["a", "b"]


# ---- redeploy --------------------------------------------------------------------


@pytest.mark.asyncio
async def test_redeploy_returns_new_deployment_id(client: RailwayClient) -> None:
    with patch.object(
        client, "_gql",
        AsyncMock(return_value={"deploymentRedeploy": {"id": "new-id-123"}}),
    ):
        assert await client.redeploy("old-id") == "new-id-123"


@pytest.mark.asyncio
async def test_redeploy_raises_when_response_missing_id(client: RailwayClient) -> None:
    with (
        patch.object(client, "_gql", AsyncMock(return_value={"deploymentRedeploy": None})),
        pytest.raises(RailwayError, match="missing id"),
    ):
        await client.redeploy("old-id")


@pytest.mark.asyncio
async def test_redeploy_uses_previous_image_tag(client: RailwayClient) -> None:
    """Critical: the redeploy must request usePreviousImageTag so we don't rebuild
    from latest source (which would defeat the rollback)."""
    gql = AsyncMock(return_value={"deploymentRedeploy": {"id": "new"}})
    with patch.object(client, "_gql", gql):
        await client.redeploy("old")
    # The query string sent to _gql should include usePreviousImageTag: true
    query_arg = gql.call_args.args[0]
    assert "usePreviousImageTag" in query_arg
    assert "true" in query_arg
