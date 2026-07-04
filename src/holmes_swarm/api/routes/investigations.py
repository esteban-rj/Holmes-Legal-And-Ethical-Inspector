"""Investigation routes."""

from __future__ import annotations

import json
import uuid
from typing import Any, AsyncIterator, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from ...investigations.service import InvestigationService


router = APIRouter(prefix="/investigations", tags=["investigations"])


class SubmitBody(BaseModel):
    target_entity_id: str = Field(..., min_length=1)
    agents: Optional[List[str]] = None
    scope: Dict[str, Any] = Field(default_factory=dict)


class SubmitResponse(BaseModel):
    request_id: uuid.UUID
    state: str
    status_url: str


class StatusResponse(BaseModel):
    request_id: uuid.UUID
    state: str
    agents_ran: List[str] = Field(default_factory=list)
    report_id: Optional[uuid.UUID] = None
    report_url: Optional[str] = None


class ReportResponse(BaseModel):
    id: uuid.UUID
    request_id: uuid.UUID
    target_entity_id: str
    agents_ran: List[str]
    signal_ids: List[uuid.UUID]
    summary: str
    emitted_at: str


def _principal(request: Request) -> str:
    auth = request.app.state.auth
    return auth.principal(request.headers.get("Authorization"))


@router.post("", response_model=SubmitResponse, status_code=status.HTTP_202_ACCEPTED)
async def submit(body: SubmitBody, request: Request, requester_id: str = Depends(_principal)) -> SubmitResponse:
    svc: InvestigationService = request.app.state.investigation_service
    try:
        req = await svc.submit(
            requester_id=requester_id,
            target_entity_id=body.target_entity_id,
            agents=body.agents,
            scope=body.scope,
        )
    except ValueError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc))
    return SubmitResponse(
        request_id=req.id,
        state=req.state,
        status_url=f"/investigations/{req.id}",
    )


@router.get("/{request_id}", response_model=StatusResponse)
def get_status(request_id: uuid.UUID, request: Request, _: str = Depends(_principal)) -> StatusResponse:
    svc: InvestigationService = request.app.state.investigation_service
    req = svc.status(request_id)
    if req is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "unknown request")
    return StatusResponse(
        request_id=req.id,
        state=req.state,
        agents_ran=req.agents or [],
        report_id=req.report_id,
        report_url=f"/investigations/{req.id}/report" if req.report_id else None,
    )


@router.get("/{request_id}/report", response_model=ReportResponse)
def get_report(request_id: uuid.UUID, request: Request, _: str = Depends(_principal)) -> ReportResponse:
    svc: InvestigationService = request.app.state.investigation_service
    rep = svc.report(request_id)
    if rep is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "report not ready")
    return ReportResponse(
        id=rep.id,
        request_id=rep.request_id,
        target_entity_id=rep.target_entity_id,
        agents_ran=rep.agents_ran,
        signal_ids=rep.signal_ids,
        summary=rep.summary,
        emitted_at=rep.emitted_at.isoformat(),
    )


@router.get("/{request_id}/stream")
async def stream(
    request_id: uuid.UUID,
    request: Request,
    token: Optional[str] = None,
) -> StreamingResponse:
    """Server-Sent Events stream of per-agent progress for an investigation.

    Accepts the bearer token via either the `Authorization` header OR a `?token=`
    query string (the latter is needed because `EventSource` in browsers cannot
    set custom headers).
    """
    auth = request.app.state.auth
    auth.principal(f"Bearer {token}" if token else request.headers.get("Authorization"))

    svc: InvestigationService = request.app.state.investigation_service

    async def event_gen() -> AsyncIterator[bytes]:
        # Replay any signals already published for this request
        bus = request.app.state.bus
        existing = bus.query_signals(investigation_request_id=str(request_id))
        if existing:
            yield b": initial replay\n\n"
            for s in existing:
                replay = {
                    "request_id": str(request_id),
                    "kind": "signal_replay",
                    "at": s.emitted_at.isoformat(),
                    "agent_id": s.source_agent,
                    "payload": {
                        "signal_id": str(s.id),
                        "signal_type": s.signal_type,
                        "confidence": s.confidence,
                        "below_threshold": s.below_threshold,
                        "evidence": s.evidence,
                        "emitted_at": s.emitted_at.isoformat(),
                    },
                }
                yield f"data: {json.dumps(replay, default=str)}\n\n".encode()
        # Replay non-signal progress events emitted before this subscriber
        # attached (state_changed, agent_started, agent_thought, etc.). This
        # is critical because /chat returns request_id *while* the background
        # task is already running, so the UI must catch up.
        for evt in svc.replay_events(request_id):
            if evt.kind == "signal":
                # already handled via query_signals above
                continue
            yield f"data: {evt.to_json()}\n\n".encode()
        # If the investigation is already done, close the stream immediately.
        req = svc.status(request_id)
        if req is not None and req.state in ("completed", "failed"):
            yield b"event: done\ndata: {}\n\n"
            return
        # Live tail (only when the run is still in progress)
        seen_replay_keys = {(e.kind, e.agent_id, e.at.isoformat()) for e in svc.replay_events(request_id)}
        async for evt in svc.stream(request_id):
            # The replay buffer already contains this event — skip duplicates
            # that the live subscriber would otherwise re-receive.
            key = (evt.kind, evt.agent_id, evt.at.isoformat())
            if key in seen_replay_keys:
                continue
            seen_replay_keys.add(key)
            if evt.kind == "completed":
                yield f"event: done\ndata: {evt.to_json()}\n\n".encode()
                return
            yield f"data: {evt.to_json()}\n\n".encode()
        yield b"event: done\ndata: {}\n\n"

    return StreamingResponse(
        event_gen(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )
