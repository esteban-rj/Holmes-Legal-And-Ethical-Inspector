"""ContractingAgent (FR-004) — LLM-driven version.

The agent reasons about the contract batch via the LLM. It MAY call:

- ``retriever_query`` to consult local SECOP historic reference (when configured).
- ``web_search``  / ``fetch_url`` to pull live SECOP data when the batch references
  a procedure code that needs an external price percentile.

Deterministic fall-through: if the LLM returns ``no-op``, the agent falls back
to its built-in heuristic (monopoly share >= 0.8 with n >= 2, or price < 0.5 *
reference). The build in reference_prices keeps the legacy behaviour
exercisable through tests.
"""

from __future__ import annotations

import inspect
import logging
from typing import Any

from ..blackboard.schema import InvestigationScope, Signal
from ..data_sources.secop import SECOPSource
from ..llm.base import LLMClient, ToolSpec
from ._runtime import make_signal, run_agent_loop
from ._tools import fetch_url_tool, retriever_query_tool, web_search_tool
from .base import AgentRuntimeContext

_log = logging.getLogger(__name__)


async def _maybe_await(value):
    if inspect.isawaitable(value):
        return await value
    return value


class ContractingAgent:
    id = "contracting"
    name = "Contracting Auditor"
    signal_type = "financial"
    confidence_threshold = 0.7

    # Reference price history per procedure code (mock for v1; legacy fallback).
    _DEFAULT_REF: dict[str, float] = {
        "93010": 1_250_000.0,
        "93508": 4_800_000.0,
        "92920": 6_300_000.0,
    }

    def __init__(
        self,
        *,
        llm: LLMClient | None = None,
        http_client: Any | None = None,
        retriever: Any | None = None,
        reference_prices: dict[str, float] | None = None,
        secop_source: SECOPSource | None = None,
        secop_percentile: float = 0.25,
        secop_pull_limit: int = 50,
        explore_allowed_hosts: tuple[str, ...] = (),
    ) -> None:
        self.llm = llm
        self.http = http_client
        self.retriever = retriever
        self._ref = reference_prices or dict(self._DEFAULT_REF)
        self._secop: SECOPSource | None = secop_source
        self._secop_percentile = secop_percentile
        self._secop_pull_limit = secop_pull_limit
        self._explore_allowed_hosts = tuple(explore_allowed_hosts)

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
            "fetch_url for live SECOP open data when needed. Emit zero or more "
            "signals as a JSON object with shape "
            "{\"signals\":[{...}]}; each signal must carry signal_type "
            "'financial', a confidence in [0,1], and an evidence object with "
            "pattern, procedure_code, and any pertinent metrics."
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

    # ---------- legacy heuristic (fall-through) ----------

    async def _secop_ref_price(self, procedure_code: str) -> float | None:
        if self._secop is None:
            return None
        try:
            records = await _maybe_await(
                self._secop.fetch_for_entity(
                    entity_id="",
                    limit=self._secop_pull_limit,
                    procedure_code=procedure_code,
                )
            )
        except Exception as exc:  # noqa: BLE001
            _log.warning(
                "secop.fetch_failed",
                extra={"procedure_code": procedure_code, "error_type": type(exc).__name__},
            )
            return None
        if not records:
            try:
                records = await _maybe_await(
                    self._secop.fetch_for_entity(
                        entity_id="*",
                        limit=self._secop_pull_limit,
                        procedure_code=procedure_code,
                    )
                )
            except Exception as exc:  # noqa: BLE001
                _log.warning(
                    "secop.fetch_failed",
                    extra={"procedure_code": procedure_code, "error_type": type(exc).__name__},
                )
                records = []
        if not isinstance(records, list):
            return None
        prices = [r.price for r in records if r.price > 0]
        if len(prices) < 3:
            return None
        prices.sort()
        k = max(0, min(len(prices) - 1, int(self._secop_percentile * (len(prices) - 1))))
        return prices[k]  # type: ignore[return-value]

    async def _secop_ref_prices(self, codes: list[str]) -> dict[str, float]:
        out: dict[str, float] = {}
        for code in codes:
            v = await self._secop_ref_price(code)
            if v is not None and v > 0:
                out[code] = v
        return out

    async def _deterministic_fallback(self, batch: dict[str, Any], scope: Any) -> list[Signal]:
        signals: list[Signal] = []
        entity_id = batch.get("entity_id") or (
            scope.target_entity_id if isinstance(scope, InvestigationScope) else None
        )
        if not entity_id:
            return signals
        contracts = batch.get("contracts", []) or []
        if not contracts:
            return signals

        code_counts: dict[str, int] = {}
        for c in contracts:
            code = str(c.get("code", ""))
            if code:
                code_counts[code] = code_counts.get(code, 0) + 1
        total = sum(code_counts.values()) or 1
        for code, n in code_counts.items():
            share = n / total
            if share >= 0.8 and n >= 2:
                signals.append(
                    make_signal(
                        agent_id=self.id,
                        signal_type=self.signal_type,
                        entity_id=entity_id,
                        confidence=min(0.95, 0.6 + 0.3 * share),
                        evidence={"pattern": "monopoly", "procedure_code": code, "share": round(share, 3), "count": n},
                        scope=scope,
                        confidence_threshold=self.confidence_threshold,
                    )
                )
        secop_refs: dict[str, float] = {}
        if self._secop is not None:
            secop_refs = await self._secop_ref_prices(list(code_counts.keys()))
        for c in contracts:
            code = str(c.get("code", ""))
            price = float(c.get("price", 0) or 0)
            ref: float | None = secop_refs.get(code) or self._ref.get(code)
            if ref and price > 0 and price < ref * 0.5:
                signals.append(
                    make_signal(
                        agent_id=self.id,
                        signal_type=self.signal_type,
                        entity_id=entity_id,
                        confidence=min(0.95, 0.6 + 0.3 * (1 - price / ref)),
                        evidence={
                            "pattern": "below_reference_price",
                            "procedure_code": code,
                            "price": price,
                            "reference": ref,
                            "platform": c.get("platform", "SECOP"),
                        },
                        scope=scope,
                        confidence_threshold=self.confidence_threshold,
                    )
                )
        return signals

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
        tools = self.tools()
        sink = getattr(ctx, "thought_sink", None) if ctx else None

        # ---- LLM path ----
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
                                "se aplicarán reglas deterministas (precios SECOP)."
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

        # ---- deterministic fall-through ----
        return await self._deterministic_fallback(batch, scope)

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
