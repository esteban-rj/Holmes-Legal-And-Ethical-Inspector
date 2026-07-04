"""Example: a plugin agent (FR-010). Drops into the registry with zero edits to core."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, List

from holmes_swarm.blackboard.schema import Signal
from holmes_swarm.investigations.models import InvestigationScope


class BedOccupancyAuditor:
    id = "bed_occupancy"
    name = "Bed Occupancy Auditor"
    signal_type = "operational"
    confidence_threshold = 0.75

    async def run(self, batch: Any, *, scope: Any = None) -> List[Signal]:
        if not isinstance(batch, dict):
            return []
        entity_id = batch.get("entity_id") or (scope.target_entity_id if isinstance(scope, InvestigationScope) else None)
        if not entity_id:
            return []
        beds = int(batch.get("beds", 0) or 0)
        admissions = int(batch.get("admissions", 0) or 0)
        if beds <= 0:
            return []
        ratio = admissions / beds
        if ratio <= 1.5:
            return []
        confidence = min(0.95, 0.5 + (ratio - 1.5) * 0.3)
        origin = (
            {"kind": "investigation", "investigation_request_id": str(scope.investigation_request_id)}
            if isinstance(scope, InvestigationScope)
            else {"kind": "autonomous-monitoring"}
        )
        return [Signal(
            entity_id=entity_id,
            signal_type=self.signal_type,
            source_agent=self.id,
            confidence=confidence,
            evidence={"pattern": "bed_oversubscription", "ratio": round(ratio, 3)},
            below_threshold=confidence < self.confidence_threshold,
            origin=origin,
            emitted_at=datetime.now(timezone.utc),
        )]

    async def shutdown(self) -> None:
        return None
