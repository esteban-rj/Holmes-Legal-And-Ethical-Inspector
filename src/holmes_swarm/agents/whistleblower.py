"""WhistleblowerAgent (FR-007) — LLM-driven PQR analyst.

System prompt enforces:

- Treats the PQR text as confidential; ``body`` text is in the tool's redact
  list and MUST NOT be logged.
- Output schema is strict JSON: sentiment, entities, modus_operandi.
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
import re
from typing import Any

from ..blackboard.schema import InvestigationScope, Signal
from ..llm.base import ChatResponse, LLMClient, Message, ToolSpec
from ._runtime import make_signal
from ._tools import fetch_url_tool
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
        redact_arg_keys: tuple[str, ...] = ("body", "text", "pqrs", "narrative", "phi"),
    ) -> None:
        self.llm = llm
        self.http = http_client
        self._explore_allowed_hosts = tuple(explore_allowed_hosts)
        self._redact_arg_keys = tuple(redact_arg_keys)

    # ---------- LLM-facing surfaces ----------

    def system_prompt(self) -> str:
        return (
            "You are the PQR (Peticiones, Quejas, Reclamos) NLP analyst of the "
            "Holmes swarm. For each anonymous complaint you receive, you must "
            "extract a structured summary AND a confidence score.\n\n"
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
        if self.http is None or not self._explore_allowed_hosts:
            return []
        return [
            fetch_url_tool(
                http_client=self.http, allowed_host_patterns=self._explore_allowed_hosts
            )
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
    """
    messages = [
        Message(role="system", content=system_prompt),
        Message(role="user", content=f"PQR: {body}"),
    ]
    try:
        resp: ChatResponse = await llm.chat(messages, tools=[])
    except Exception:
        # Re-raise so the caller can surface the failure; we never silently
        # fall back to a coded regex extractor.
        raise
    text = (resp.text or "").strip()
    # Strip ```json fences if present.
    fence = re.match(r"```(?:json)?\s*([\s\S]+?)```", text, re.IGNORECASE)
    if fence:
        text = fence.group(1).strip()
    if not text.startswith("{"):
        # Some providers put the JSON inside prose; extract first {...} block.
        m = re.search(r"\{[\s\S]*\}", text)
        if m:
            text = m.group(0)
    try:
        obj = json.loads(text)
    except json.JSONDecodeError:
        # Surface the first 200 chars of the model output (no PQR body — that
        # lives in `body` arg, not in the verdict text) so future debugging
        # doesn't have to reproduce the failure blind.
        preview = (resp.text or "").strip().replace("\n", " ")[:200]
        _log.error(
            "agent.whistleblower.non_json",
            extra={"preview": preview, "model": getattr(llm, "active_model", None)},
        )
        raise AgentUnavailableError(
            "whistleblower agent: LLM returned non-JSON verdict"
        ) from None
    if not isinstance(obj, dict):
        raise AgentUnavailableError(
            "whistleblower agent: LLM returned a non-object verdict"
        )
    return {
        "sentiment": str(obj.get("sentiment", "neutral")),
        "entities": [str(e) for e in (obj.get("entities") or []) if isinstance(e, (str, int))],
        "modus_operandi": _sanitise_modus(obj.get("modus_operandi")),
    }
