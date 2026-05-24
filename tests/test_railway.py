"""Railway client tests — exercise the rollback target-picking logic without hitting the API."""

from __future__ import annotations

import os
from unittest.mock import AsyncMock, patch

import pytest

from utils.railway import Deployment, RailwayClient, RailwayError


@pytest.fixture
def client() -> RailwayClient:
    return RailwayClient(token="test", service_id="svc-123")


@pytest.mark.asyncio
async def test_rollback_picks_previous_when_current_is_top(client: RailwayClient) -> None:
    """When the current deployment is the most recent SUCCESS, target the next one back."""
    deployments = [
        Deployment(id="dep-3", status="SUCCESS", created_at="2026-05-23T10:00:00Z"),
        Deployment(id="dep-2", status="SUCCESS", created_at="2026-05-22T10:00:00Z"),
        Deployment(id="dep-1", status="SUCCESS", created_at="2026-05-21T10:00:00Z"),
    ]
    with (
        patch.object(client, "recent_successful_deployments", AsyncMock(return_value=deployments)),
        patch.object(client, "redeploy", AsyncMock(return_value="new-dep-id")) as redeploy,
        patch.dict(os.environ, {"RAILWAY_DEPLOYMENT_ID": "dep-3"}),
    ):
        target, new_id = await client.rollback_to_previous()
    assert target.id == "dep-2"
    assert new_id == "new-dep-id"
    redeploy.assert_awaited_once_with("dep-2")


@pytest.mark.asyncio
async def test_rollback_skips_current_even_if_not_top(client: RailwayClient) -> None:
    """If RAILWAY_DEPLOYMENT_ID matches a non-top entry (rare), we still skip it correctly."""
    deployments = [
        Deployment(id="dep-3", status="SUCCESS", created_at="2026-05-23T10:00:00Z"),
        Deployment(id="dep-2", status="SUCCESS", created_at="2026-05-22T10:00:00Z"),
    ]
    with (
        patch.object(client, "recent_successful_deployments", AsyncMock(return_value=deployments)),
        patch.object(client, "redeploy", AsyncMock(return_value="new-id")),
        patch.dict(os.environ, {"RAILWAY_DEPLOYMENT_ID": "dep-3"}),
    ):
        target, _ = await client.rollback_to_previous()
    assert target.id == "dep-2"


@pytest.mark.asyncio
async def test_rollback_without_current_picks_top(client: RailwayClient) -> None:
    """Off-Railway runs (no RAILWAY_DEPLOYMENT_ID injected) just take the most recent SUCCESS."""
    deployments = [
        Deployment(id="dep-3", status="SUCCESS", created_at="2026-05-23T10:00:00Z"),
        Deployment(id="dep-2", status="SUCCESS", created_at="2026-05-22T10:00:00Z"),
    ]
    env_without = {k: v for k, v in os.environ.items() if k != "RAILWAY_DEPLOYMENT_ID"}
    with (
        patch.object(client, "recent_successful_deployments", AsyncMock(return_value=deployments)),
        patch.object(client, "redeploy", AsyncMock(return_value="new-id")),
        patch.dict(os.environ, env_without, clear=True),
    ):
        target, _ = await client.rollback_to_previous()
    assert target.id == "dep-3"


@pytest.mark.asyncio
async def test_rollback_raises_when_no_successes(client: RailwayClient) -> None:
    with (
        patch.object(client, "recent_successful_deployments", AsyncMock(return_value=[])),
        pytest.raises(RailwayError, match="no successful deployments"),
    ):
        await client.rollback_to_previous()


@pytest.mark.asyncio
async def test_rollback_raises_when_only_current_exists(client: RailwayClient) -> None:
    deployments = [Deployment(id="dep-1", status="SUCCESS", created_at="2026-05-23T10:00:00Z")]
    with (
        patch.object(client, "recent_successful_deployments", AsyncMock(return_value=deployments)),
        patch.dict(os.environ, {"RAILWAY_DEPLOYMENT_ID": "dep-1"}),
        pytest.raises(RailwayError, match="nothing to roll back to"),
    ):
        await client.rollback_to_previous()
