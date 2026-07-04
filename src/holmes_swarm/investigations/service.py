"""Investigation service (FR-024..FR-028, FR-030).

Coordinates the user-initiated investigation flow:
1. `submit(request, *, requester_id)` — validate, audit, build scope, run selected
   agents with `scope=InvestigationScope(...)`.
2. `status(request_id)` — current state of the request.
3. `report(request_id)` — compiled InvestigationReport.

Edge cases:
- If a selected agent fails, signals already produced remain on the Blackboard
   with origin `investigation:<request_id>` and may still emit alerts.
- Default timeout (FR-026) transitions the request to `failed` with a partial summary.

Progress events:
- During `_run` the service publishes `ProgressEvent`s on an in-process pub/sub
  (`subscribe(request_id)`) so a UI can render per-agent status in real time.
- Event kinds: `agent_started`, `agent_completed`, `agent_failed`, `signal`,
  `state_changed`, `completed`, `failed`.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from ..agents.base import AgentRuntimeContext, AgentUnavailableError
from ..agents.registry import AgentRegistry
from ..blackboard.queue_bus import QueueBus
from ..blackboard.schema import AuditLogEntry, PublishRejected, Signal
from ..config import Settings
from ..llm.base import LLMClient, ThoughtSink
from .audit import AuditLog
from .models import InvestigationReport, InvestigationRequest, InvestigationScope


@dataclass
class ProgressEvent:
    """An observable event emitted during an investigation run.

    Serialisable to JSON for SSE transport. `payload` is the per-kind data.
    """
    request_id: uuid.UUID
    # agent_started | agent_completed | agent_failed | signal | state_changed | completed | failed
    kind: str
    at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    agent_id: str | None = None
    payload: dict[str, Any] = field(default_factory=dict)

    def to_json(self) -> str:
        return json.dumps(
            {
                "request_id": str(self.request_id),
                "kind": self.kind,
                "at": self.at.isoformat(),
                "agent_id": self.agent_id,
                "payload": self.payload,
            },
            default=str,
        )


class _Subscriber:
    """Per-request asyncio.Queue of ProgressEvents."""

    def __init__(self) -> None:
        self.queue: asyncio.Queue[ProgressEvent] = asyncio.Queue()
        self.closed = False


def _summarise_batch(batch: dict[str, Any]) -> dict[str, Any]:
    """Compact overview of a per-agent input batch for the live UI feed."""
    out: dict[str, Any] = {"entity_id": batch.get("entity_id")}
    for key in (
        "contracts",
        "attendances",
        "procedures",
        "distances",
        "pqrs",
        "specialty",
        "services",
    ):
        if key in batch:
            v = batch[key]
            if isinstance(v, list):
                out[key] = f"{len(v)} elemento(s)"
            else:
                out[key] = short_val(v)
    return out


def short_val(v: Any, limit: int = 60) -> str:
    if v is None:
        return ""
    if isinstance(v, str):
        return v if len(v) <= limit else v[:limit] + "…"
    try:
        s = str(v)
    except Exception:
        return repr(v)[:limit]
    return s if len(s) <= limit else s[:limit] + "…"



class InvestigationService:
    def __init__(
        self,
        *,
        registry: AgentRegistry,
        bus: QueueBus,
        audit: AuditLog,
        settings: Settings,
        llm: LLMClient,
        data_loader: Any | None = None,
    ) -> None:
        self.registry = registry
        self.bus = bus
        self.audit = audit
        self.settings = settings
        self.llm = llm
        # data_loader: optional callable (entity_id) -> dict with keys per agent type
        self.data_loader = data_loader
        self._subscribers: dict[str, list[_Subscriber]] = {}
        self._sub_lock = asyncio.Lock() if False else None  # lazily created
        # Per-request ring buffer so subscribers that attach after the run
        # started (e.g. SSE opened right after /chat returns) still see the
        # earlier events. The UI depends on this: state_changed -> running,
        # agent_started, and any thoughts emitted before stream() is wired up
        # would otherwise be lost.
        self._replay: dict[str, list[ProgressEvent]] = {}
        self._replay_max = 500

    async def submit(
        self,
        *,
        requester_id: str,
        target_entity_id: str,
        agents: list[str] | None = None,
        scope: dict[str, Any] | None = None,
    ) -> InvestigationRequest:
        scope = scope or {}
        # Validate agent ids (if supplied)
        if agents:
            unknown = [a for a in agents if self.registry.get(a) is None]
            if unknown:
                raise ValueError(f"unknown agent ids: {unknown}")
        # Default = all enabled agents
        if not agents:
            agents = [a.id for a in self.registry.all()]
        # Exclude consensus from the run set
        agents = [a for a in agents if a != "consensus"]
        req = InvestigationRequest(
            requester_id=requester_id,
            target_entity_id=target_entity_id,
            agents=agents,
            scope=scope,
            state="queued",
        )
        self.audit.append(AuditLogEntry(
            actor=requester_id,
            action="investigation.submit",
            target_entity_id=target_entity_id,
            request_id=req.id,
        ))
        self.remember(req)
        # Announce queuing BEFORE starting the run so an SSE subscriber attached
        # at /investigations/{id}/stream gets the lifecycle in order.
        self._publish_event(
            req.id,
            "state_changed",
            payload={"state": "queued", "agents": agents, "target_entity_id": req.target_entity_id},
        )
        # Run in a background task so submit() returns immediately and the SSE
        # stream attached by the UI can consume the progress events as they
        # are produced. Previously _run blocked submit(), which meant the UI
        # never saw `agent_started`/`signal` events unless the subscriber was
        # attached BEFORE the chat response returned.
        self._run_in_background(req, agents)
        return req

    def _run_in_background(self, req: InvestigationRequest, agents: list[str]) -> None:
        loop = asyncio.get_event_loop()
        self._bg_tasks = getattr(self, "_bg_tasks", [])
        task = loop.create_task(self._safe_run(req, agents))
        self._bg_tasks.append(task)

    async def _safe_run(self, req: InvestigationRequest, agents: list[str]) -> None:
        try:
            await self._run(req, agents)
        except Exception as exc:  # noqa: BLE001 — never crash the swarm
            req.state = "failed"
            self._publish_event(
                req.id,
                "failed",
                payload={"reason": f"unhandled: {type(exc).__name__}: {exc}"},
            )
            self._close_subscribers(req.id)

    async def _run(self, req: InvestigationRequest, agents: list[str]) -> None:
        req.state = "running"
        self._publish_event(
            req.id,
            "state_changed",
            payload={
                "state": "running",
                "target_entity_id": req.target_entity_id,
                "agents": agents,
            },
        )
        investigation_scope = InvestigationScope(
            investigation_request_id=req.id,
            target_entity_id=req.target_entity_id,
            date_from=req.scope.get("date_from"),
            date_to=req.scope.get("date_to"),
            location=req.scope.get("location"),
            procedure=req.scope.get("procedure"),
            narrative=req.scope.get("narrative"),
        )
        timeout = self.settings.investigations.default_timeout_seconds
        # Build per-agent batches via data_loader if available
        async def _wrap(agent_id: str):
            agent = self.registry.get(agent_id)
            if agent is None:
                return agent_id, []
            batch = self._batch_for(agent_id, req.target_entity_id, investigation_scope)
            self._publish_event(
                req.id,
                "agent_started",
                agent_id=agent_id,
                payload={
                    "agent_name": agent.name,
                    "signal_type": agent.signal_type,
                    "confidence_threshold": agent.confidence_threshold,
                    # Surface which LLM is actually reasoning for this agent,
                    # including whether the thinking variant is active. Lets
                    # the UI confirm in real time that M3-thinking is in use.
                    "llm_model": getattr(self.llm, "active_model", None),
                    "llm_thinking": bool(getattr(self.llm, "thinking", False)),
                },
            )

            # Per-agent thought sink that forwards reasoning events to the SSE
            # stream. _publish_event appends each emission to the per-request
            # replay buffer so subscribers attaching after the agent starts
            # still see the early thoughts.
            thought_sink = ThoughtSink(
                sink=lambda kind, payload: self._on_agent_thought(
                    req.id, agent_id, agent.name, kind, payload
                ),
                agent_id=agent_id,
                agent_name=agent.name,
            )
            ctx = AgentRuntimeContext(
                llm=self.llm,
                thought_sink=thought_sink,
            )

            async def _on_note(msg: str) -> None:
                self._publish_event(
                    req.id,
                    "agent_thought",
                    agent_id=agent_id,
                    payload={"kind": "note", "agent_name": agent.name, "message": msg},
                )

            try:
                # Many agents will read ctx but ignore the tool loop; for those
                # we still want to capture the deterministic verdict.
                self._publish_event(
                    req.id,
                    "agent_thought",
                    agent_id=agent_id,
                    payload={
                        "kind": "input",
                        "agent_name": agent.name,
                        "batch_summary": _summarise_batch(batch),
                    },
                )
                sigs = await agent.run(batch, scope=investigation_scope, ctx=ctx)
            except TypeError:
                # Agents built before ctx= became part of the run() signature.
                sigs = await agent.run(batch, scope=investigation_scope)
            except Exception as exc:
                # Surface the LLM/network failure as a thought so the UI can
                # show what went wrong (otherwise the user just sees a red
                # `agent_failed` line with no context). `AgentUnavailableError`
                # is the contract for "this agent's LLM is not functional" —
                # the swarm degrades (other agents still publish), but no
                # coded/deterministic verdict stands in for the agent.
                is_llm_unavailable = isinstance(exc, AgentUnavailableError)
                reason = "llm_unavailable" if is_llm_unavailable else "agent_error"
                self._publish_event(
                    req.id,
                    "agent_thought",
                    agent_id=agent_id,
                    payload={
                        "kind": "error",
                        "reason": reason,
                        "agent_name": agent.name,
                        "message": f"Error durante la ejecución: {type(exc).__name__}: {exc}",
                    },
                )
                self._publish_event(
                    req.id,
                    "agent_failed",
                    agent_id=agent_id,
                    payload={
                        "error": str(exc),
                        "error_type": type(exc).__name__,
                        "reason": reason,
                    },
                )
                self.audit.append(AuditLogEntry(
                    actor="system",
                    action="agent.unavailable" if is_llm_unavailable else "agent.failed",
                    target_entity_id=req.target_entity_id,
                    request_id=req.id,
                    extra={"agent": agent_id, "error": str(exc), "reason": reason},
                ))
                return agent_id, []



        tasks = [asyncio.create_task(_wrap(aid)) for aid in agents]
        try:
            await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), timeout=timeout)
        except asyncio.TimeoutError:
            req.state = "failed"
            self._publish_event(
                req.id,
                "failed",
                payload={"reason": "timeout", "timeout_seconds": timeout},
            )
            self._finalise(req, agents, partial=True)
            self._close_subscribers(req.id)
            return
        # State transition happens inside _finalise; just publish the final event here
        self._finalise(req, agents, partial=False)
        # Final event with report summary
        rep = self.report(req.id)
        self._publish_event(req.id, "completed", payload={
            "report_id": str(rep.id) if rep else None,
            "summary": rep.summary if rep else "",
            "signal_count": len(rep.signal_ids) if rep else 0,
        })
        self._close_subscribers(req.id)

    async def _publish_signals(self, agent, signals: list[Signal], scope: InvestigationScope) -> list[Signal]:
        published: list[Signal] = []
        for s in signals:
            s.origin = {"kind": "investigation", "investigation_request_id": str(scope.investigation_request_id)}
            s.below_threshold = s.confidence < agent.confidence_threshold
            try:
                await self.bus.publish(s)
                published.append(s)
                self._publish_event(
                    scope.investigation_request_id,
                    "signal",
                    agent_id=agent.id,
                    payload={
                        "signal_id": str(s.id),
                        "signal_type": s.signal_type,
                        "confidence": s.confidence,
                        "below_threshold": s.below_threshold,
                        "evidence": s.evidence,
                        "emitted_at": s.emitted_at.isoformat(),
                    },
                )
            except PublishRejected:
                # validation/dedup; skip but continue
                pass
        return published

    def _batch_for(self, agent_id: str, entity_id: str, scope: InvestigationScope) -> dict[str, Any]:
        if self.data_loader is None:
            return {"entity_id": entity_id}
        try:
            data = self.data_loader(entity_id) or {}
        except Exception:
            data = {}
        data.setdefault("entity_id", entity_id)
        return data

    def _on_agent_thought(
        self,
        request_id: uuid.UUID,
        agent_id: str,
        agent_name: str,
        kind: str,
        payload: dict[str, Any],
    ) -> None:
        """Translate `llm.base` thought events into a UI-friendly ProgressEvent.

        The ThoughtSink wraps a sync callable (None-or-awaitable return). We
        only need to translate the structured payload from `llm.base` into a
        single `agent_thought` ProgressEvent that the UI can render in a live
        feed per agent.
        """
        if kind == "llm_step":
            step = payload.get("step")
            text = (payload.get("text") or "").strip()
            calls = payload.get("tool_calls") or []
            first_line = text.splitlines()[0] if text else ""
            suffix = ""
            if calls:
                names = ", ".join(c.get("name", "?") for c in calls)
                suffix = f" → herramientas: {names}"
            rendered = f"paso {(step or 0) + 1}: {first_line[:400]}{suffix}" if first_line else f"paso {(step or 0) + 1}{suffix}"
        elif kind == "thinking":
            # Chain-of-thought tokens from the thinking variant. Render
            # each line as a separate thought so the UI can show the
            # model's reasoning trace step-by-step instead of one giant
            # block.
            text = (payload.get("text") or "").strip()
            if not text:
                return
            for line in text.splitlines():
                line = line.strip()
                if not line:
                    continue
                self._publish_event(
                    request_id,
                    "agent_thought",
                    agent_id=agent_id,
                    payload={
                        "kind": "thinking",
                        "agent_name": agent_name,
                        "message": line[:600],
                    },
                )
            return
        elif kind == "tool_invoked":
            tool = payload.get("tool")
            args = payload.get("args_redacted") or {}
            snippet = ", ".join(f"{k}={short_val(v)}" for k, v in list(args.items())[:3])
            rendered = f"consultando «{tool}» ({snippet})"
        elif kind == "tool_succeeded":
            rendered = f"«{payload.get('tool')}» devolvió datos"
        elif kind == "tool_failed":
            rendered = f"«{payload.get('tool')}» falló: {payload.get('error') or payload.get('error_type') or 'error'}"
        else:
            rendered = (payload.get("message") or str(payload))[:400]

        self._publish_event(
            request_id,
            "agent_thought",
            agent_id=agent_id,
            payload={
                "kind": kind,
                "agent_name": agent_name,
                "message": rendered,
                "raw_text": payload.get("text"),
            },
        )# ---------- Progress pub/sub ----------

    def _publish_event(
        self,
        request_id: uuid.UUID,
        kind: str,
        *,
        agent_id: str | None = None,
        payload: dict[str, Any] | None = None,
    ) -> None:
        evt = ProgressEvent(
            request_id=request_id,
            kind=kind,
            agent_id=agent_id,
            payload=payload or {},
        )
        # Append to replay buffer so late subscribers see this event too.
        key = str(request_id)
        buf = self._replay.setdefault(key, [])
        buf.append(evt)
        if len(buf) > self._replay_max:
            del buf[: len(buf) - self._replay_max]
        for sub in list(self._subscribers.get(key, [])):
            try:
                sub.queue.put_nowait(evt)
            except asyncio.QueueFull:
                pass

    def _replay_events(self, request_id: uuid.UUID) -> list[ProgressEvent]:
        """Snapshot of every event emitted so far for this request.

        Includes state changes, agent lifecycle, signal events, and per-step
        agent_thought events. Replayed in order to a newly attached SSE
        subscriber so the UI catches up before live-tailing.
        """
        return list(self._replay.get(str(request_id), []))

    def replay_events(self, request_id: uuid.UUID) -> list[ProgressEvent]:
        """Public API used by the SSE endpoint."""
        return self._replay_events(request_id)

    async def subscribe(self, request_id: uuid.UUID) -> _Subscriber:
        sub = _Subscriber()
        self._subscribers.setdefault(str(request_id), []).append(sub)
        return sub

    def unsubscribe(self, request_id: uuid.UUID, sub: _Subscriber) -> None:
        subs = self._subscribers.get(str(request_id), [])
        if sub in subs:
            subs.remove(sub)

    def _close_subscribers(self, request_id: uuid.UUID) -> None:
        for sub in list(self._subscribers.get(str(request_id), [])):
            sub.closed = True
        self._subscribers.pop(str(request_id), None)

    async def stream(self, request_id: uuid.UUID) -> AsyncIterator[ProgressEvent]:
        """Async-iterate over ProgressEvents for a request. Replays past events from
        `_run` only via subscribers attached before/during the run; new subscribers
        attached after completion will receive no events (the run is done)."""
        sub = await self.subscribe(request_id)
        try:
            while True:
                if sub.closed and sub.queue.empty():
                    return
                try:
                    evt = await asyncio.wait_for(sub.queue.get(), timeout=30.0)
                except asyncio.TimeoutError:
                    # heartbeat
                    yield ProgressEvent(request_id=request_id, kind="heartbeat")
                    continue
                yield evt
        finally:
            self.unsubscribe(request_id, sub)

    def _finalise(self, req: InvestigationRequest, agents: list[str], *, partial: bool) -> None:
        signal_ids = [
            s.id for s in self.bus.query_signals(
                entity_id=req.target_entity_id,
                investigation_request_id=str(req.id),
            )
        ]
        agents_ran = list(agents)
        if req.state != "failed":
            req.state = "completed" if signal_ids else ("failed" if partial else "completed")
        summary = (
            f"{len(signal_ids)} signal(s) from {len(agents_ran)} agent(s)"
            + (" [partial]" if partial else "")
        )
        report = InvestigationReport(
            request_id=req.id,
            target_entity_id=req.target_entity_id,
            agents_ran=agents_ran,
            signal_ids=signal_ids,
            summary=summary,
        )
        req.report_id = report.id
        self.audit.append(AuditLogEntry(
            actor="system",
            action="investigation.complete",
            target_entity_id=req.target_entity_id,
            request_id=req.id,
            report_id=report.id,
            extra={"partial": partial},
        ))
        # store report on bus for retrieval
        self._reports = getattr(self, "_reports", {})
        self._reports[str(report.id)] = report
        self._reports_by_request = getattr(self, "_reports_by_request", {})
        self._reports_by_request[str(req.id)] = report

    def status(self, request_id: uuid.UUID) -> InvestigationRequest | None:
        return self._requests.get(str(request_id)) if hasattr(self, "_requests") else None

    def report(self, request_id: uuid.UUID) -> InvestigationReport | None:
        rep_by_req = getattr(self, "_reports_by_request", {})
        return rep_by_req.get(str(request_id))

    def remember(self, req: InvestigationRequest) -> None:
        self._requests = getattr(self, "_requests", {})
        self._requests[str(req.id)] = req
