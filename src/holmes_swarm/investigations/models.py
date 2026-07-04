"""Investigation models (InvestigationRequest / Report / Scope)."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _uuid() -> uuid.UUID:
    return uuid.uuid4()


class InvestigationScope(BaseModel):
    """Optional scope passed to agents' `run(batch, *, scope=...)`."""
    model_config = ConfigDict(extra="forbid")
    investigation_request_id: uuid.UUID
    target_entity_id: str
    date_from: Optional[str] = None
    date_to: Optional[str] = None
    location: Optional[str] = None
    procedure: Optional[str] = None
    narrative: Optional[str] = None


class InvestigationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: uuid.UUID = Field(default_factory=_uuid)
    requester_id: str
    target_entity_id: str
    agents: Optional[List[str]] = None
    scope: Dict[str, Any] = Field(default_factory=dict)
    submitted_at: datetime = Field(default_factory=_utcnow)
    state: str = "queued"
    report_id: Optional[uuid.UUID] = None


class InvestigationReport(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: uuid.UUID = Field(default_factory=_uuid)
    request_id: uuid.UUID
    target_entity_id: str
    agents_ran: List[str] = Field(default_factory=list)
    signal_ids: List[uuid.UUID] = Field(default_factory=list)
    summary: str = ""
    emitted_at: datetime = Field(default_factory=_utcnow)
