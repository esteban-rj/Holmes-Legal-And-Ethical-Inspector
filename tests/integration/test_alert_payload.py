"""SC-004 / SC-014 alert payload tests."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest


@pytest.mark.asyncio
async def test_alert_payload_completeness(app, bus, consensus):
    svc = app.state.investigation_service
    fixtures = Path(__file__).resolve().parent.parent / "fixtures"

    def loader(eid):
        return {
            "entity_id": eid,
            "contracts": json.loads((fixtures / "contracts.json").read_text()).get("contracts", []),
        }

    svc.data_loader = loader
    req = await svc.submit(
        requester_id="user:x", target_entity_id="900123456-7", agents=["contracting"]
    )
    # submit() now runs the investigation in the background; wait for it.
    for _ in range(50):
        if req.state == "completed":
            break
        await asyncio.sleep(0.1)
    assert req.state == "completed", req.state
    await consensus.evaluate_once()
    alerts = bus.list_alerts(entity_id="900123456-7")
    assert len(alerts) == 1
    a = alerts[0]
    assert a.entity_id == "900123456-7"
    assert str(a.investigation_request_id) == str(req.id)
    assert a.contributing_signal_ids
    assert a.contributing_agent_ids
    assert a.summary
