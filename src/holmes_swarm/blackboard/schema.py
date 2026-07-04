"""Central schema (pydantic v2) for Entity, Signal, Alert, InvestigationRequest, etc.

FR-002: Every signal published on the Blackboard MUST conform to a documented schema
that includes: target entity identifier, signal type, source agent, confidence score,
supporting evidence reference, and emission timestamp.

FR-031: Every Signal MUST carry an `origin` attribute. Allowed values:
  - `autonomous-monitoring`
  - `investigation:<investigation_request_id>`
The Blackboard MUST reject any signal with a missing or unknown origin value.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Literal, Optional, Union

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from ..config import InternetProfile


SignalType = Literal["financial", "physical", "clinical", "operational"]


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _uuid() -> uuid.UUID:
    return uuid.uuid4()


# ---------- Entity ----------

class Entity(BaseModel):
    """Subject of investigation. Stable id (tax ID for providers, professional license for individuals).

    Spec assumption: a unique stable identifier is available for every entity referenced by signals;
    name-based matching is NOT relied upon.
    """
    model_config = ConfigDict(extra="forbid")
    id: str = Field(..., min_length=1, description="Stable entity identifier (tax ID or professional license).")
    type: Literal["provider", "individual"] = "provider"
    display_name: str = ""
    created_at: datetime = Field(default_factory=_utcnow)


# ---------- Origin (FR-031 discriminated union) ----------

class OriginAutonomous(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["autonomous-monitoring"] = "autonomous-monitoring"


class OriginInvestigation(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["investigation"] = "investigation"
    investigation_request_id: uuid.UUID


Origin = Union[OriginAutonomous, OriginInvestigation]
ORIGIN_KINDS = ("autonomous-monitoring", "investigation")


# ---------- Evidence reference ----------

class EvidenceReference(BaseModel):
    """Opaque payload owned by the source agent. Blackboard does not interpret it."""
    model_config = ConfigDict(extra="allow")
    pass


# ---------- Signal ----------

class Signal(BaseModel):
    """A single observation emitted by an agent.

    FR-002 + FR-015 + FR-031: validation at publish time, origin required.
    """
    model_config = ConfigDict(extra="forbid")
    id: uuid.UUID = Field(default_factory=_uuid)
    entity_id: str = Field(..., min_length=1)
    signal_type: SignalType
    source_agent: str = Field(..., min_length=1)
    confidence: float = Field(..., ge=0.0, le=1.0)
    evidence: Dict[str, Any] = Field(default_factory=dict)
    emitted_at: datetime = Field(default_factory=_utcnow)
    origin: Dict[str, Any] = Field(...)  # validated below
    below_threshold: bool = False

    @field_validator("origin")
    @classmethod
    def _validate_origin(cls, v: Any) -> Dict[str, Any]:
        if not isinstance(v, dict):
            raise ValueError("origin must be an object")
        kind = v.get("kind")
        if kind == "autonomous-monitoring":
            return {"kind": "autonomous-monitoring"}
        if kind == "investigation":
            rid = v.get("investigation_request_id")
            if not rid:
                raise ValueError("investigation_request_id required when origin.kind=='investigation'")
            try:
                uuid.UUID(str(rid))
            except (ValueError, TypeError):
                raise ValueError("investigation_request_id must be a UUID")
            return {"kind": "investigation", "investigation_request_id": str(rid)}
        raise ValueError(f"unknown origin kind: {kind!r}; allowed: {ORIGIN_KINDS}")


# ---------- Agent ----------

class Agent(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str
    name: str
    signal_type: SignalType
    enabled: bool = True
    confidence_threshold: float = Field(default=0.7, ge=0.0, le=1.0)
    internet_profile: InternetProfile = Field(default_factory=InternetProfile)


# ---------- Investigation request / report / scope ----------

InvestigationState = Literal[
    "queued", "running", "awaiting-external-data", "completed", "failed"
]


class InvestigationScope(BaseModel):
    """Optional scope of an investigation request. Passed to agents as `scope`."""
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
    requester_id: str = Field(..., min_length=1)
    target_entity_id: str = Field(..., min_length=1)
    agents: Optional[List[str]] = None  # None = all enabled agents
    scope: Dict[str, Any] = Field(default_factory=dict)
    submitted_at: datetime = Field(default_factory=_utcnow)
    state: InvestigationState = "queued"
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


# ---------- Critical Fraud Alert (FR-008 / FR-034) ----------

class CriticalFraudAlert(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: uuid.UUID = Field(default_factory=_uuid)
    entity_id: str
    emitted_at: datetime = Field(default_factory=_utcnow)
    investigation_request_id: uuid.UUID  # REQUIRED: no alert without a case (FR-034)
    contributing_signal_ids: List[uuid.UUID] = Field(default_factory=list)
    contributing_agent_ids: List[str] = Field(default_factory=list)
    summary: str = ""


# ---------- Audit log entry (FR-030) ----------

AuditAction = Literal[
    "investigation.submit",
    "investigation.complete",
    "alert.emit",
    "auth.rejected",
]


class AuditLogEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: uuid.UUID = Field(default_factory=_uuid)
    at: datetime = Field(default_factory=_utcnow)
    actor: str
    action: AuditAction
    target_entity_id: Optional[str] = None
    request_id: Optional[uuid.UUID] = None
    report_id: Optional[uuid.UUID] = None
    alert_id: Optional[uuid.UUID] = None
    extra: Dict[str, Any] = Field(default_factory=dict)


# ---------- Errors ----------

class PublishRejected(Exception):
    """Raised by Blackboard.publish when a signal is rejected."""

    def __init__(self, reason: str, message: str = "") -> None:
        super().__init__(message or reason)
        self.reason = reason


class BlockedHostError(Exception):
    """Raised by the allow-listed httpx client when a non-allow-listed host is targeted."""
