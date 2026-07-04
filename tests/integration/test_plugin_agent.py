"""SC-002 / FR-010 plugin agent integration test."""

from __future__ import annotations

import asyncio

import pytest

from examples.bed_occupancy_agent import BedOccupancyAuditor


@pytest.mark.asyncio
async def test_plugin_agent_signals_flow(app, bus, consensus):
    registry = app.state.registry
    bus_app = app.state.bus

    # Drop in the plugin agent at runtime (FR-010)
    plugin = BedOccupancyAuditor()
    registry.register(plugin)

    try:
        # Run with scope=None (autonomous)
        sigs = await plugin.run({"entity_id": "900123456-7", "beds": 50, "admissions": 200})
        assert len(sigs) == 1
        sigs[0].origin = {"kind": "autonomous-monitoring"}
        sigs[0].entity_id = "900123456-7-autonomous"  # distinct entity to avoid dedup collision
        await bus_app.publish(sigs[0])

        # Run with scope=InvestigationScope (origin-gated)
        from holmes_swarm.investigations.models import InvestigationScope
        import uuid

        rid = uuid.uuid4()
        scope = InvestigationScope(investigation_request_id=rid, target_entity_id="900123456-7-inv")
        sigs2 = await plugin.run(
            {"entity_id": "900123456-7-inv", "beds": 50, "admissions": 250}, scope=scope
        )
        assert len(sigs2) == 1
        assert sigs2[0].origin["kind"] == "investigation"
        await bus_app.publish(sigs2[0])

        emitted = await consensus.evaluate_once()
        assert len(emitted) == 1  # only the investigation-origin signal triggers
        a = emitted[0]
        assert "bed_occupancy" in a.contributing_agent_ids
        assert str(a.investigation_request_id) == str(rid)
    finally:
        registry.unregister(plugin.id)
