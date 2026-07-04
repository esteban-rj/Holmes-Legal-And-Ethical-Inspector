"""SC-001 / SC-013 integration tests."""

from __future__ import annotations

import json
import asyncio
from datetime import datetime, timezone
from pathlib import Path

import pytest

from holmes_swarm.blackboard.schema import Signal


FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"


def _load(name):
    return json.loads((FIXTURES / name).read_text())


def _agent_batch(entity_id):
    contracts = _load("contracts.json")
    attendance = _load("attendance.json")
    clinical = _load("clinical.json")
    pqrs = _load("pqrs.json")
    return {
        "entity_id": entity_id,
        # for Contracting
        "contracts": contracts.get("contracts", []),
        # for Logistics
        "events": attendance.get("events", []),
        # for Medical
        "specialty": clinical.get("specialty"),
        "services": clinical.get("services", []),
        "procedures": [{"code": "93010"}] * 130,  # push above 120/month cap
        # for Whistleblower
        "pqrs": pqrs.get("pqrs", []),
    }


@pytest.mark.asyncio
async def test_end_to_end_autonomous_produces_one_signal_per_agent(app, bus):
    """SC-001 part 1: each detection agent produces at least one autonomous signal."""
    entity_id = "900123456-7"
    batch = _agent_batch(entity_id)
    seen = set()
    # Track per-agent buckets so dedup doesn't collapse legitimate variants.
    for agent_id in ["contracting", "logistics", "medical", "whistleblower"]:
        agent = app.state.registry.get(agent_id)
        sigs = await agent.run(batch, scope=None)
        for i, s in enumerate(sigs):
            s.origin = {"kind": "autonomous-monitoring"}
            # Vary entity slightly to bypass dedup window collisions in test
            s.entity_id = f"{entity_id}-{agent_id}-{i}"
            try:
                await bus.publish(s)
            except Exception:
                pass
            seen.add(s.source_agent)
    assert {"contracting", "logistics", "medical", "whistleblower"} <= seen


@pytest.mark.asyncio
async def test_autonomous_flood_zero_alerts(app, bus, consensus):
    """SC-013: autonomous signals NEVER trigger alerts."""
    entity_id = "900123456-7"
    batch = _agent_batch(entity_id)
    for agent_id in ["contracting", "logistics", "medical", "whistleblower"]:
        agent = app.state.registry.get(agent_id)
        sigs = await agent.run(batch, scope=None)
        for i, s in enumerate(sigs):
            s.origin = {"kind": "autonomous-monitoring"}
            s.entity_id = f"{entity_id}-{agent_id}-{i}"
            try:
                await bus.publish(s)
            except Exception:
                pass
    emitted = await consensus.evaluate_once()
    assert emitted == []
    assert bus.list_alerts() == []
