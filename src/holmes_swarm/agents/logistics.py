"""LogisticsAgent (FR-005) — LLM-driven version.

The LLM decides whether a pair of events violates physical feasibility. Its
tool chest:

- ``fetch_url`` to call a routing API (OSRM, OpenRouteService) when the local
  haversine heuristic is not enough.
- ``web_search`` when it wants to corroborate a hospital location.

When the LLM returns no signals, the heuristic (haversine km /
25 km/h urban speed + 15 minute floor) is the authoritative fall-through,
preserving the SC-005 behavioural contract.
"""

from __future__ import annotations

from math import asin, cos, radians, sin, sqrt
from typing import Any

from ..blackboard.schema import InvestigationScope, Signal
from ..llm.base import LLMClient, ToolSpec
from ._runtime import make_signal, run_agent_loop
from ._tools import fetch_url_tool, web_search_tool
from .base import AgentRuntimeContext


class LogisticsAgent:
    id = "logistics"
    name = "Geo-temporal Forensics"
    signal_type = "physical"
    confidence_threshold = 0.6

    _URBAN_KMH = 25.0
    _DEFAULT_DISTANCE_KM = 30.0
    _MIN_FEASIBLE_MIN = 15.0

    def __init__(
        self,
        *,
        llm: LLMClient | None = None,
        http_client: Any | None = None,
        distance_provider: Any | None = None,
        explore_allowed_hosts: tuple[str, ...] = (),
    ) -> None:
        self.llm = llm
        self.http = http_client
        self._distance = distance_provider
        self._explore_allowed_hosts = tuple(explore_allowed_hosts)

    # ---------- LLM-facing surfaces ----------

    def system_prompt(self) -> str:
        return (
            "You are the Geo-temporal Forensics agent of the Holmes swarm. "
            "Given a chronological list of attendance events for a provider, "
            "detect physically impossible movements (the same provider at two "
            "distant locations within an infeasible travel window).\n\n"
            "Heuristic reference when you have no live data:\n"
            "- Minimum required travel minutes = max(15, distance_km / 25 * 60).\n"
            "- Trigger a signal when observed gap < 0.5 * minimum_required_minutes.\n\n"
            "Tools: use fetch_url to call a routing API (OSRM, OpenRouteService) "
            "when location data is precise enough to be worth a remote query; "
            "web_search otherwise. Emit a JSON verdict: "
            "{\"signals\":[{signal_type:'physical', confidence:[0,1], "
            "evidence:{pattern:'impossible_movement', from, to, "
            "distance_km, observed_minutes, minimum_required_minutes}}]}."
        )

    def tools(self) -> list[ToolSpec]:
        out: list[ToolSpec] = []
        if self.http is not None and self._explore_allowed_hosts:
            out.append(
                web_search_tool(http_client=self.http, allowed_host_patterns=self._explore_allowed_hosts)
            )
            out.append(
                fetch_url_tool(http_client=self.http, allowed_host_patterns=self._explore_allowed_hosts)
            )
        return out

    # ---------- deterministic helpers ----------

    @staticmethod
    def _haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
        if not (lat1 and lat2 and lon1 and lon2):
            return 0.0
        R = 6371.0
        dlat = radians(lat2 - lat1)
        dlon = radians(lon2 - lon1)
        a = sin(dlat / 2) ** 2 + cos(radians(lat1)) * cos(radians(lat2)) * sin(dlon / 2) ** 2
        return 2 * R * asin(sqrt(a))

    @staticmethod
    def _event_pair_minutes(a: dict[str, Any], b: dict[str, Any]) -> float | None:
        from datetime import datetime
        ta, tb = a.get("ts"), b.get("ts")
        if not (ta and tb):
            return None
        try:
            if isinstance(ta, str):
                ta = datetime.fromisoformat(ta.replace("Z", "+00:00"))
            if isinstance(tb, str):
                tb = datetime.fromisoformat(tb.replace("Z", "+00:00"))
            return (tb - ta).total_seconds() / 60.0
        except Exception:
            return None

    async def _deterministic_fallback(self, batch: dict[str, Any], scope: Any) -> list[Signal]:
        signals: list[Signal] = []
        entity_id = batch.get("entity_id") or (
            scope.target_entity_id if isinstance(scope, InvestigationScope) else None
        )
        if not entity_id:
            return signals
        events = batch.get("events", []) or []
        for i in range(len(events) - 1):
            a, b = events[i], events[i + 1]
            dt_min = self._event_pair_minutes(a, b)
            if dt_min is None or dt_min <= 0:
                continue
            loc_a, loc_b = a.get("location") or {}, b.get("location") or {}
            km = self._haversine_km(
                float(loc_a.get("lat", 0)),
                float(loc_a.get("lon", 0)),
                float(loc_b.get("lat", 0)),
                float(loc_b.get("lon", 0)),
            ) or self._DEFAULT_DISTANCE_KM
            min_required_min = max(self._MIN_FEASIBLE_MIN, (km / self._URBAN_KMH) * 60.0)
            if dt_min < min_required_min * 0.5:
                confidence = min(0.99, 0.6 + 0.4 * (1 - dt_min / min_required_min))
                signals.append(
                    make_signal(
                        agent_id=self.id,
                        signal_type=self.signal_type,
                        entity_id=entity_id,
                        confidence=confidence,
                        evidence={
                            "pattern": "impossible_movement",
                            "from": a.get("location"),
                            "to": b.get("location"),
                            "distance_km": round(km, 2),
                            "observed_minutes": round(dt_min, 2),
                            "minimum_required_minutes": round(min_required_min, 2),
                        },
                        scope=scope,
                        confidence_threshold=self.confidence_threshold,
                    )
                )
        return signals

    async def run(
        self, batch: Any, *, scope: Any = None, ctx: AgentRuntimeContext | None = None
    ) -> list[Signal]:
        if not isinstance(batch, dict):
            return []
        entity_id = batch.get("entity_id") or (
            scope.target_entity_id if isinstance(scope, InvestigationScope) else None
        )
        if not entity_id:
            return []

        llm = (ctx.llm if ctx else None) or self.llm
        tools = self.tools()
        sink = getattr(ctx, "thought_sink", None) if ctx else None

        if llm is not None:
            try:
                raw = await run_agent_loop(
                    llm=llm,
                    system_prompt=self.system_prompt(),
                    tools=tools,
                    batch=batch,
                    scope=scope,
                    chat_fn=_llm_chat_factory(llm),
                    ctx=ctx,
                )
            except Exception as exc:
                if sink is not None:
                    await sink.emit(
                        "note",
                        {
                            "message": (
                                f"LLM no disponible ({type(exc).__name__}: {exc}); "
                                "se aplicarán reglas deterministas (movimiento geo-temporal)."
                            )
                        },
                    )
                return await self._deterministic_fallback(batch, scope)
            signals = _verdict_to_signals(
                raw,
                agent_id=self.id,
                signal_type=self.signal_type,
                scope=scope,
                confidence_threshold=self.confidence_threshold,
                entity_id_fallback=entity_id,
            )
            if signals:
                return signals

        return await self._deterministic_fallback(batch, scope)

    async def shutdown(self) -> None:
        if self.http is not None:
            try:
                await self.http.aclose()
            except Exception:
                pass


def _llm_chat_factory(llm: LLMClient):
    async def chat(messages, tools=(), **kwargs):
        return await llm.chat(messages, tools=list(tools), **kwargs)

    return chat


def _verdict_to_signals(
    raw: list[dict[str, Any]],
    *,
    agent_id: str,
    signal_type: str,
    scope: Any,
    confidence_threshold: float,
    entity_id_fallback: str,
) -> list[Signal]:
    out: list[Signal] = []
    for v in raw:
        try:
            conf = max(0.0, min(1.0, float(v.get("confidence", 0.0))))
        except (TypeError, ValueError):
            conf = 0.0
        evidence = v.get("evidence") or {}
        if not isinstance(evidence, dict):
            evidence = {"raw_evidence": str(evidence)}
        sig_evidence = dict(evidence)
        sig_evidence.setdefault("reasoning_source", "llm")
        out.append(
            make_signal(
                agent_id=agent_id,
                signal_type=evidence.get("signal_type") or signal_type,  # type: ignore[arg-type]
                entity_id=entity_id_fallback,
                confidence=conf,
                evidence=sig_evidence,
                scope=scope,
                confidence_threshold=confidence_threshold,
            )
        )
    return out
