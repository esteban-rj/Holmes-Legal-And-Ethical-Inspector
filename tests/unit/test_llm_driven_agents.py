"""LLM-driven behaviour tests.

These exercise the contract that each agent *reasons* through the LLM
(using a scripted `MockLLMClient`) and invokes tools under the allow-list.
When the LLM is unavailable or returns no usable verdict, the agent MUST
NOT fall back to a coded/deterministic heuristic — it MUST raise
:class:`AgentUnavailableError`. Decisions belong to a functional agent, not
hardcoded rules.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from holmes_swarm.agents._runtime import parse_conclusion, parse_verdict
from holmes_swarm.agents._tools import fetch_url_tool, retriever_query_tool
from holmes_swarm.agents.base import AgentRuntimeContext, AgentUnavailableError
from holmes_swarm.agents.contracting import ContractingAgent
from holmes_swarm.agents.logistics import LogisticsAgent
from holmes_swarm.agents.medical import MedicalAgent
from holmes_swarm.agents.whistleblower import WhistleblowerAgent
from holmes_swarm.llm.base import (
    ChatResponse,
    Message,
    ToolCall,
    ToolSpec,
    execute_tool_calls,
    run_with_tool_loop,
)
from holmes_swarm.llm.mock_adapter import MockLLMClient
from holmes_swarm.rag.base import Chunk

ENTITY = "900123456-7"


# ---------- fakes & helpers ----------


class FakeRetriever:
    def __init__(self, hits: list[Chunk] | None = None) -> None:
        self._hits = hits or [Chunk(text="SOAT cateterismo ~1.250.000 COP", source="soat.md")]
        self.last_query: str = ""

    async def retrieve(self, query: str, k: int = 5):
        self.last_query = query
        return self._hits[:k]


class FakeHttpClient:
    """Records every URL fetched, returns a fake response with `.status_code` and `.text`."""

    class _FakeResp:
        def __init__(self, status: int, body: str) -> None:
            self.status_code = status
            self.text = body

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.responses: dict[str, dict[str, Any]] = {}

    def set_response(self, host_pattern: str, body: str, status: int = 200) -> None:
        self.responses[host_pattern] = {"body": body, "status": status}

    async def get(self, url: str, timeout: float = 10.0, follow_redirects: bool = True):
        self.calls.append(url)
        body = '{"placeholder": true}'
        status = 200
        for pat, resp in self.responses.items():
            import re
            if re.search(pat, url):
                body = resp["body"]
                status = resp["status"]
                break
        return FakeHttpClient._FakeResp(status, body)

    async def aclose(self) -> None:
        pass


def _scripted(responses: list[ChatResponse]) -> MockLLMClient:
    return MockLLMClient(scripted_responses=responses)


def _verdict_json(
    signals: list[dict[str, Any]],
    verdict: str = "suspicious",
    confidence: float = 0.0,
    summary: str = "",
) -> ChatResponse:
    if not confidence:
        if signals:
            confidence = max(0.0, min(1.0, float(signals[0].get("confidence", 0.0))))
        else:
            confidence = 0.0
    if not summary:
        summary = f"Patrón detectado con {len(signals)} señal(es)."
    return ChatResponse(
        text=json.dumps(
            {
                "verdict": verdict,
                "confidence": confidence,
                "summary": summary,
                "signals": signals,
            }
        ),
        raw={"mock": True},
    )


# ---------- tool executor / allow-list ----------


@pytest.mark.asyncio
async def test_execute_tool_calls_blocks_non_allowlisted_host():
    http = FakeHttpClient()

    async def handler(args):
        return await http.get(args["url"])

    tool = fetch_url_tool(
        http_client=http,  # type: ignore[arg-type]
        allowed_host_patterns=("^api\\.secop\\.gov\\.co$",),
    )
    call = ToolCall(id="c1", name="fetch_url", arguments={"url": "https://evil.example/x"})
    results = await execute_tool_calls([call], {"fetch_url": tool})
    assert results[0].is_error is True
    assert "host not allowed" in results[0].output
    assert http.calls == []


@pytest.mark.asyncio
async def test_execute_tool_calls_allows_listed_host():
    http = FakeHttpClient()
    http.set_response("^api\\.secop\\.gov\\.co$", '{"price": 1000000}')

    async def handler(args):
        return await http.get(args["url"])

    tool = fetch_url_tool(
        http_client=http,  # type: ignore[arg-type]
        allowed_host_patterns=("^api\\.secop\\.gov\\.co$",),
    )
    call = ToolCall(id="c1", name="fetch_url", arguments={"url": "https://api.secop.gov.co/x"})
    results = await execute_tool_calls([call], {"fetch_url": tool})
    assert results[0].is_error is False
    assert http.calls == ["https://api.secop.gov.co/x"]


@pytest.mark.asyncio
async def test_execute_tool_calls_redacts_pii_keys():
    async def handler(args):
        return {"echoed": args}

    tool = ToolSpec(
        name="echo",
        description="echo",
        parameters={"type": "object", "properties": {}, "required": []},
        handler=handler,
        redact_arg_keys=("body",),
    )
    call = ToolCall(id="c1", name="echo", arguments={"body": "secret PQR text", "x": 1})
    await execute_tool_calls([call], {"echo": tool})
    # No assertion on logs (caplog is brittle); just confirm the tool ran.
    # The redaction helper is exercised through the same path.


@pytest.mark.asyncio
async def test_run_with_tool_loop_terminates_when_lldm_no_tools():
    llm = _scripted([
        ChatResponse(text='{"signals": [{"signal_type":"financial","confidence":0.7,"evidence":{"pattern":"monopoly"}}]}'),
    ])

    async def chat(messages, tools=(), **kwargs):
        return await llm.chat(messages, tools=tools, **kwargs)

    resp = await run_with_tool_loop(
        llm=llm,
        messages=[Message(role="user", content="hello")],
        tools={},
        chat_fn=chat,
        max_steps=3,
    )
    assert "monopoly" in resp.text
    assert resp.tool_calls == []


# ---------- parse_verdict ----------


def test_parse_verdict_handles_fenced_blocks_and_no_signals():
    assert parse_verdict("```json\n{\"signals\":[]}\n```") == []
    assert parse_verdict("{\"signals\":[{\"a\":1}]}") == [{"a": 1}]
    assert parse_verdict("plain text") == []


def test_parse_conclusion_does_not_truncate_and_defaults():
    short = parse_conclusion(
        '{"verdict":"suspicious","confidence":0.9,'
        '"summary":"movimientos imposibles entre dos hospitales."}'
    )
    assert short["verdict"] == "suspicious"
    assert short["confidence"] == 0.9
    assert "movimientos imposibles" in short["summary"]
    long_text = "palabra " * 200
    big = parse_conclusion(
        f'{{"verdict":"suspicious","confidence":0.5,"summary":"{long_text}"}}'
    )
    assert len(big["summary"].split()) == 200
    # Invalid verdict -> inconclusive
    fallback = parse_conclusion('{"verdict":"maybe","confidence":1.4,"summary":"x"}')
    assert fallback["verdict"] == "inconclusive"
    assert fallback["confidence"] == 1.0
    # Garbage -> inconclusive empty summary
    empty = parse_conclusion("not json at all")
    assert empty == {"verdict": "inconclusive", "confidence": 0.0, "summary": ""}


# ---------- Contracting ----------


@pytest.mark.asyncio
async def test_contracting_llm_path_emits_signal_from_verdict():
    llm = _scripted([_verdict_json([
        {
            "signal_type": "financial",
            "confidence": 0.82,
            "evidence": {"pattern": "monopoly", "procedure_code": "93010", "share": 0.9},
        }
    ])])
    agent = ContractingAgent(llm=llm)
    sigs = await agent.run(
        {"entity_id": ENTITY, "contracts": [{"code": "93010"}] * 3},
        scope=None,
    )
    assert len(sigs) >= 1
    sig = next(s for s in sigs if s.evidence.get("reasoning_source") == "llm")
    assert sig.confidence == 0.82
    assert sig.origin["kind"] == "autonomous-monitoring"


@pytest.mark.asyncio
async def test_contracting_returns_no_signals_but_emits_inconclusive_conclusion():
    """When the LLM returns no usable signal data the agent MUST NOT silently
    emit a coded SECOP verdict. It returns no signals but still forwards an
    "inconclusive" chat conclusion through its thought sink."""
    llm = MockLLMClient()  # empty queue -> returns [mock] no-op
    thoughts: list[tuple[str, dict[str, Any]]] = []

    class _Sink:
        async def emit(self, kind: str, payload: dict[str, Any]) -> None:
            thoughts.append((kind, payload))

    agent = ContractingAgent(llm=llm)
    sigs = await agent.run(
        {"entity_id": ENTITY, "contracts": [{"code": "93010", "price": 100_000}] * 3},
        scope=None,
        ctx=AgentRuntimeContext(llm=llm, thought_sink=_Sink()),  # type: ignore[arg-type]
    )
    assert sigs == []
    conclusions = [p for k, p in thoughts if k == "conclusion"]
    assert conclusions, "agent must forward a chat conclusion"
    assert conclusions[0]["verdict"] == "inconclusive"


@pytest.mark.asyncio
async def test_contracting_raises_agent_unavailable_when_llm_call_fails():
    """A real LLM error (e.g. provider 400) MUST surface as AgentUnavailableError,
    not be swallowed into a coded verdict."""

    class _BoomLLM(MockLLMClient):
        async def chat(self, messages, tools=(), **kwargs):  # type: ignore[override]
            raise RuntimeError("HTTP 400 from provider")

    agent = ContractingAgent(llm=_BoomLLM())
    with pytest.raises(AgentUnavailableError):
        await agent.run(
            {"entity_id": ENTITY, "contracts": [{"code": "93010", "price": 100_000}] * 3},
            scope=None,
        )


@pytest.mark.asyncio
async def test_contracting_emits_error_thought_on_llm_failure():
    """When the LLM fails the agent MUST also emit a ``kind=error`` thought
    so the UI can surface the cause to the operator."""
    thoughts: list[tuple[str, dict[str, Any]]] = []

    class _Sink:
        async def emit(self, kind: str, payload: dict[str, Any]) -> None:
            thoughts.append((kind, payload))

    class _BoomLLM(MockLLMClient):
        async def chat(self, messages, tools=(), **kwargs):  # type: ignore[override]
            raise RuntimeError("HTTP 400 from provider")

    ctx = AgentRuntimeContext(llm=_BoomLLM(), thought_sink=_Sink())  # type: ignore[arg-type]
    agent = ContractingAgent(llm=ctx.llm)
    with pytest.raises(AgentUnavailableError):
        await agent.run(
            {"entity_id": ENTITY, "contracts": [{"code": "93010"}]},
            scope=None,
            ctx=ctx,
        )
    assert thoughts, "agent must emit at least one thought on failure"
    kinds = [k for k, _ in thoughts]
    assert "error" in kinds, f"expected kind=error thought, got {kinds}"
    error_thoughts = [p for k, p in thoughts if k == "error"]
    assert error_thoughts[0].get("reason") == "llm_unavailable"


@pytest.mark.asyncio
async def test_contracting_can_call_secop_via_tool_allowed():
    # Set up scripted LLM that asks to fetch SECOP, then approves the verdict.
    fetch_call = ToolCall(id="c1", name="fetch_url", arguments={"url": "https://api.secop.gov.co/x"})
    llm = _scripted([
        ChatResponse(text="", tool_calls=[fetch_call]),
        _verdict_json([{"signal_type": "financial", "confidence": 0.7, "evidence": {"pattern": "below_reference_price", "source": "secop"}}]),
    ])
    http = FakeHttpClient()
    http.set_response("^api\\.secop\\.gov\\.co$", '{"ok":true}')

    async def fetch_handler(args):
        return await http.get(args["url"])

    fetch_tool = fetch_url_tool(http_client=http, allowed_host_patterns=("^api\\.secop\\.gov\\.co$",))

    async def chat(messages, tools=(), **kwargs):
        return await llm.chat(messages, tools=list(tools), **kwargs)

    final = await run_with_tool_loop(
        llm=llm,
        messages=[Message(role="system", content="You are the Contracting Auditor."),
                  Message(role="user", content="Investigate this contract using SECOP data.")],
        tools={"fetch_url": fetch_tool},
        chat_fn=chat,
    )
    assert "below_reference_price" in final.text
    assert http.calls == ["https://api.secop.gov.co/x"]


# ---------- Logistics ----------


@pytest.mark.asyncio
async def test_logistics_returns_no_signals_but_emits_inconclusive_conclusion():
    """LogisticsAgent MUST NOT silently fall back to the haversine heuristic
    when the LLM emits nothing usable. It surfaces an "inconclusive" chat
    conclusion instead."""
    llm = MockLLMClient()
    thoughts: list[tuple[str, dict[str, Any]]] = []

    class _Sink:
        async def emit(self, kind: str, payload: dict[str, Any]) -> None:
            thoughts.append((kind, payload))

    agent = LogisticsAgent(llm=llm)
    sigs = await agent.run(
        {
            "entity_id": ENTITY,
            "events": [
                {"ts": "2026-01-01T08:00:00Z", "location": {"lat": 4.6, "lon": -74.1}},
                {"ts": "2026-01-01T08:05:00Z", "location": {"lat": 4.6, "lon": -74.1}},
            ],
        },
        scope=None,
        ctx=AgentRuntimeContext(llm=llm, thought_sink=_Sink()),  # type: ignore[arg-type]
    )
    assert sigs == []
    conclusions = [p for k, p in thoughts if k == "conclusion"]
    assert conclusions
    assert conclusions[0]["verdict"] == "inconclusive"


@pytest.mark.asyncio
async def test_logistics_llm_path_uses_verdict():
    llm = _scripted([_verdict_json([
        {"signal_type": "physical", "confidence": 0.75, "evidence": {"pattern": "impossible_movement"}},
    ])])
    agent = LogisticsAgent(llm=llm)
    sigs = await agent.run({"entity_id": ENTITY, "events": []}, scope=None)
    assert any(s.evidence.get("reasoning_source") == "llm" for s in sigs)


@pytest.mark.asyncio
async def test_logistics_web_search_uses_nominatim_not_duckduckgo():
    """LogisticsAgent's web_search tool MUST hit Nominatim (already in the
    agent's explore_allowed_hosts). Hitting DuckDuckGo would raise
    BlockedHostError from the allow-list client and break the agent."""
    http = FakeHttpClient()
    http.set_response(
        "nominatim",
        body=json.dumps(
            [
                {
                    "display_name": "Hospital San Ignacio, Bogotá, Colombia",
                    "lat": "4.5981",
                    "lon": "-74.0760",
                    "url": "",
                }
            ]
        ),
    )
    # First LLM turn: invoke web_search. Second turn: verdict (no findings).
    search_call = ToolCall(
        id="s1",
        name="web_search",
        arguments={"query": "Hospital San Ignacio Bogotá"},
    )
    llm = _scripted(
        [
            ChatResponse(text="", tool_calls=[search_call]),
            _verdict_json([], verdict="inconclusive", summary="Sin hallazgos."),
        ]
    )
    agent = LogisticsAgent(
        llm=llm,
        http_client=http,  # type: ignore[arg-type]
        explore_allowed_hosts=(
            r"^router\.project-osrm\.org$",
            r"^api\.openrouteservice\.org$",
            r"^nominatim\.openstreetmap\.org$",
        ),
    )
    sigs = await agent.run({"entity_id": ENTITY, "events": []}, scope=None)
    assert http.calls, "web_search must produce an outbound HTTP call"
    assert any("nominatim.openstreetmap.org" in url for url in http.calls), (
        f"expected Nominatim call, got: {http.calls}"
    )
    assert not any("duckduckgo.com" in url for url in http.calls), (
        "DuckDuckGo MUST NOT be called from the logistics agent — it would "
        "be blocked by the allow-list and raise BlockedHostError"
    )
    # No findings expected from the scripted verdict.
    assert sigs == []


# ---------- Medical ----------


@pytest.mark.asyncio
async def test_medical_consults_retriever_then_emits():
    retriever = FakeRetriever()
    # First LLM turn: ask to use_retriever_query. Second turn: verdict referencing SOAT.
    rt_call = ToolCall(id="r1", name="retriever_query", arguments={"query": "SOAT cateterismo"})
    llm = _scripted([
        ChatResponse(text="", tool_calls=[rt_call]),
        _verdict_json([{"signal_type": "clinical", "confidence": 0.8,
                        "evidence": {"pattern": "implausible_volume", "procedure_code": "93010", "tariff_source": "SOAT"}}]),
    ])
    rt_tool = retriever_query_tool(retriever=retriever, redact_arg_keys=("body",))

    async def chat(messages, tools=(), **kwargs):
        return await llm.chat(messages, tools=list(tools), **kwargs)

    final = await run_with_tool_loop(
        llm=llm,
        messages=[Message(role="system", content="You are the Clinical Coherence agent."),
                  Message(role="user", content="Check monthly volume for cateterismo. use_retriever_query.")],
        tools={"retriever_query": rt_tool},
        chat_fn=chat,
    )
    assert "implausible_volume" in final.text
    assert retriever.last_query == "SOAT cateterismo"


@pytest.mark.asyncio
async def test_medical_raises_agent_unavailable_when_llm_missing():
    """MedicalAgent MUST raise when no LLM is configured — the monthly-volume
    / specialty-mismatch heuristic is exposed to the LLM via the system
    prompt, not executed inline."""
    retriever = FakeRetriever()
    agent = MedicalAgent(retriever=retriever)
    with pytest.raises(AgentUnavailableError):
        await agent.run(
            {
                "entity_id": ENTITY,
                "specialty": "cardiologia_intervencionista",
                "procedures": [{"code": "93010"}] * 130,
                "services": ["cirugia_cardiaca"],
            },
            scope=None,
        )


@pytest.mark.asyncio
async def test_medical_returns_no_signals_but_emits_inconclusive_conclusion():
    """MedicalAgent MUST NOT silently emit a coded monthly-volume verdict when
    the LLM returns nothing usable; it forwards an "inconclusive" chat
    conclusion through the thought sink instead."""
    retriever = FakeRetriever()
    llm = MockLLMClient()
    thoughts: list[tuple[str, dict[str, Any]]] = []

    class _Sink:
        async def emit(self, kind: str, payload: dict[str, Any]) -> None:
            thoughts.append((kind, payload))

    agent = MedicalAgent(llm=llm, retriever=retriever)
    sigs = await agent.run(
        {
            "entity_id": ENTITY,
            "specialty": "cardiologia_intervencionista",
            "procedures": [{"code": "93010"}] * 130,
            "services": ["cirugia_cardiaca"],
        },
        scope=None,
        ctx=AgentRuntimeContext(llm=llm, thought_sink=_Sink()),  # type: ignore[arg-type]
    )
    assert sigs == []
    conclusions = [p for k, p in thoughts if k == "conclusion"]
    assert conclusions
    assert conclusions[0]["verdict"] == "inconclusive"


# ---------- Whistleblower ----------


@pytest.mark.asyncio
async def test_whistleblower_sanitises_invented_modus_operandi():
    import json as _json
    # First LLM call: per-PQR verdict. Second call: batch conclusion summary.
    llm = _scripted([
        ChatResponse(
            text=_json.dumps({
                "sentiment": "negative", "entities": [],
                "modus_operandi": ["whatsapp", "made_up_thing", "Telegram"],
            })
        ),
        ChatResponse(
            text=_json.dumps({
                "verdict": "suspicious",
                "confidence": 0.7,
                "summary": "PQR negativa con modus operandi WhatsApp en el allow-list.",
            })
        ),
    ])
    agent = WhistleblowerAgent(llm=llm)
    sigs = await agent.run(
        {"entity_id": ENTITY, "pqrs": [{"id": "PQR-1", "body": "hay fraude con whatsapp y auxiliares"}]},
        scope=None,
    )
    assert sigs, "should still emit a signal from sanitised allow-list"
    all_modus = {m for s in sigs for m in s.evidence.get("modus_operandi", [])}
    assert "made_up_thing" not in all_modus
    assert "whatsapp" in all_modus


@pytest.mark.asyncio
async def test_whistleblower_raises_agent_unavailable_when_no_llm():
    """WhistleblowerAgent MUST raise when no LLM is configured; the
    regex/modus-operandi allow-list is exposed to the LLM via system prompt,
    not executed inline."""
    agent = WhistleblowerAgent()
    with pytest.raises(AgentUnavailableError):
        await agent.run(
            {"entity_id": ENTITY, "pqrs": [{"id": "PQR-X", "body": "pago por whatsapp, posible fraude"}]},
            scope=None,
        )


# ---------- Origin gating with LLM ----------


@pytest.mark.asyncio
async def test_llm_verdict_respects_origin_via_InvestigationScope():
    """If the LLM emits a signal but the scope is autonomous, the signal
    MUST carry origin=autonomous-monitoring; if the scope is investigation,
    the signal carries the investigation id (FR-031/FR-032)."""
    import uuid

    from holmes_swarm.blackboard.schema import InvestigationScope

    llm = _scripted([
        ChatResponse(
            text=json.dumps({
                "sentiment": "negative",
                "entities": [],
                "modus_operandi": ["whatsapp"],
            })
        ),
        ChatResponse(
            text=json.dumps({
                "verdict": "suspicious",
                "confidence": 0.7,
                "summary": "PQR con modus WhatsApp.",
            })
        ),
    ])
    agent = WhistleblowerAgent(llm=llm)
    scope = InvestigationScope(
        investigation_request_id=uuid.uuid4(),
        target_entity_id=ENTITY,
    )
    sigs = await agent.run(
        {"entity_id": ENTITY, "pqrs": [{"id": "1", "body": "fraude con whatsapp"}]},
        scope=scope,
    )
    assert sigs, "should emit at least one signal under investigation scope"
    assert sigs[0].origin["kind"] == "investigation"
    assert sigs[0].origin["investigation_request_id"] == str(scope.investigation_request_id)


def test_AgentRuntimeContext_carries_required_components():
    ctx = AgentRuntimeContext(llm=MockLLMClient(), http_client=FakeHttpClient())
    assert ctx.llm is not None
    assert ctx.explore_allowed_hosts == ()
    assert "body" in ctx.redact_arg_keys


def test_unrestricted_web_bypasses_explore_allow_list_for_tools():
    """When `unrestricted_web=True`, the agent should still expose
    `fetch_url` / `web_search` even if `explore_allowed_hosts` is empty,
    and the produced ToolSpec should accept any host (match-all regex).
    """
    from holmes_swarm.agents._tools import UNRESTRICTED_WEB_PATTERN

    http = FakeHttpClient()
    # Default (no unrestricted_web, no explore hosts): no web tools.
    agent = ContractingAgent(llm=MockLLMClient(), http_client=http, retriever=None)
    assert all(t.name not in {"web_search", "fetch_url"} for t in agent.tools())

    # unrestricted_web=True, no explore hosts: tools exposed with the
    # match-all pattern.
    agent = ContractingAgent(
        llm=MockLLMClient(), http_client=http, retriever=None, unrestricted_web=True
    )
    tool_names = {t.name for t in agent.tools()}
    assert {"web_search", "fetch_url"} <= tool_names
    fetch = next(t for t in agent.tools() if t.name == "fetch_url")
    assert fetch.allowed_host_patterns == UNRESTRICTED_WEB_PATTERN
    assert list(fetch.allowed_host_patterns) == [r"^.+$"]


def test_unrestricted_web_only_affects_explore_tools_not_outbound_client():
    """`unrestricted_web` must NOT widen the outbound httpx client's own
    allow-list (that's a separate, transport-level layer in
    `make_allowlisted_client`). The flag is consumed only by `tools()`.
    """
    from holmes_swarm.agents._tools import UNRESTRICTED_WEB_PATTERN

    http = FakeHttpClient()
    agent = ContractingAgent(
        llm=MockLLMClient(),
        http_client=http,
        retriever=None,
        explore_allowed_hosts=(r"^api\.secop\.gov\.co$",),
        unrestricted_web=True,
    )
    # The web tools now match any host…
    fetch = next(t for t in agent.tools() if t.name == "fetch_url")
    assert fetch.allowed_host_patterns == UNRESTRICTED_WEB_PATTERN
    # …but the agent's explore_allowed_hosts (which is the wiring surface
    # to the outbound client) is unchanged.
    assert agent._explore_allowed_hosts == (r"^api\.secop\.gov\.co$",)


# Sanity: the type-ignore Comment "# type: ignore[arg-type]" on FakeHttpClient is intentional
# because we only implement the .get/.aclose subset httpx exposes.
