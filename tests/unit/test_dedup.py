"""FR-012 dedup tests."""

from __future__ import annotations

import uuid

from holmes_swarm.blackboard.schema import Signal
from holmes_swarm.blackboard.dedup import Deduper


def _sig(entity_id, agent, st, confidence=0.8, bucket=0):
    return Signal(
        entity_id=entity_id,
        signal_type=st,
        source_agent=agent,
        confidence=confidence,
        origin={"kind": "autonomous-monitoring"},
        emitted_at=_epoch_for_bucket(bucket),
    )


def _epoch_for_bucket(bucket: int, window: int = 60) -> "datetime":
    from datetime import datetime, timezone, timedelta
    return datetime.fromtimestamp(bucket * window, tz=timezone.utc)


def test_first_accepted_second_dropped():
    d = Deduper(window_seconds=60)
    a = _sig("e1", "contracting", "financial")
    b = _sig("e1", "contracting", "financial")
    assert d.accept(a) is True
    assert d.accept(b) is False
    assert d.stats.accepted == 1
    assert d.stats.dropped == 1


def test_different_entities_not_deduped():
    d = Deduper(window_seconds=60)
    assert d.accept(_sig("e1", "contracting", "financial")) is True
    assert d.accept(_sig("e2", "contracting", "financial")) is True


def test_different_agents_not_deduped():
    d = Deduper(window_seconds=60)
    assert d.accept(_sig("e1", "contracting", "financial")) is True
    assert d.accept(_sig("e1", "logistics", "financial")) is True


def test_different_buckets_not_deduped():
    d = Deduper(window_seconds=60)
    assert d.accept(_sig("e1", "contracting", "financial", bucket=0)) is True
    assert d.accept(_sig("e1", "contracting", "financial", bucket=5)) is True
