"""WhistleblowerAgent (FR-007) — LLM-driven PQR analyst.

System prompt enforces:

- Treats the PQR text as confidential; ``body`` text is in the tool's redact
  list and MUST NOT be logged.
- Output schema is strict JSON: sentiment, entities, modus_operandi.
- ``fetch_url`` is allowed only against the agent's allow-list (e.g. a
  moderation-API or a curated PQR-glossary host) and the LLM decides whether
  to consult it.

When the LLM is missing or returns no signals, the regex fall-through on
known modus operandi patterns still applies, preserving back-compat.
"""

from __future__ import annotations

import json
import re
from typing import Any

from ..blackboard.schema import InvestigationScope, Signal
from ..llm.base import ChatResponse, LLMClient, Message, ToolSpec
from ._runtime import make_signal
from ._tools import fetch_url_tool
from .base import AgentRuntimeContext

_MODUS_PATTERNS = [
    r"whatsapp", r"telegram", r"mensajer[íi]a instant[áa]nea",
    r"auxiliares?", r"comisi[óo]n", r"soborno", r"coima", r"facturas? falsas?",
    r"cartel", r"colusi[óo]n",
]


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

            sink = getattr(ctx, "thought_sink", None) if ctx else None
            if sink is not None:
                await sink.emit(
                    "note",
                    {"message": f"Analizando PQR «{pqr.get('id', '?')}» — {pqr.get('channel', 'pqr')}"},
                )

            verdict: dict[str, Any] = {}
            if llm is not None:
                if sink is not None:
                    await sink.emit(
                        "note",
                        {"message": "Pidiendo al LLM un veredicto sobre la queja…"},
                    )
                try:
                    verdict = await _ask_llm_for_verdict(llm, self.system_prompt(), text)
                except Exception as exc:
                    if sink is not None:
                        await sink.emit(
                            "note",
                            {
                                "message": (
                                    f"LLM no disponible ({type(exc).__name__}: {exc}); "
                                    "se usará heurística regex."
                                )
                            },
                        )
                    verdict = {}

            if not verdict.get("modus_operandi"):
                # Grep-based fallback when the LLM declined or returned empty.
                hits = []
                for pat in _MODUS_PATTERNS:
                    if re.search(pat, text, re.IGNORECASE):
                        hits.append(_canonical(pat))
                # Always merge regex hits into the verdict so downstream sees them.
                if not verdict:
                    sentiment = _fallback_sentiment(text)
                    verdict = {"sentiment": sentiment, "entities": [], "modus_operandi": hits}
                else:
                    verdict["modus_operandi"] = hits
                    verdict.setdefault("sentiment", _fallback_sentiment(text))

            if not verdict.get("modus_operandi"):
                # Grep-based fallback when the LLM declined or returned empty.
                hits = []
                for pat in _MODUS_PATTERNS:
                    if re.search(pat, text, re.IGNORECASE):
                        hits.append(_canonical(pat))
                # Always merge regex hits into the verdict so downstream sees them.
                if not verdict:
                    sentiment = _fallback_sentiment(text)
                    verdict = {"sentiment": sentiment, "entities": [], "modus_operandi": hits}
                else:
                    verdict["modus_operandi"] = hits
                    verdict.setdefault("sentiment", _fallback_sentiment(text))

            modus = verdict.get("modus_operandi") or []
            if not modus:
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
                        "reasoning_source": "llm" if llm is not None else "regex_fallback",
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


def _canonical(pattern: str) -> str:
    base = re.sub(r"[\\?+*]", "", pattern).rstrip("\\")
    return {
        "whatsapp": "whatsapp",
        "telegram": "telegram",
        "mensajería instantánea": "mensajeria_instantanea",
        "mensajeria instantanea": "mensajeria_instantanea",
        "mensajer[íi]a instant[áa]nea": "mensajeria_instantanea",
        "auxiliares?": "auxiliares",
        "auxiliares": "auxiliares",
        "comisión": "comision",
        "comision": "comision",
        "comisi[óo]n": "comision",
        "soborno": "soborno",
        "coima": "coima",
        "facturas? falsas?": "facturas_falsas",
        "facturas falsas": "facturas_falsas",
        "cartel": "cartel",
        "colusión": "colusion",
        "colusion": "colusion",
        "colusi[óo]n": "colusion",
    }.get(base, base)


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


def _fallback_sentiment(text: str) -> str:
    t = text.lower()
    if any(w in t for w in ["fraude", "irregular", "corrup", "cobro", "amenaza"]):
        return "negative"
    return "neutral"


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
        return {}
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
        return {}
    if not isinstance(obj, dict):
        return {}
    return {
        "sentiment": str(obj.get("sentiment", "neutral")),
        "entities": [str(e) for e in (obj.get("entities") or []) if isinstance(e, (str, int))],
        "modus_operandi": _sanitise_modus(obj.get("modus_operandi")),
    }
