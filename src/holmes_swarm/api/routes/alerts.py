"""Alert routes."""

from __future__ import annotations

from typing import List, Optional

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel

from ...blackboard.schema import CriticalFraudAlert, Signal


router = APIRouter(prefix="/alerts", tags=["alerts"])


class AlertView(BaseModel):
    id: str
    entity_id: str
    emitted_at: str
    investigation_request_id: str
    contributing_signal_ids: List[str]
    contributing_agent_ids: List[str]
    summary: str


class AlertList(BaseModel):
    items: List[AlertView]


def _principal(request: Request) -> str:
    return request.app.state.auth.principal(request.headers.get("Authorization"))


def _view(a: CriticalFraudAlert) -> AlertView:
    return AlertView(
        id=str(a.id),
        entity_id=a.entity_id,
        emitted_at=a.emitted_at.isoformat(),
        investigation_request_id=str(a.investigation_request_id),
        contributing_signal_ids=[str(x) for x in a.contributing_signal_ids],
        contributing_agent_ids=a.contributing_agent_ids,
        summary=a.summary,
    )


@router.get("", response_model=AlertList)
def list_alerts(
    request: Request,
    entity_id: Optional[str] = None,
    _: str = Depends(_principal),
) -> AlertList:
    bus = request.app.state.bus
    alerts = bus.list_alerts(entity_id=entity_id)
    return AlertList(items=[_view(a) for a in alerts])


@router.get("/{alert_id}", response_model=AlertView)
def get_alert(
    alert_id: str,
    request: Request,
    _: str = Depends(_principal),
) -> AlertView:
    bus = request.app.state.bus
    a = bus.get_alert(alert_id)
    if a is None:
        from fastapi import HTTPException, status
        raise HTTPException(status.HTTP_404_NOT_FOUND, "unknown alert")
    return _view(a)
