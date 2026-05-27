"""Additional tests for utils.railway, _gql error paths + low-level methods.

The existing tests/test_railway.py covers the high-level rollback_to_previous flow.
These tests target the underlying primitives.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from utils.railway import Deployment, LogEntry, RailwayClient, RailwayError


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


# ---- deployment_logs -----------------------------------------------------------


@pytest.mark.asyncio
async def test_deployment_logs_parses_entries(client: RailwayClient) -> None:
    fake_data = {
        "deploymentLogs": [
            {"timestamp": "2026-05-27T02:36:25Z", "message": "boot", "severity": "info"},
            {"timestamp": "2026-05-27T02:36:26Z", "message": "ready", "severity": "info"},
        ]
    }
    with patch.object(client, "_gql", AsyncMock(return_value=fake_data)):
        entries = await client.deployment_logs("dep-1", limit=50)
    assert len(entries) == 2
    assert entries[0] == LogEntry(timestamp="2026-05-27T02:36:25Z", message="boot", severity="info")


@pytest.mark.asyncio
async def test_deployment_logs_handles_empty(client: RailwayClient) -> None:
    with patch.object(client, "_gql", AsyncMock(return_value={"deploymentLogs": []})):
        assert await client.deployment_logs("dep-1") == []


@pytest.mark.asyncio
async def test_deployment_logs_handles_missing_key(client: RailwayClient) -> None:
    with patch.object(client, "_gql", AsyncMock(return_value={})):
        assert await client.deployment_logs("dep-1") == []


@pytest.mark.asyncio
async def test_deployment_logs_passes_filter(client: RailwayClient) -> None:
    gql = AsyncMock(return_value={"deploymentLogs": []})
    with patch.object(client, "_gql", gql):
        await client.deployment_logs("dep-1", limit=20, filter_text="EVENT")
    variables = gql.call_args.args[1]
    assert variables["filter"] == "EVENT"
    assert variables["limit"] == 20


@pytest.mark.asyncio
async def test_deployment_logs_omits_filter_when_none(client: RailwayClient) -> None:
    gql = AsyncMock(return_value={"deploymentLogs": []})
    with patch.object(client, "_gql", gql):
        await client.deployment_logs("dep-1", limit=10)
    variables = gql.call_args.args[1]
    assert "filter" not in variables


# ---- latest_deployment_id -----------------------------------------------------


@pytest.mark.asyncio
async def test_latest_deployment_id_returns_first(client: RailwayClient) -> None:
    fake_data = {
        "deployments": {
            "edges": [
                {"node": {"id": "latest-dep", "status": "SUCCESS", "createdAt": "t1"}},
            ]
        }
    }
    with patch.object(client, "_gql", AsyncMock(return_value=fake_data)):
        assert await client.latest_deployment_id() == "latest-dep"


@pytest.mark.asyncio
async def test_latest_deployment_id_raises_when_empty(client: RailwayClient) -> None:
    with (
        patch.object(client, "_gql", AsyncMock(return_value={"deployments": {"edges": []}})),
        pytest.raises(RailwayError, match="no deployments found"),
    ):
        await client.latest_deployment_id()


# ---- redeploy (existing) -------------------------------------------------------


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
