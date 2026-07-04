"""FR-018 blocked-host enforcement test."""

from __future__ import annotations

import asyncio

import pytest

from holmes_swarm.blackboard.schema import BlockedHostError


@pytest.mark.asyncio
async def test_contracting_blocked_host(app):
    contracting = app.state.registry.get("contracting")
    if contracting.http is None:
        pytest.skip("contracting http client disabled by default")
    with pytest.raises(BlockedHostError):
        await contracting.http.get("https://evil.example.com/x")


@pytest.mark.asyncio
async def test_medical_has_no_http_client(app):
    medical = app.state.registry.get("medical")
    assert medical.http is None


@pytest.mark.asyncio
async def test_consensus_has_no_http_client():
    from holmes_swarm.agents.consensus import ConsensusAgent
    from holmes_swarm.blackboard.queue_bus import QueueBus

    co = ConsensusAgent(bus=QueueBus())
    assert not hasattr(co, "http") or co.http is None
