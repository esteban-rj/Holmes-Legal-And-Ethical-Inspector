"""Signal query routes (FR-035)."""

from __future__ import annotations

from datetime import datetime
from typing import List, Optional

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel

from ...blackboard.schema import Signal


router = APIRouter(prefix="/signals", tags=["signals"])


class SignalView(BaseModel):
    id: str
    entity_id: str
    signal_type: str
    source_agent: str
    confidence: float
    evidence: dict
    emitted_at: str
    origin: dict
    below_threshold: bool


class SignalList(BaseModel):
    items: List[SignalView]
    next_offset: Optional[int] = None


def _principal(request: Request) -> str:
    return request.app.state.auth.principal(request.headers.get("Authorization"))


def _view(s: Signal) -> SignalView:
    return SignalView(
        id=str(s.id),
        entity_id=s.entity_id,
        signal_type=s.signal_type,
        source_agent=s.source_agent,
        confidence=s.confidence,
        evidence=s.evidence,
        emitted_at=s.emitted_at.isoformat(),
        origin=s.origin,
        below_threshold=s.below_threshold,
    )


@router.get("", response_model=SignalList)
def list_signals(
    request: Request,
    entity_id: Optional[str] = None,
    origin_kind: Optional[str] = None,
    investigation_request_id: Optional[str] = None,
    since: Optional[datetime] = None,
    until: Optional[datetime] = None,
    limit: int = Query(default=100, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
    include_evidence: bool = Query(default=True),
    _: str = Depends(_principal),
) -> SignalList:
    bus = request.app.state.bus
    items = bus.query_signals(
        entity_id=entity_id,
        origin_kind=origin_kind,
        investigation_request_id=investigation_request_id,
        since=since,
        until=until,
        limit=limit,
        offset=offset,
    )
    views = [_view(s) for s in items]
    if not include_evidence:
        for v in views:
            v.evidence = {}
    next_offset = (offset + limit) if (offset + limit) > 0 and len(items) == limit else None
    return SignalList(items=views, next_offset=next_offset)
