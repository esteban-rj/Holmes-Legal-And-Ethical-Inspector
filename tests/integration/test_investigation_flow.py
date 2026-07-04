"""SC-009 / SC-010 / SC-011 / SC-012 investigation flow integration tests."""

from __future__ import annotations

import asyncio
import json
import uuid
from pathlib import Path

import pytest

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"


def _agent_batch(entity_id):
    contracts = json.loads((FIXTURES / "contracts.json").read_text())
    attendance = json.loads((FIXTURES / "attendance.json").read_text())
    clinical = json.loads((FIXTURES / "clinical.json").read_text())
    pqrs = json.loads((FIXTURES / "pqrs.json").read_text())
    return {
        "entity_id": entity_id,
        "contracts": contracts.get("contracts", []),
        "events": attendance.get("events", []),
        "specialty": clinical.get("specialty"),
        "services": clinical.get("services", []),
        "procedures": clinical.get("procedures", [])[:3],
        "pqrs": pqrs.get("pqrs", []),
    }


@pytest.mark.asyncio
async def test_investigation_happy_path(app, bus, consensus, audit):
    svc = app.state.investigation_service
    svc.data_loader = lambda eid: _agent_batch(eid)
    req = await svc.submit(
        requester_id="user:esteban",
        target_entity_id="900123456-7",
        agents=["contracting", "medical", "whistleblower"],
    )
    # submit() now runs the investigation in the background; wait for it.
    for _ in range(50):
        if req.state == "completed":
            break
        await asyncio.sleep(0.1)
    assert req.state == "completed"
    assert req.report_id is not None
    inv_sigs = bus.query_signals(investigation_request_id=str(req.id))
    assert all(s.origin["kind"] == "investigation" for s in inv_sigs)
    await consensus.evaluate_once()
    alerts = bus.list_alerts(entity_id="900123456-7")
    assert len(alerts) >= 1
    # All alerts must reference the originating investigation id
    for a in alerts:
        assert str(a.investigation_request_id) == str(req.id)


@pytest.mark.asyncio
async def test_unauthorised_request_rejected(app):
    """SC-012: unauthorised request returns 4xx with zero side effects."""
    import httpx

    transport = httpx.ASGITransport(app=app)
    client = httpx.AsyncClient(transport=transport, base_url="http://test")
    r = await client.post("/investigations", json={"target_entity_id": "900123456-7"})
    assert r.status_code in (401, 403)
    # No signals should have been produced
    assert app.state.bus.all_signals() == []
    await client.aclose()


@pytest.mark.asyncio
async def test_audit_log_completeness(app, audit):
    svc = app.state.investigation_service
    svc.data_loader = lambda eid: _agent_batch(eid)
    req = await svc.submit(
        requester_id="user:auditor",
        target_entity_id="900123456-7",
        agents=["contracting"],
    )
    for _ in range(50):
        if any(e.request_id == req.id for e in audit.query(action="investigation.complete")):
            break
        await asyncio.sleep(0.1)
    submits = audit.query(action="investigation.submit")
    completes = audit.query(action="investigation.complete")
    assert any(e.actor == "user:auditor" and str(e.request_id) == str(req.id) for e in submits)
    assert any(str(e.request_id) == str(req.id) for e in completes)


@pytest.mark.asyncio
async def test_chat_endpoint_parses_spanish_query_and_runs_agents(app):
    """The /chat endpoint must accept a natural-language Spanish request,
    extract a target entity, and submit an investigation that produces signals."""
    import httpx

    from holmes_swarm.api.chat import _fallback_parse

    # 1) Parser unit check (deterministic, no LLM)
    parsed = _fallback_parse(
        "Encuentra movimientos alarmantes del señor Dr. Ciro Alfonso Gómez Meisel "
        "de la clínica Clínica Meisel SAS en la SUBRED INTEGRADA DE SERVICIOS DE SALUD Norte y Sur"
    )
    assert "Ciro Alfonso Gómez Meisel" in parsed.target_entity_id
    assert parsed.location is not None
    assert "SUBRED" in parsed.location.upper()
    assert "contracting" in parsed.agents
    assert "logistics" in parsed.agents

    # 2) End-to-end via HTTP
    svc = app.state.investigation_service
    svc.data_loader = lambda eid: _agent_batch(eid)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        r = await client.post(
            "/chat",
            headers={"Authorization": "Bearer demo-token-esteban"},
            json={
                "message": (
                    "Investiga al Dr. Ciro Alfonso Gómez Meisel en la "
                    "Clínica Meisel SAS, SUBRED INTEGRADA DE SERVICIOS DE SALUD Norte y Sur"
                ),
                "auto_submit": True,
            },
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["request_id"]
        assert "Ciro Alfonso Gómez Meisel" in body["parsed"]["target_entity_id"]
        # Stream URL is returned (so the UI can attach)
        assert body["stream_url"].startswith("/investigations/")
        # The investigation now runs in the background, so poll briefly for signals.
        rid = body["request_id"]
        signals = []
        for _ in range(50):  # up to 5s
            signals = app.state.bus.query_signals(investigation_request_id=rid)
            if signals:
                break
            await asyncio.sleep(0.1)
        assert len(signals) >= 1, "investigation did not produce signals in time"
        assert all(s.origin["kind"] == "investigation" for s in signals)


@pytest.mark.asyncio
async def test_progress_events_emitted_during_run(app):
    """The service's pub/sub mechanism delivers events in order to a subscriber
    that is attached before any events are published."""
    import uuid
    svc = app.state.investigation_service
    rid = uuid.uuid4()
    sub = await svc.subscribe(rid)
    svc._publish_event(rid, "state_changed", payload={"state": "running"})
    svc._publish_event(rid, "agent_started", agent_id="contracting", payload={"a": 1})
    svc._publish_event(rid, "signal", agent_id="contracting", payload={"x": 2})
    svc._publish_event(rid, "agent_completed", agent_id="contracting", payload={"b": 3})
    svc._publish_event(rid, "completed", payload={"summary": "ok"})

    kinds = []
    while not sub.queue.empty():
        evt = sub.queue.get_nowait()
        kinds.append(evt.kind)
    assert kinds == [
        "state_changed",
        "agent_started",
        "signal",
        "agent_completed",
        "completed",
    ]
    # Cleanup
    svc.unsubscribe(rid, sub)


@pytest.mark.asyncio
async def test_sse_stream_replays_signals_for_completed_investigation(app):
    """GET /investigations/{id}/stream must replay signals for a completed run
    and then close the stream."""
    import httpx
    svc = app.state.investigation_service
    svc.data_loader = lambda eid: _agent_batch(eid)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        # Submit
        r = await client.post(
            "/chat",
            headers={"Authorization": "Bearer demo-token-esteban"},
            json={"message": "Investiga Dr. Ciro Alfonso Gómez Meisel en SUBRED NORTE", "auto_submit": True},
        )
        rid = r.json()["request_id"]
        # Wait for the investigation to fully complete before reading the stream
        # so the bus has the signals for the replay path.
        req = svc.status(uuid.UUID(rid))
        for _ in range(100):
            req = svc.status(uuid.UUID(rid))
            if req is not None and req.state == "completed":
                break
            await asyncio.sleep(0.1)
        # Stream
        s = await client.get(
            f"/investigations/{rid}/stream",
            params={"token": "demo-token-esteban"},
        )
        assert s.status_code == 200
        assert s.headers["content-type"].startswith("text/event-stream")
        body = s.text
        # Replayed at least one signal
        assert "signal_replay" in body
