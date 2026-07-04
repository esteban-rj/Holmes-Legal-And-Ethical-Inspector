"""MedicalAgent (FR-006) — LLM-driven, RAG-bound clinical coherence checker.

The LLM uses ``retriever_query`` to look up SOAT tariffs and clinical
guidelines (Hermetic, no PHI egress — FR-022 spirit kept even though air-gap
is no longer the global default).

The LLM is the only judge. If the LLM is unavailable or returns no verdict
the agent raises :class:`AgentUnavailableError` — it MUST NOT silently fall
back to a coded monthly-volume / specialty-mismatch heuristic. The formulas
live inside the system prompt so the LLM can apply them explicitly when
reasoning.
"""

from __future__ import annotations

import logging
from typing import Any

from ..blackboard.schema import InvestigationScope, Signal
from ..llm.base import LLMClient, ToolSpec
from ..rag.base import Retriever
from ._runtime import make_signal, run_agent_loop
from ._tools import retriever_query_tool
from .base import AgentRuntimeContext, AgentUnavailableError

_log = logging.getLogger(__name__)


class MedicalAgent:
    id = "medical"
    name = "Clinical Coherence"
    signal_type = "clinical"
    confidence_threshold = 0.75

    def __init__(
        self,
        *,
        llm: LLMClient | None = None,
        retriever: Retriever | None = None,
        redact_arg_keys: tuple[str, ...] = ("body", "text", "phi"),
    ) -> None:
        self.llm = llm
        self.retriever = retriever
        self.http = None  # FR-019: no internet egress for clinical agent.
        self._redact_arg_keys = tuple(redact_arg_keys)

    # ---------- LLM-facing surfaces ----------

    def system_prompt(self) -> str:
        return (
            "You are the Clinical Coherence agent of the Holmes swarm. You "
            "analyse a batch of clinical procedures for plausibility:\n"
            "- implausible monthly volume for a specialty,\n"
            "- procedure profile that is mismatched with the entity's "
            "registered service line.\n"
            "You MUST consult the local RAG knowledge base (SOAT / ISS "
            "guidelines) via retriever_query before emitting a verdict.\n"
            "Respond with JSON: {\"signals\":[{signal_type:'clinical', "
            "confidence:[0,1], evidence:{pattern, procedure_code, "
            "monthly_volume?, specialty?, service?, tariff_source}}]}."
        )

    def tools(self) -> list[ToolSpec]:
        if self.retriever is None:
            return []
        return [
            retriever_query_tool(
                retriever=self.retriever, redact_arg_keys=self._redact_arg_keys
            )
        ]

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
                f"agent={self.id} has no LLM configured; cannot reason about clinical coherence"
            )

        sink = getattr(ctx, "thought_sink", None) if ctx else None

        try:
            raw = await run_agent_loop(
                llm=llm,
                system_prompt=self.system_prompt(),
                tools=self.tools(),
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
                f"medical agent: LLM call failed ({type(exc).__name__}): {exc}"
            ) from exc

        signals = _verdict_to_signals(
            raw,
            agent_id=self.id,
            signal_type=self.signal_type,
            scope=scope,
            confidence_threshold=self.confidence_threshold,
            entity_id_fallback=entity_id,
        )
        if not signals:
            raise AgentUnavailableError(
                f"medical agent: LLM returned no signals for entity={entity_id}"
            )
        return signals

    async def shutdown(self) -> None:
        return None


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
        sig_evidence.setdefault("reasoning_source", "llm+rag")
        out.append(
            make_signal(
                agent_id=agent_id,
                signal_type=signal_type,  # Clinical always 'clinical'
                entity_id=entity_id_fallback,
                confidence=conf,
                evidence=sig_evidence,
                scope=scope,
                confidence_threshold=confidence_threshold,
            )
        )
    return out
