"""Blackboard implementation over asyncio.Queue (per topic).

FR-001: shared Blackboard/Event Bus. Agents MUST NOT communicate directly.
FR-014: continue operating when a single data source or single agent is unavailable.
FR-015: validate every signal against the schema at publish time.
FR-035: signals (both origins) remain queryable.
FR-008/FR-033: origin gating enforced at the alert-store write path (defense in depth).
"""

from __future__ import annotations

import asyncio
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any, AsyncIterator, Dict, Iterable, List, Optional, Protocol

from pydantic import ValidationError

from .dedup import Deduper
from .schema import (
    CriticalFraudAlert,
    Entity,
    PublishRejected,
    Signal,
)
from .staleness import filter_eligible, is_stale


class Blackboard(Protocol):
    async def publish(self, signal: Signal) -> None: ...
    def subscribe(self, topic: str) -> "Subscription": ...
    def store_signal(self, signal: Signal) -> None: ...
    def query_signals(
        self,
        *,
        entity_id: Optional[str] = None,
        origin_kind: Optional[str] = None,
        investigation_request_id: Optional[str] = None,
        since: Optional[datetime] = None,
        until: Optional[datetime] = None,
        limit: int = 100,
        offset: int = 0,
    ) -> List[Signal]: ...
    def store_alert(self, alert: CriticalFraudAlert) -> None: ...
    def list_alerts(self, *, entity_id: Optional[str] = None) -> List[CriticalFraudAlert]: ...
    def get_alert(self, alert_id: str) -> Optional[CriticalFraudAlert]: ...
    def get_entity(self, entity_id: str) -> Optional[Entity]: ...


class Subscription:
    """A consumer's per-topic asyncio.Queue with an iterator API."""

    def __init__(self, topic: str, bus: "QueueBus") -> None:
        self.topic = topic
        self._bus = bus
        self._queue: Optional[asyncio.Queue] = None
        self._bus._register_subscription(self)

    def _ensure_queue(self) -> asyncio.Queue:
        if self._queue is None:
            self._queue = asyncio.Queue()
        return self._queue

    async def __aenter__(self) -> "Subscription":
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        self._bus._unregister_subscription(self)

    async def get(self) -> Any:
        return await self._ensure_queue().get()

    def __aiter__(self) -> AsyncIterator[Any]:
        return self

    async def __anext__(self) -> Any:
        return await self._ensure_queue().get()

    def push(self, item: Any) -> None:
        try:
            self._ensure_queue().put_nowait(item)
        except asyncio.QueueFull:
            # v1 has no backpressure policy beyond drop-oldest; for prototype we drop newest.
            pass


class QueueBus:
    """In-memory Blackboard implementation."""

    def __init__(self, *, dedup_window_seconds: int = 60) -> None:
        self.dedup = Deduper(dedup_window_seconds)
        self._signals: List[Signal] = []
        self._alerts: Dict[str, CriticalFraudAlert] = {}
        self._entities: Dict[str, Entity] = {}
        self._subscriptions: Dict[str, List[Subscription]] = defaultdict(list)
        self._lock: Optional[asyncio.Lock] = None

    def _get_lock(self) -> asyncio.Lock:
        if self._lock is None:
            self._lock = asyncio.Lock()
        return self._lock

    # ---------- Blackboard protocol ----------

    async def publish(self, signal: Signal) -> None:
        # FR-015: validate schema at publish time. Schema validation already happened
        # at Signal construction; we re-validate to defend against direct construction.
        try:
            Signal.model_validate(signal.model_dump())
        except ValidationError as exc:
            raise PublishRejected("validation", str(exc)) from exc

        # FR-012: dedup
        if not self.dedup.accept(signal):
            raise PublishRejected("duplicate_dropped", "dedup window")

        # Persist + emit to subscribers
        self.store_signal(signal)
        for sub in list(self._subscriptions.get(signal.signal_type, [])):
            sub.push(signal)

    def store_signal(self, signal: Signal) -> None:
        self._signals.append(signal)
        # auto-create entity if missing
        if signal.entity_id not in self._entities:
            self._entities[signal.entity_id] = Entity(id=signal.entity_id)

    def subscribe(self, topic: str) -> Subscription:
        return Subscription(topic, self)

    def query_signals(
        self,
        *,
        entity_id: Optional[str] = None,
        origin_kind: Optional[str] = None,
        investigation_request_id: Optional[str] = None,
        since: Optional[datetime] = None,
        until: Optional[datetime] = None,
        limit: int = 100,
        offset: int = 0,
    ) -> List[Signal]:
        out: List[Signal] = []
        for s in self._signals:
            if entity_id is not None and s.entity_id != entity_id:
                continue
            if origin_kind is not None and s.origin.get("kind") != origin_kind:
                continue
            if investigation_request_id is not None:
                if s.origin.get("kind") != "investigation":
                    continue
                if str(s.origin.get("investigation_request_id")) != str(investigation_request_id):
                    continue
            if since is not None and s.emitted_at < since:
                continue
            if until is not None and s.emitted_at > until:
                continue
            out.append(s)
        return out[offset : offset + limit]

    def store_alert(self, alert: CriticalFraudAlert) -> None:
        # Defense in depth for FR-008 / FR-033 / FR-034: reject writes that lack an
        # investigation origin (should be impossible — Consensus Agent only emits
        # investigation-origin alerts — but the alert store enforces it as a backstop).
        if alert.investigation_request_id is None:
            raise PublishRejected(
                "validation",
                "CriticalFraudAlert requires an investigation_request_id (FR-034)",
            )
        self._alerts[str(alert.id)] = alert
        for sub in list(self._subscriptions.get("alerts", [])):
            sub.push(alert)

    def list_alerts(self, *, entity_id: Optional[str] = None) -> List[CriticalFraudAlert]:
        out = list(self._alerts.values())
        if entity_id is not None:
            out = [a for a in out if a.entity_id == entity_id]
        return out

    def get_alert(self, alert_id: str) -> Optional[CriticalFraudAlert]:
        return self._alerts.get(alert_id)

    def get_entity(self, entity_id: str) -> Optional[Entity]:
        return self._entities.get(entity_id)

    # ---------- internal ----------

    def _register_subscription(self, sub: Subscription) -> None:
        self._subscriptions[sub.topic].append(sub)

    def _unregister_subscription(self, sub: Subscription) -> None:
        try:
            self._subscriptions[sub.topic].remove(sub)
        except ValueError:
            pass

    # ---------- helpers used by tests/integration ----------

    def all_signals(self) -> List[Signal]:
        return list(self._signals)

    def eligible_signals(self, *, now: Optional[datetime] = None, window_seconds: int = 86400) -> List[Signal]:
        now = now or datetime.now(timezone.utc)
        return filter_eligible(self._signals, now, window_seconds)
