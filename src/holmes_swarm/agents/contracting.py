"""ContractingAgent (FR-004) — LLM-driven.

The agent reasons about the contract batch via the LLM. It MAY call:

- ``retriever_query`` to consult local SECOP historic reference (when configured).
- ``web_search``  / ``fetch_url`` to pull live SECOP data when the batch references
  a procedure code that needs an external price percentile.

The LLM is the only judge. If the LLM is unavailable or returns no verdict
the agent raises :class:`AgentUnavailableError` — it MUST NOT silently fall
back to a coded SECOP heuristic. Coded rules live only in the RAG corpus
(``corpora/tariffs_soat.md``) and are consulted via ``retriever_query``,
not executed inline.
"""

from __future__ import annotations

import logging
from typing import Any

from ..blackboard.schema import InvestigationScope, Signal
from ..data_sources.secop import SECOPSource
from ..llm.base import LLMClient, ToolSpec
from ._runtime import emit_conclusion, make_signal, run_agent_loop
from ._tools import fetch_url_tool, retriever_query_tool, web_search_tool
from .base import AgentRuntimeContext, AgentUnavailableError

_log = logging.getLogger(__name__)


class ContractingAgent:
    id = "contracting"
    name = "Contracting Auditor"
    signal_type = "financial"
    confidence_threshold = 0.7

    def __init__(
        self,
        *,
        llm: LLMClient | None = None,
        http_client: Any | None = None,
        retriever: Any | None = None,
        explore_allowed_hosts: tuple[str, ...] = (),
        # Legacy knobs — accepted but unused. The agent no longer executes
        # coded SECOP / reference-price rules inline; the LLM is the only
        # judge and reaches tariff data via retriever_query (RAG corpora).
        secop_source: SECOPSource | None = None,
        secop_percentile: float = 0.25,
        secop_pull_limit: int = 50,
        reference_prices: dict[str, float] | None = None,
    ) -> None:
        self.llm = llm
        self.http = http_client
        self.retriever = retriever
        self._explore_allowed_hosts = tuple(explore_allowed_hosts)
        if secop_source is not None or reference_prices is not None:
            _log.warning(
                "contracting.legacy_knobs_ignored",
                extra={
                    "reason": "agent is LLM-only; pass tariff data via retriever_query / RAG corpora",
                },
            )

    # ---------- LLM-facing surfaces ----------

    def system_prompt(self) -> str:
        return (
            "You are the Contracting Auditor of the Holmes swarm. Your job is "
            "to detect monopolistic contracting patterns and abnormally low "
            "prices in a batch of public-health contracts.\n\n"
            "Patterns to look for:\n"
            "1. Monopoly: a single entity concentrates >=80% of contracts for "
            "the same procedure code (and n >= 2).\n"
            "2. Below reference: a contract price < 0.5 * the reference price "
            "for that procedure code (reference = local RAG percentile or "
            "SECOP fetched data, falling back to bundled reference_prices).\n"
            "3. Sanity checks against platform metadata (SECOP vs manual).\n\n"
            "Use retriever_query for the local reference table, web_search + "
            "fetch_url for live SECOP open data when needed.\n\n"
            "When you have reached a final verdict, respond ONLY with JSON "
            "matching {\"verdict\": 'suspicious'|'inconclusive'|'no_findings', "
            "\"confidence\": float in [0,1], \"summary\": \"...\"}. The summary "
            "is what the human will read in the chat — name the pattern, the "
            "entity, the strongest evidence, and your final decision in <=100 "
            "words. Do not exceed 100 words."
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
        if self.retriever is not None:
            out.append(
                retriever_query_tool(retriever=self.retriever, redact_arg_keys=("body", "text"))
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
                f"agent={self.id} has no LLM configured; cannot reason about contracts"
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
                f"contracting agent: LLM call failed ({type(exc).__name__}): {exc}"
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
            # The LLM reached a verdict (it produced a chat conclusion), so the
            # absence of structured signals is a benign "no_findings" outcome
            # rather than an LLM failure. Surface it as such and let the
            # investigation continue.
            await emit_conclusion(
                sink,
                {
                    "verdict": conclusion.get("verdict", "no_findings"),
                    "confidence": conclusion.get("confidence", 0.0),
                    "summary": conclusion.get("summary", "")
                    or "No se identificaron patrones de contratación sospechosos.",
                },
            )
            return []
        return signals

    async def shutdown(self) -> None:
        if self.http is not None:
            try:
                await self.http.aclose()
            except Exception:
                pass


# ---------- pure helpers (kept module-level for testability) ----------


def _llm_chat_factory(llm: LLMClient):
    """Adapter-bound chat fn used by the agentic loop."""

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
            conf = float(v.get("confidence", 0.0))
        except (TypeError, ValueError):
            conf = 0.0
        conf = max(0.0, min(1.0, conf))
        evidence = v.get("evidence") or {}
        if not isinstance(evidence, dict):
            evidence = {"raw_evidence": str(evidence)}
        sig_evidence = dict(evidence)
        sig_evidence.setdefault("reasoning_source", "llm")
        entity_id = (
            (evidence.get("entity_id") if isinstance(evidence, dict) else None)
            or entity_id_fallback
        )
        out.append(
            make_signal(
                agent_id=agent_id,
                signal_type=sig_evidence.get("signal_type") or signal_type,  # type: ignore[arg-type]
                entity_id=entity_id,
                confidence=conf,
                evidence=sig_evidence,
                scope=scope,
                confidence_threshold=confidence_threshold,
            )
        )
    return out
