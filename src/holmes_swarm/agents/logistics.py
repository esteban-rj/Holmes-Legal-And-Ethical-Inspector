"""LogisticsAgent (FR-005) — LLM-driven.

The LLM decides whether a pair of events violates physical feasibility. Its
tool chest:

- ``fetch_url`` to call a routing API (OSRM, OpenRouteService) when the local
  haversine heuristic is not enough.
- ``web_search`` when it wants to corroborate a hospital location.

The LLM is the only judge. If the LLM is unavailable or returns no verdict
the agent raises :class:`AgentUnavailableError` — it MUST NOT silently fall
back to a coded haversine heuristic. The haversine / 25 km/h formula is
exposed inside the agent's system prompt so the LLM can apply it explicitly
when reasoning.
"""

from __future__ import annotations

import logging
from typing import Any
from urllib.parse import quote

from ..blackboard.schema import InvestigationScope, Signal
from ..llm.base import LLMClient, ToolSpec
from ._runtime import emit_conclusion, make_signal, run_agent_loop
from ._tools import UNRESTRICTED_WEB_PATTERN, fetch_url_tool, web_search_tool
from .base import AgentRuntimeContext, AgentUnavailableError

_log = logging.getLogger(__name__)


def _nominatim_search_url(query: str) -> str:
    """Build a Nominatim ``/search`` URL for a free-form place query.

    Nominatim is the OpenStreetMap geocoder; ``nominatim.openstreetmap.org``
    is already in this agent's ``explore_allowed_hosts``, so the resulting
    URL passes the allow-list (no ``BlockedHostError``). The endpoint
    returns JSON, which ``web_search_tool`` parses into ``{title, url}``
    entries pointing back at the same host so the LLM can ``fetch_url``
    individual results if it needs more detail.
    """
    return (
        "https://nominatim.openstreetmap.org/search?"
        f"q={quote(query)}&format=json&addressdetails=1&limit=5"
    )


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
        explore_allowed_hosts: tuple[str, ...] = (),
        unrestricted_web: bool = False,
    ) -> None:
        self.llm = llm
        self.http = http_client
        self._explore_allowed_hosts = tuple(explore_allowed_hosts)
        self._unrestricted_web = bool(unrestricted_web)

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
            "use web_search to geocode a place name via Nominatim "
            "(e.g. a hospital name) when only textual addresses are available.\n\n"
            "When you have reached a final verdict, respond ONLY with JSON "
            "matching {\"verdict\": 'suspicious'|'inconclusive'|'no_findings', "
            "\"confidence\": float in [0,1], \"summary\": \"<MUST be written in Spanish>\"}. "
            "The summary is what the human will read in the chat — name the movement pair, "
            "the distance, the observed vs minimum required minutes and your "
            "final decision, always in Spanish. Aim for around 100 words, but "
            "use more if the evidence requires it."
        )

    def tools(self) -> list[ToolSpec]:
        out: list[ToolSpec] = []
        patterns: tuple[str, ...] = (
            UNRESTRICTED_WEB_PATTERN if self._unrestricted_web else self._explore_allowed_hosts
        )
        if self.http is not None and patterns:
            out.append(
                web_search_tool(
                    http_client=self.http,
                    allowed_host_patterns=patterns,
                    url_builder=_nominatim_search_url,
                )
            )
            out.append(
                fetch_url_tool(http_client=self.http, allowed_host_patterns=patterns)
            )
        return out

    # ---------- main entry point ----------

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
        if llm is None:
            raise AgentUnavailableError(
                f"agent={self.id} has no LLM configured; cannot reason about movements"
            )

        tools = self.tools()
        sink = getattr(ctx, "thought_sink", None) if ctx else None

        try:
            raw, conclusion = await run_agent_loop(
                llm=llm,
                system_prompt=self.system_prompt(),
                tools=tools,
                batch=batch,
                scope=scope,
                chat_fn=_llm_chat_factory(llm),
                ctx=ctx,
            )
        except AgentUnavailableError:
            raise
        except Exception as exc:
            _log.error(
                "agent.unavailable",
                extra={
                    "agent": self.id,
                    "model": getattr(llm, "active_model", None),
                    "error_type": type(exc).__name__,
                    "error": str(exc),
                    "reason": "llm_call_failed",
                },
            )
            if sink is not None:
                try:
                    await sink.emit(
                        "error",
                        {
                            "agent": self.name,
                            "reason": "llm_unavailable",
                            "error_type": type(exc).__name__,
                            "message": (
                                f"LLM no disponible ({type(exc).__name__}: {exc}). "
                                "Sin LLM funcional no se emiten veredictos."
                            ),
                        },
                    )
                except Exception:
                    pass
            raise AgentUnavailableError(
                f"logistics agent: LLM call failed ({type(exc).__name__}): {exc}"
            ) from exc

        await emit_conclusion(sink, conclusion)
        signals = _verdict_to_signals(
            raw,
            agent_id=self.id,
            signal_type=self.signal_type,
            scope=scope,
            confidence_threshold=self.confidence_threshold,
            entity_id_fallback=entity_id,
        )
        if not signals:
            return []
        return signals

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
