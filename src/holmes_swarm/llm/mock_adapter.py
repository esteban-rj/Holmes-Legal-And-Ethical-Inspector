"""Deterministic offline LLM client (still the default for tests).

The mock now understands the tool-calling protocol: an agent can either get
straight text back, or get a `ToolCall` synthesised from trigger keywords in
its prompt. This is enough to drive the agentic loop in tests without hitting
a network.
"""

from __future__ import annotations

import json
import re
import uuid
from typing import Any

from .base import (
    ChatResponse,
    Message,
    ToolCall,
    ToolSpec,
)

# Same heuristic corpus as before, kept for grep-style fallback.
_MODUS_OPERANDI_PATTERNS = [
    r"whatsapp", r"telegram", r"mensajer[íi]a instant[áa]nea",
    r"auxiliares?", r"comisi[óo]n", r"soborno", r"coima", r"facturas? falsas?",
    r"cartel", r"colusi[óo]n",
]


def _trigger_pattern(prompt: str, tool_name: str) -> bool:
    return re.search(rf"\buse[_\s]?{re.escape(tool_name)}\b", prompt, re.IGNORECASE) is not None


def _json_arg(arg_text: str) -> dict[str, Any]:
    try:
        return json.loads(arg_text)
    except Exception:
        return {}


class MockLLMClient:
    """Deterministic offline LLM that honours `tools=...` contracts.

    Rules (all keyword-based, no real reasoning):

    - If the prompt contains ``use_web_search`` or matches a heuristic phrase
      (``secop``, ``tarifa``, ``contrato``), the LLM "calls" `web_search`.
    - If the prompt contains ``use_fetch_url`` AND a URL appears in context,
      it calls `fetch_url`.
    - If the prompt contains ``use_retriever``, it calls `retriever_query`.
    - PQR text -> sentiment + modus_operandi response (back-compat).
    - Anything else -> no-op text reply.
    """

    def __init__(self, *, scripted_responses: list[ChatResponse] | None = None) -> None:
        # Tests can pin exact responses to simulate multi-turn tool loops.
        self._scripted = list(scripted_responses or [])
        self._cursor = 0

    async def chat(
        self,
        messages: list[Message],
        tools: list[ToolSpec] | None = None,
        **kwargs: Any,
    ) -> ChatResponse:
        if self._cursor < len(self._scripted):
            resp = self._scripted[self._cursor]
            self._cursor += 1
            return resp

        prompt = "\n".join(m.content for m in messages).lower()
        tool_names = {t.name for t in (tools or [])}

        # ---- tool triggering ----
        if tools:
            if "web_search" in tool_names and _trigger_pattern(prompt, "web_search"):
                q = _extract_query(prompt, default=prompt[-200:])
                return self._mk_tool_call("web_search", {"query": q})
            if "fetch_url" in tool_names and _trigger_pattern(prompt, "fetch_url"):
                url = _extract_url(prompt) or "about:blank"
                return self._mk_tool_call("fetch_url", {"url": url})
            if "retriever_query" in tool_names and _trigger_pattern(prompt, "retriever_query"):
                q = _extract_query(prompt, default=prompt[-200:])
                return self._mk_tool_call("retriever_query", {"query": q})

        # ---- legacy heuristics (PQR / SOAT) for back-compat ----
        if "pqr" in prompt or "queja" in prompt or "denuncia" in prompt:
            sentiment = (
                "negative"
                if any(w in prompt for w in ["fraude", "irregular", "corrup", "cobro"])
                else "neutral"
            )
            hits = [pat for pat in _MODUS_OPERANDI_PATTERNS if re.search(pat, prompt)]
            payload = {"sentiment": sentiment, "entities": [], "modus_operandi": hits}
            return ChatResponse(
                text=_render(payload), raw={"mock": True, "payload": payload}
            )

        if "tariff" in prompt or "soat" in prompt or "iss" in prompt or "manual" in prompt:
            return ChatResponse(
                text=(
                    "[mock] referencing local SOAT/ISS corpus: plausible clinical "
                    "verdict requires comparison with reference procedure volume."
                ),
                raw={"mock": True},
            )

        # Default: no-op so agent can fall back to deterministic rules.
        return ChatResponse(text="[mock] no-op", raw={"mock": True})

    def _mk_tool_call(self, name: str, arguments: dict[str, Any]) -> ChatResponse:
        tc = ToolCall(id=f"call_{uuid.uuid4().hex[:8]}", name=name, arguments=arguments)
        return ChatResponse(text="", raw={"mock": True}, tool_calls=[tc])


def _extract_query(prompt: str, *, default: str = "") -> str:
    m = re.search(r"query[:=]\s*[\"']?([^\"'\n]+)", prompt)
    return m.group(1).strip() if m else default


def _extract_url(prompt: str) -> str | None:
    m = re.search(r"https?://[\w\-\.:/%?&=#]+", prompt)
    return m.group(0) if m else None


def _render(d: dict[str, Any]) -> str:
    return "|".join(f"{k}={v}" for k, v in d.items())
