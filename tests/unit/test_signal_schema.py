"""FR-031: Origin discriminated union validation."""

from __future__ import annotations

import uuid

import pytest
from pydantic import ValidationError

from holmes_swarm.blackboard.schema import Signal


def _base(**overrides):
    base = dict(
        entity_id="900123456-7",
        signal_type="financial",
        source_agent="contracting",
        confidence=0.8,
        evidence={},
        origin={"kind": "autonomous-monitoring"},
    )
    base.update(overrides)
    return base


def test_origin_autonomous_ok():
    s = Signal(**_base())
    assert s.origin == {"kind": "autonomous-monitoring"}


def test_origin_investigation_ok():
    rid = str(uuid.uuid4())
    s = Signal(**_base(origin={"kind": "investigation", "investigation_request_id": rid}))
    assert s.origin["investigation_request_id"] == rid


def test_origin_missing_rejected():
    with pytest.raises(ValidationError):
        Signal(**{k: v for k, v in _base().items() if k != "origin"})


def test_origin_unknown_rejected():
    with pytest.raises(ValidationError):
        Signal(**_base(origin={"kind": "made-up"}))


def test_origin_investigation_missing_id_rejected():
    with pytest.raises(ValidationError):
        Signal(**_base(origin={"kind": "investigation"}))


def test_confidence_range():
    with pytest.raises(ValidationError):
        Signal(**_base(confidence=1.5))
    with pytest.raises(ValidationError):
        Signal(**_base(confidence=-0.1))
