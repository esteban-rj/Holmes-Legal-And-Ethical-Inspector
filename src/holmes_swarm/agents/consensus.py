"""ConsensusAgent (FR-008, FR-031..FR-034) — LLM-driven synthesis.

Origin gating is still deterministic (we never want a stray LLM turn to lift
the guard). The LLM is invoked *only* to draft the ``summary`` of a
CriticalFraudAlert and to ensure the contributing agents form a coherent
fraud narrative.

It MAY call:

- ``read_blackboard`` to refresh its context window between batches.
- (No internet egress — enforced by construction: no http_client injected.)

When the LLM is unavailable, the alert summary falls back to a deterministic
template, preserving the SC-003/SC-004/SC-014 invariant set.
"""

from __future__ import annotations

import asyncio
from collections import defaultdict
from datetime import datetime, timezone

from ..blackboard.queue_bus import QueueBus
from ..blackboard.schema import CriticalFraudAlert, Signal
from ..blackboard.staleness import filter_eligible
from ..llm.base import LLMClient, Message, ToolSpec
from ._tools import read_blackboard_tool


class ConsensusAgent:
    id = "consensus"
    name = "Consensus / Alert Synthesiser"
    signal_type = "alerts"
    confidence_threshold = 0.0  # per-agent thresholds applied individually

    def __init__(
        self,
        *,
        bus: QueueBus,
        llm: LLMClient | None = None,
        staleness_window_seconds: int = 86400,
        poll_interval_seconds: float = 5.0,
        redact_arg_keys: tuple[str, ...] = ("body", "text", "phi", "narrative"),
    ) -> None:
        self.bus = bus
        self.llm = llm
        self.staleness_window = staleness_window_seconds
        self.poll_interval = poll_interval_seconds
        self._last_emission: dict[str, CriticalFraudAlert] = {}
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()
        self.emitted_count = 0

    # ---------- LLM-facing surfaces ----------

    def system_prompt(self) -> str:
        return (
            "You are the consensus / alert synthesiser of the Holmes swarm. "
            "You receive a set of investigation-origin signals already approved "
            "by origin gating. Draft a CONCISE, EVIDENCE-GROUNDED summary of "
            "the fraud pattern they describe. Length: 1-2 sentences. Be "
            "specific: name the pattern, the entity, and the strongest "
            "evidence. The summary MUST be written in Spanish. "
            "Do not speculate beyond the signals you were given."
        )

    def tools(self) -> list[ToolSpec]:
        if self.llm is None:
            return []
        return [
            read_blackboard_tool(
                bus=self.bus, redact_arg_keys=("body", "text", "phi", "narrative", "pqrs")
            )
        ]

    async def _summarise(self, signals: list[Signal]) -> str:
        if self.llm is None:
            return self._deterministic_summary(signals)
        # Build a compact, PHI-free rendering of the signals for the LLM.
        rendered = [
            {
                "entity_id": s.entity_id,
                "signal_type": s.signal_type,
                "source_agent": s.source_agent,
                "confidence": s.confidence,
                "evidence_keys": sorted((s.evidence or {}).keys()),
                "evidence_pattern": (s.evidence or {}).get("pattern"),
            }
            for s in signals
        ]
        messages = [
            Message(role="system", content=self.system_prompt()),
            Message(
                role="user",
                content=("Draft a fraud-alert summary for these signals:\n"
                         f"{rendered}\nReturn ONLY the summary, no prose."),
            ),
        ]
        try:
            resp = await self.llm.chat(messages, tools=[])
            text = (resp.text or "").strip()
            if 20 <= len(text) <= 600:
                return text
        except Exception:
            pass
        return self._deterministic_summary(signals)

    @staticmethod
    def _deterministic_summary(signals: list[Signal]) -> str:
        patterns = sorted({(s.evidence or {}).get("pattern", "unknown") for s in signals})
        agents = sorted({s.source_agent for s in signals})
        return (
            f"{len(signals)} qualifying signal(s) from {len(agents)} agent(s); "
            f"patterns: {', '.join(p for p in patterns if p)}"
        )

    # ---------- main entry point ----------

    async def evaluate_once(
        self, *, now: datetime | None = None
    ) -> list[CriticalFraudAlert]:
        now = now or datetime.now(timezone.utc)
        eligible = filter_eligible(self.bus.all_signals(), now, self.staleness_window)
        investigation_signals = [s for s in eligible if s.origin.get("kind") == "investigation"]
        qualifying = [s for s in investigation_signals if not s.below_threshold]

        groups: dict[tuple, list[Signal]] = defaultdict(list)
        for s in qualifying:
            key = (s.entity_id, s.origin["investigation_request_id"])
            groups[key].append(s)

        emitted: list[CriticalFraudAlert] = []
        for (entity_id, request_id), sigs in groups.items():
            last = self._last_emission.get(entity_id)
            if last is not None and str(last.investigation_request_id) == str(request_id):
                new_contrib = [s.id for s in sigs if s.id not in last.contributing_signal_ids]
                if not new_contrib:
                    continue
                summary = await self._summarise(sigs)
                alert = CriticalFraudAlert(
                    id=last.id,
                    entity_id=entity_id,
                    investigation_request_id=request_id,  # type: ignore[arg-type]
                    contributing_signal_ids=last.contributing_signal_ids + new_contrib,
                    contributing_agent_ids=sorted(
                        {s.source_agent for s in sigs} | set(last.contributing_agent_ids)
                    ),
                    summary=summary or f"Enriched: +{len(new_contrib)} signals",
                )
                self.bus.store_alert(alert)
                self._last_emission[entity_id] = alert
                emitted.append(alert)
                self.emitted_count += 1
            else:
                summary = await self._summarise(sigs)
                alert = CriticalFraudAlert(
                    entity_id=entity_id,
                    investigation_request_id=request_id,  # type: ignore[arg-type]
                    contributing_signal_ids=[s.id for s in sigs],
                    contributing_agent_ids=sorted({s.source_agent for s in sigs}),
                    summary=summary,
                )
                self.bus.store_alert(alert)
                self._last_emission[entity_id] = alert
                emitted.append(alert)
                self.emitted_count += 1
        return emitted

    async def run_forever(self) -> None:
        self._stop.clear()
        while not self._stop.is_set():
            try:
                await self.evaluate_once()
            except Exception:
                pass
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self.poll_interval)
            except asyncio.TimeoutError:
                pass

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self.run_forever())

    async def stop(self) -> None:
        self._stop.set()
        if self._task:
            try:
                await asyncio.wait_for(self._task, timeout=self.poll_interval + 1.0)
            except asyncio.TimeoutError:
                self._task.cancel()

    async def shutdown(self) -> None:
        await self.stop()
