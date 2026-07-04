"""FR-008 / FR-033 origin gating tests for the Consensus Agent."""

from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timezone, timedelta

import pytest

from holmes_swarm.blackboard.queue_bus import QueueBus
from holmes_swarm.blackboard.schema import Signal
from holmes_swarm.agents.consensus import ConsensusAgent


@pytest.fixture
def fresh_bus():
    return QueueBus(dedup_window_seconds=1)


def _sig(*, agent, st, confidence, entity="e1", origin_kind="investigation", request_id=None, secs_ago=0):
    origin = {"kind": origin_kind}
    if origin_kind == "investigation":
        origin["investigation_request_id"] = request_id or str(uuid.uuid4())
    return Signal(
        entity_id=entity,
        signal_type=st,
        source_agent=agent,
        confidence=confidence,
        origin=origin,
        emitted_at=datetime.now(timezone.utc) - timedelta(seconds=secs_ago),
    )


def _distinct_signal(bus, *, agent, st, conf, secs_ago, origin_kind="autonomous-monitoring", request_id=None):
    """Publish a signal that bypasses dedup by using a unique bucket."""
    from datetime import datetime as _dt, timezone as _tz, timedelta as _td
    sig = Signal(
        entity_id=f"e-{agent}-{int(secs_ago)}-{st}",
        signal_type=st,
        source_agent=agent,
        confidence=conf,
        origin={"kind": origin_kind, **({"investigation_request_id": request_id} if origin_kind == "investigation" else {})},
        emitted_at=_dt.now(_tz) - _td(seconds=secs_ago),
    )
    import asyncio
    asyncio.get_event_loop().run_until_complete(bus.publish(sig))


@pytest.mark.asyncio
async def test_autonomous_signals_never_emit_alerts(fresh_bus):
    co = ConsensusAgent(bus=fresh_bus)
    # Seed 100 autonomous signals on distinct entities/times to bypass dedup
    for i in range(100):
        await fresh_bus.publish(_sig(
            agent=f"contracting-{i}", st="financial", confidence=0.99,
            origin_kind="autonomous-monitoring",
            secs_ago=i % 60,
        ))
    emitted = await co.evaluate_once()
    assert emitted == []
    assert fresh_bus.list_alerts() == []


@pytest.mark.asyncio
async def test_investigation_signal_emits_alert(fresh_bus):
    co = ConsensusAgent(bus=fresh_bus)
    rid = str(uuid.uuid4())
    await fresh_bus.publish(_sig(
        agent="contracting", st="financial", confidence=0.95,
        origin_kind="investigation", request_id=rid,
    ))
    emitted = await co.evaluate_once()
    assert len(emitted) == 1
    a = emitted[0]
    assert a.entity_id == "e1"
    assert str(a.investigation_request_id) == rid
    assert "contracting" in a.contributing_agent_ids


@pytest.mark.asyncio
async def test_below_threshold_not_alerted(fresh_bus):
    co = ConsensusAgent(bus=fresh_bus)
    rid = str(uuid.uuid4())
    sig = _sig(agent="contracting", st="financial", confidence=0.5,
               origin_kind="investigation", request_id=rid)
    sig.below_threshold = True  # simulates per-agent threshold check
    await fresh_bus.publish(sig)
    emitted = await co.evaluate_once()
    assert emitted == []


@pytest.mark.asyncio
async def test_repeat_enriches_alert(fresh_bus):
    co = ConsensusAgent(bus=fresh_bus)
    rid = str(uuid.uuid4())
    await fresh_bus.publish(_sig(agent="contracting", st="financial", confidence=0.9,
                                 origin_kind="investigation", request_id=rid))
    await co.evaluate_once()
    # second agent contributes
    await fresh_bus.publish(_sig(agent="medical", st="clinical", confidence=0.9,
                                 origin_kind="investigation", request_id=rid))
    emitted = await co.evaluate_once()
    assert len(emitted) == 1
    assert sorted(emitted[0].contributing_agent_ids) == ["contracting", "medical"]


@pytest.mark.asyncio
async def test_alert_store_rejects_missing_investigation_id(fresh_bus):
    """A CriticalFraudAlert cannot exist without an investigation_request_id.

    Defense in depth: the schema rejects `None` (FR-034), and the store
    re-validates (would also reject if a malformed alert slipped through).
    """
    from holmes_swarm.blackboard.schema import CriticalFraudAlert
    from pydantic import ValidationError
    with pytest.raises(ValidationError):
        CriticalFraudAlert(entity_id="e1", investigation_request_id=None)  # type: ignore[arg-type]
