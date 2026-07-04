"""FR-011 confidence threshold + FR-008 origin-gating on the ConsensusAgent."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest

from holmes_swarm.agents.consensus import ConsensusAgent
from holmes_swarm.blackboard.queue_bus import QueueBus
from holmes_swarm.blackboard.schema import Signal


def _sig(agent, st, conf, rid, entity="e-unique"):
    return Signal(
        entity_id=entity, signal_type=st, source_agent=agent, confidence=conf,
        origin={"kind": "investigation", "investigation_request_id": rid},
        emitted_at=datetime.now(timezone.utc),
    )


@pytest.mark.asyncio
async def test_below_threshold_signal_stored_below_threshold_flag():
    bus = QueueBus(dedup_window_seconds=1)
    rid = str(uuid.uuid4())
    sig = _sig("contracting", "financial", 0.5, rid, entity="e1-below")
    sig.below_threshold = True
    await bus.publish(sig)
    items = bus.query_signals(entity_id="e1-below")
    assert len(items) == 1
    assert items[0].below_threshold is True
    co = ConsensusAgent(bus=bus)
    emitted = await co.evaluate_once()
    assert emitted == []


@pytest.mark.asyncio
async def test_above_threshold_emits_alert():
    bus = QueueBus(dedup_window_seconds=1)
    rid = str(uuid.uuid4())
    sig = _sig("contracting", "financial", 0.95, rid, entity="e1-above")
    sig.below_threshold = False
    await bus.publish(sig)
    co = ConsensusAgent(bus=bus)
    emitted = await co.evaluate_once()
    assert len(emitted) == 1