"""Shared tool implementations used by the LLM-driven agents.

These are the *only* outbound primitives agents should call to touch the
network or local RAG. Each tool declares:

- `allowed_host_patterns`: enforced by `execute_tool_calls` (FR-018).
- `redact_arg_keys`: keys whose values MUST NOT be logged (FR-021).

If an agent requires a tool that doesn't fit one of these three, the agent
should define its own scoped tool in its own module rather than bypass this
abstraction.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import httpx

from ..llm.base import ToolSpec


# Used as the allow-list for fetch_url / web_search when an agent opts into
# `unrestricted_web: true` in its InternetProfile. Re.match with this pattern
# matches any non-empty host, so the host check in `execute_tool_calls` is
# effectively bypassed for that tool only. Note: the outbound httpx client's
# own `allowed_hosts` event-hook is a separate layer; setting
# `unrestricted_web` does NOT widen that.
UNRESTRICTED_WEB_PATTERN: tuple[str, ...] = (r"^.+$",)


def _http_tool(
    name: str,
    description: str,
    parameters: dict[str, Any],
    handler,
    *,
    allowed_host_patterns: Sequence[str],
    redact_arg_keys: Sequence[str] = (),
) -> ToolSpec:
    return ToolSpec(
        name=name,
        description=description,
        parameters=parameters,
        handler=handler,
        allowed_host_patterns=allowed_host_patterns,
        redact_arg_keys=redact_arg_keys,
    )


def fetch_url_tool(
    *, http_client: httpx.AsyncClient, allowed_host_patterns: Sequence[str]
) -> ToolSpec:
    """GET a URL and return the body as a string, truncated to ~16KB."""

    async def _handler(args: dict[str, Any]) -> dict[str, Any]:
        url = args["url"]
        try:
            resp = await http_client.get(url, timeout=10.0, follow_redirects=True)
            status = getattr(resp, "status_code", 0)
            if status and status >= 400:
                return {"status": status, "error": "http_error", "body": ""}
            # Both real httpx and fakes get a body. We don't require
            # `raise_for_status` to be available (test fakes can't).
            try:
                text = resp.text  # type: ignore[attr-defined]
            except Exception:
                text = "<binary>"
            body = text[:16384]
            return {"status": status, "body": body}
        except Exception as exc:  # noqa: BLE001 — fail open, never crash the agent
            return {"status": 0, "error": type(exc).__name__, "body": ""}

    return _http_tool(
        name="fetch_url",
        description=(
            "Fetch a public URL and return its text body, truncated to 16KB. "
            "Use this when you need to inspect an external data source (SECOP "
            "open data, public contracting pages, etc.). The host MUST match "
            "the agent's allowed_hosts configuration."
        ),
        parameters={
            "type": "object",
            "properties": {"url": {"type": "string", "description": "Absolute URL to fetch."}},
            "required": ["url"],
            "additionalProperties": False,
        },
        handler=_handler,
        allowed_host_patterns=allowed_host_patterns,
        redact_arg_keys=(),
    )


def web_search_tool(
    *,
    http_client: httpx.AsyncClient,
    allowed_host_patterns: Sequence[str],
    url_builder=None,
) -> ToolSpec:
    """Run a search via an allow-listed search provider.

    `url_builder` is an optional callable ``(query: str) -> str`` that builds
    the search URL. When omitted the tool falls back to DuckDuckGo's HTML
    endpoint (which requires ``duckduckgo.com`` in ``allowed_host_patterns``).
    Agents whose allow-list only contains domain-specific APIs (e.g. the
    logistics agent, allow-listed for OSRM / OpenRouteService / Nominatim)
    should pass a ``url_builder`` that targets one of their allowed hosts —
    Nominatim's ``/search`` endpoint is a good fit for geocoding lookups.
    """

    async def _handler(args: dict[str, Any]) -> list[dict[str, Any]]:
        q = args["query"]
        if url_builder is not None:
            url = url_builder(q)
        else:
            # Default DuckDuckGo fallback; callers MUST include duckduckgo.com
            # in allowed_host_patterns or the request will be blocked.
            url = f"https://duckduckgo.com/html/?q={httpx.QueryParams({'q': q})}"  # type: ignore[attr-defined]
        resp = await http_client.get(url, timeout=10.0, follow_redirects=True)
        resp.raise_for_status()
        try:
            payload = resp.json()
        except Exception:
            payload = None
        if isinstance(payload, list):
            results: list[dict[str, Any]] = []
            for item in payload:
                if not isinstance(item, dict):
                    continue
                results.append(
                    {
                        "title": item.get("display_name") or item.get("title") or "",
                        "url": item.get("url") or "",
                    }
                )
            return results[:5]
        # Non-JSON responses (e.g. DuckDuckGo HTML): keep the original tiny
        # shape so the LLM knows to call fetch_url for a specific result.
        return [
            {"query": q, "status": resp.status_code, "hint": "use fetch_url on a specific result"}
        ]

    return _http_tool(
        name="web_search",
        description=(
            "Search the public web for a query. Returns a short hint to call "
            "fetch_url on a specific result. The provider host MUST be in the "
            "agent's allowed_hosts."
        ),
        parameters={
            "type": "object",
            "properties": {"query": {"type": "string", "description": "Search query."}},
            "required": ["query"],
            "additionalProperties": False,
        },
        handler=_handler,
        allowed_host_patterns=allowed_host_patterns,
        redact_arg_keys=(),
    )


def retriever_query_tool(*, retriever, redact_arg_keys: Sequence[str]) -> ToolSpec:
    """Query a local RAG retriever (matches `Retriever.retrieve` Protocol)."""

    async def _handler(args: dict[str, Any]) -> list[dict[str, Any]]:
        q = args["query"]
        k = int(args.get("k", 4))
        docs = await retriever.retrieve(q, k=k)
        return [{"text": d.text, "source": d.source, "score": d.score} for d in docs]

    return ToolSpec(
        name="retriever_query",
        description=(
            "Query the local medical/clinical knowledge base (SOAT tariffs, "
            "ISS guidelines). Use when you need to compare a procedure against "
            "official reference material."
        ),
        parameters={
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Natural-language query."},
                "k": {"type": "integer", "description": "How many chunks to return (default 4)."},
            },
            "required": ["query"],
            "additionalProperties": False,
        },
        handler=_handler,
        allowed_host_patterns=(),
        redact_arg_keys=redact_arg_keys,
    )


def read_blackboard_tool(*, bus, redact_arg_keys: Sequence[str]) -> ToolSpec:
    """Read recent signals from the Blackboard (used by Consensus Agent)."""

    async def _handler(args: dict[str, Any]) -> dict[str, Any]:
        entity_id = args.get("entity_id")
        limit = int(args.get("limit", 50))
        sigs = bus.query_signals(entity_id=entity_id) if entity_id else bus.all_signals()
        sigs = sigs[-limit:]
        return [
            {
                "id": str(s.id),
                "entity_id": s.entity_id,
                "signal_type": s.signal_type,
                "source_agent": s.source_agent,
                "confidence": s.confidence,
                "evidence": s.evidence,
                "origin": s.origin,
            }
            for s in sigs
        ]

    return ToolSpec(
        name="read_blackboard",
        description=(
            "Read recent signals from the Blackboard, optionally filtered by "
            "entity_id. Use this to gather context before summarising an alert."
        ),
        parameters={
            "type": "object",
            "properties": {
                "entity_id": {"type": "string", "description": "Optional entity id filter."},
                "limit": {"type": "integer", "description": "Max number of signals (default 50)."},
            },
            "additionalProperties": False,
        },
        handler=_handler,
        allowed_host_patterns=(),
        redact_arg_keys=redact_arg_keys,
    )
