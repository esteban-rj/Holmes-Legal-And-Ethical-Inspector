"""Agents endpoint: list registered agents and their config."""

from __future__ import annotations

from typing import Any, Dict, List

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel

from .investigations import _principal


router = APIRouter(prefix="/agents", tags=["agents"])


class AgentSummary(BaseModel):
    id: str
    name: str
    signal_type: str
    confidence_threshold: float
    enabled: bool


@router.get("", response_model=List[AgentSummary])
def list_agents(request: Request, _: str = Depends(_principal)) -> List[AgentSummary]:
    registry = request.app.state.registry
    settings = request.app.state.settings
    out: List[AgentSummary] = []
    for a in registry.all():
        cfg = settings.agents.get(a.id)
        enabled = bool(cfg.enabled) if cfg else True
        out.append(AgentSummary(
            id=a.id,
            name=a.name,
            signal_type=a.signal_type,
            confidence_threshold=a.confidence_threshold,
            enabled=enabled,
        ))
    return out