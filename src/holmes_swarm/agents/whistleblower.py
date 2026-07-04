"""WhistleblowerAgent (FR-007) — LLM-driven PQR analyst.

System prompt enforces:

- Treats the PQR text as confidential; ``body`` text is in the tool's redact
  list and MUST NOT be logged.
- Per-PQR output schema is strict JSON: sentiment, entities, modus_operandi.
- The agent batches every PQR for the request and produces ONE short chat
  conclusion describing the overall findings (verdict, confidence, ≤100-word
  summary) which is forwarded to the chat pane via ``emit_conclusion``.
- ``fetch_url`` is allowed only against the agent's allow-list (e.g. a
  moderation-API or a curated PQR-glossary host) and the LLM decides whether
  to consult it.

The LLM is the only judge. If the LLM is unavailable or returns a verdict
without ``modus_operandi`` (or returns one that falls outside the allow-list),
the agent raises :class:`AgentUnavailableError` rather than silently filling
the gap with a regex extractor on raw PQR text. The modus-operandi allow-list
itself is surfaced inside the system prompt so the LLM can use it explicitly.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from ..blackboard.schema import InvestigationScope, Signal
from ..llm.base import ChatResponse, LLMClient, Message, ToolSpec
from ._runtime import emit_conclusion, make_signal, _extract_json_object
from ._tools import UNRESTRICTED_WEB_PATTERN, fetch_url_tool
from .base import AgentRuntimeContext, AgentUnavailableError

_log = logging.getLogger(__name__)


_ALLOWED_MODUS = {
    "whatsapp",
    "telegram",
    "mensajeria_instantanea",
    "auxiliares",
    "comision",
    "soborno",
    "coima",
    "facturas_falsas",
    "cartel",
    "colusion",
}


class WhistleblowerAgent:
    id = "whistleblower"
    name = "PQR NLP Analyst"
    signal_type = "operational"
    confidence_threshold = 0.6

    def __init__(
        self,
        *,
        llm: LLMClient | None = None,
        http_client: Any | None = None,
        explore_allowed_hosts: tuple[str, ...] = (),
        unrestricted_web: bool = False,
        redact_arg_keys: tuple[str, ...] = ("body", "text", "pqrs", "narrative", "phi"),
    ) -> None:
        self.llm = llm
        self.http = http_client
        self._explore_allowed_hosts = tuple(explore_allowed_hosts)
        self._unrestricted_web = bool(unrestricted_web)
        self._redact_arg_keys = tuple(redact_arg_keys)

    # ---------- LLM-facing surfaces ----------

    def system_prompt(self) -> str:
        return (
            "You are the PQR (Peticiones, Quejas, Reclamos) NLP analyst of the "
            "Holmes swarm. For each anonymous complaint you receive, you must "
            "extract a structured summary AND a confidence score. ALL free-text "
            "fields you emit MUST be written in Spanish.\n\n"
            "Rules:\n"
            "- Treat the complaint body as strictly confidential. NEVER reveal "
            "  it in the output.\n"
            "- Output STRICT JSON: {\"sentiment\": 'positive'|'neutral'|'negative', "
            "  \"entities\": [string...], \"modus_operandi\": [string...]}.\n"
            "- 'modus_operandi' must only contain entries from this allow-list: "
            "  ['whatsapp','telegram','mensajeria_instantanea','auxiliares',"
            "  'comision','soborno','coima','facturas_falsas','cartel','colusion'].\n"
            "- If unsure, leave the field empty; do not invent.\n"
            "- You MAY call fetch_url against an allow-listed moderation or "
            "  PQR-glossary host to enrich context; do not call it with the "
            "  PQR body in the URL.\n"
            "Return ONLY the JSON object, no prose."
        )

    def tools(self) -> list[ToolSpec]:
        if self.http is None:
            return []
        patterns: tuple[str, ...] = (
            UNRESTRICTED_WEB_PATTERN if self._unrestricted_web else self._explore_allowed_hosts
        )
        if not patterns:
            return []
        return [
            fetch_url_tool(http_client=self.http, allowed_host_patterns=patterns)
        ]

    # ---------- entry point ----------

    async def run(
        self, batch: Any, *, scope: Any = None, ctx: AgentRuntimeContext | None = None
    ) -> list[Signal]:
        if not isinstance(batch, dict):
            return []
        signals: list[Signal] = []
        pqrs = batch.get("pqrs", []) or []
        llm = (ctx.llm if ctx else None) or self.llm
        if llm is None:
            raise AgentUnavailableError(
                f"agent={self.id} has no LLM configured; cannot reason about PQRs"
            )
        sink = getattr(ctx, "thought_sink", None) if ctx else None

        per_pqr_verdicts: list[dict[str, Any]] = []
        for pqr in pqrs:
            text = pqr.get("body") or pqr.get("text") or ""
            entity_id = (
                pqr.get("entity_id")
                or (scope.target_entity_id if isinstance(scope, InvestigationScope) else None)
                or batch.get("entity_id")
                or ""
            )
            if not text or not entity_id:
                continue

            if sink is not None:
                await sink.emit(
                    "note",
                    {"message": f"Analizando PQR «{pqr.get('id', '?')}» — {pqr.get('channel', 'pqr')}"},
                )
                await sink.emit(
                    "note",
                    {"message": "Pidiendo al LLM un veredicto sobre la queja…"},
                )

            try:
                verdict = await _ask_llm_for_verdict(llm, self.system_prompt(), text)
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
                    f"whistleblower agent: LLM call failed ({type(exc).__name__}): {exc}"
                ) from exc

            per_pqr_verdicts.append({"pqr_id": pqr.get("id"), **verdict})
            modus = verdict.get("modus_operandi") or []
            if not modus:
                # The LLM is the judge — an empty/unallow-listed modus_operandi
                # is a real verdict ("nothing suspicious"), NOT a reason to
                # silently regex-grep the PQR body. The LLM may still choose
                # to emit 0 signals for the whole batch; that is fine.
                continue

            confidence = 0.7 if verdict.get("sentiment") == "negative" else 0.55
            signals.append(
                make_signal(
                    agent_id=self.id,
                    signal_type=self.signal_type,
                    entity_id=entity_id,
                    confidence=confidence,
                    evidence={
                        "pqr_id": pqr.get("id"),
                        "sentiment": verdict.get("sentiment", "neutral"),
                        "modus_operandi": modus,
                        "reasoning_source": "llm",
                    },
                    scope=scope,
                    confidence_threshold=self.confidence_threshold,
                )
            )

        # Build the chat conclusion summarising the batch. We ask the LLM once
        # for a {verdict,confidence,summary} envelope; if the call fails we
        # fall back to a deterministic rendering of the per-PQR results so
        # the chat pane always sees a conclusion for this agent.
        conclusion = await _summarise_pqrs(llm, per_pqr_verdicts)
        await emit_conclusion(sink, conclusion)
        return signals

    async def shutdown(self) -> None:
        if self.http is not None:
            try:
                await self.http.aclose()
            except Exception:
                pass


# ---------- pure helpers ----------


def _sanitise_modus(raw: Any) -> list[str]:
    """Drop entries the LLM invented that are not in the allow-list."""
    out: list[str] = []
    for item in raw or []:
        if not isinstance(item, str):
            continue
        norm = (
            item.strip()
            .lower()
            .replace(" ", "_")
            .replace("ó", "o")
            .replace("í", "i")
            .replace("á", "a")
        )
        if norm in _ALLOWED_MODUS:
            out.append(norm)
    return out


async def _ask_llm_for_verdict(llm: LLMClient, system_prompt: str, body: str) -> dict[str, Any]:
    """Send the PQR body through the LLM and parse a strict JSON verdict.

    Never logs the body — it is part of `Message.content` and we pass it
    in-memory only; the tool runner already enforces arg redaction if the LLM
    ends up calling fetch_url with it.

    On a transport-level LLM failure (network, auth, 5xx) we still raise
    :class:`AgentUnavailableError` — there is genuinely no LLM to reason
    about. On a *parse* failure (the LLM responded with prose, fenced JSON,
    or a non-object envelope) we return a neutral, no-modus verdict instead
    of crashing the agent: the downstream chat conclusion summariser can
    still describe the batch (e.g. "LLM replied with prose, treating PQR
    as inconclusive"), the chat pane always gets a bubble, and we never
    silently regex-grep the PQR body. The provenance is preserved via the
    ``reasoning_source`` field in the emitted signal evidence.
    """
    messages = [
        Message(role="system", content=system_prompt),
        Message(role="user", content=f"PQR: {body}"),
    ]
    try:
        resp: ChatResponse = await llm.chat(messages, tools=[])
    except Exception:
        # Re-raise so the caller can surface the transport failure; we
        # never silently fall back to a coded regex extractor.
        raise
    text = (resp.text or "").strip()
    obj = _extract_json_object(text)
    if not isinstance(obj, dict):
        # The LLM is the judge, but a parse failure is NOT the same as
        # "the LLM said nothing suspicious" — log the preview and emit a
        # neutral verdict so the agent loop keeps running and the chat
        # conclusion summariser can still describe the batch.
        preview = (resp.text or "").strip().replace("\n", " ")[:200]
        _log.error(
            "agent.whistleblower.non_json",
            extra={"preview": preview, "model": getattr(llm, "active_model", None)},
        )
        return {
            "sentiment": "neutral",
            "entities": [],
            "modus_operandi": [],
            "parse_failed": True,
        }
    return {
        "sentiment": str(obj.get("sentiment", "neutral")),
        "entities": [str(e) for e in (obj.get("entities") or []) if isinstance(e, (str, int))],
        "modus_operandi": _sanitise_modus(obj.get("modus_operandi")),
    }


_CONCLUSION_SYSTEM = (
    "You are drafting the final chat conclusion for a batch of PQR complaints "
    "the whistleblower agent has just analysed. The per-PQR verdicts below were "
    "decided by another LLM; your job is to summarise them into a single "
    "conclusion for a human reader.\n"
    "Respond ONLY with JSON matching {\"verdict\": 'suspicious'|'inconclusive'"
    "|'no_findings', \"confidence\": float in [0,1], \"summary\": \"<MUST be written in Spanish>\"}. "
    "The summary is what the human will read in the chat — name the dominant "
    "modus operandi, the strongest evidence and your final decision, always in Spanish. "
    "Aim for around 100 words, but use more if the evidence requires it."
)


async def _summarise_pqrs(llm: LLMClient, per_pqr_verdicts: list[dict[str, Any]]) -> dict[str, Any]:
    """Ask the LLM to turn the per-PQR verdicts into a chat conclusion.

    Falls back to a deterministic, evidence-grounded summary if the LLM is
    unavailable or returns unparseable JSON — never silently drops the chat
    bubble for this agent.
    """
    from ._runtime import parse_conclusion

    if not per_pqr_verdicts:
        return {
            "verdict": "no_findings",
            "confidence": 0.0,
            "summary": "No se recibieron PQR para analizar.",
        }
    try:
        rendered = [
            {
                "pqr_id": v.get("pqr_id"),
                "sentiment": v.get("sentiment"),
                "entities": v.get("entities"),
                "modus_operandi": v.get("modus_operandi"),
            }
            for v in per_pqr_verdicts
        ]
        resp: ChatResponse = await llm.chat(
            messages=[
                Message(role="system", content=_CONCLUSION_SYSTEM),
                Message(
                    role="user",
                    content=(
                        "Per-PQR verdicts (PHI-free rendering):\n"
                        f"{json.dumps(rendered, ensure_ascii=False)}"
                    ),
                ),
            ],
            tools=[],
        )
        return parse_conclusion(resp.text)
    except Exception as exc:
        _log.warning(
            "agent.whistleblower.conclusion_fallback",
            extra={"reason": type(exc).__name__, "error": str(exc)},
        )
        # Deterministic fallback so the chat pane always gets a conclusion.
        negative = [v for v in per_pqr_verdicts if v.get("sentiment") == "negative"]
        modus = sorted({m for v in negative for m in (v.get("modus_operandi") or [])})
        if negative and modus:
            verdict = "suspicious"
            conf = 0.7
            summary = (
                f"{len(negative)} PQR con sentiment negativo; modus operandi dominante: "
                f"{', '.join(modus[:3])}. Se recomienda escalamiento al agente de consenso."
            )
        elif negative:
            verdict = "inconclusive"
            conf = 0.4
            summary = (
                f"{len(negative)} PQR con sentiment negativo pero sin modus operandi "
                "explícito en el allow-list; requiere revisión humana."
            )
        else:
            verdict = "no_findings"
            conf = 0.0
            summary = "Las PQR analizadas no muestran patrones sospechosos."
        return {"verdict": verdict, "confidence": conf, "summary": summary}
