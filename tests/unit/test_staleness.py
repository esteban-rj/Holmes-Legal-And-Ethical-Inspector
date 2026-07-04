"""FR-013 staleness tests."""

from __future__ import annotations

from datetime import datetime, timezone, timedelta

from holmes_swarm.blackboard.schema import Signal
from holmes_swarm.blackboard.staleness import is_stale, filter_eligible


def _sig(seconds_ago: int):
    return Signal(
        entity_id="e",
        signal_type="financial",
        source_agent="contracting",
        confidence=0.8,
        origin={"kind": "autonomous-monitoring"},
        emitted_at=datetime.now(timezone.utc) - timedelta(seconds=seconds_ago),
    )


def test_fresh_is_not_stale():
    assert is_stale(_sig(seconds_ago=10), datetime.now(timezone.utc), window_seconds=60) is False


def test_old_is_stale():
    assert is_stale(_sig(seconds_ago=120), datetime.now(timezone.utc), window_seconds=60) is True


def test_filter_eligible_excludes_stale():
    now = datetime.now(timezone.utc)
    sigs = [_sig(seconds_ago=10), _sig(seconds_ago=120)]
    eligible = filter_eligible(sigs, now, window_seconds=60)
    assert len(eligible) == 1
