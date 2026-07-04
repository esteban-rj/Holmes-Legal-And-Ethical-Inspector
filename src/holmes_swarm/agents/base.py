"""Standard Agent contract (FR-003) — LLM-driven version.

A concrete agent now plugs into the swarm as an LLM-driven module:

* It receives an `AgentRuntimeContext` that gives it:
  - `llm`: the LLM transport (MinimaxLLMClient or MockLLMClient),
  - `http_client`: an allow-listed httpx client (or None),
  - `retriever`: a langchain-style `Retriever` for local RAG,
  - `explore_allowed_hosts`: the per-agent host allow-list (FR-017/FR-018),
  - `redact_arg_keys`: arg keys whose values the tool executor must redact
    (FR-021; defaults cover PQR text and PHI labels).
* It declares its tools via `tools()` so the runtime can pass them to the LLM.
* It implements `system_prompt()` returning the role + responsibilities
  description (used both in the LLM system message and in audit logs).
* It still implements `async run(batch, *, scope=None) -> list[Signal]`.
* It still implements `async shutdown() -> None`.

FR-031 / FR-032 origin rules are preserved: when `scope is None` the agent
MUST publish signals with origin `autonomous-monitoring`; inside an
InvestigationRequest, origin MUST be `investigation:<request_id>` and the agent
MUST NOT mix autonomous-origin emissions during the same run.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from ..blackboard.schema import Signal
from ..llm.base import LLMClient, ThoughtSink, ToolSpec
from ..rag.base import Retriever


@dataclass
class AgentRuntimeContext:
    """Per-run context injected into every agent's `run()` call.

    Built once at agent construction and reused. Keep it cheap and stateless
    beyond the long-lived clients (httpx, LLM, retriever).
    """

    llm: LLMClient
    http_client: Any | None = None  # httpx.AsyncClient | None
    retriever: Retriever | None = None
    explore_allowed_hosts: Sequence[str] = field(default_factory=tuple)
    redact_arg_keys: Sequence[str] = field(
        default_factory=lambda: ("body", "text", "pqrs", "phi", "narrative")
    )
    allowlist_logger: Any | None = None
    # Optional ThoughtSink: when set, the agent's LLM loop forwards per-step
    # reasoning to it so the UI can show "what the agent is thinking".
    thought_sink: ThoughtSink | None = None


@runtime_checkable
class Agent(Protocol):
    # ---- discovery ----
    id: str
    name: str
    signal_type: str
    confidence_threshold: float

    # ---- LLM-facing surfaces ----
    def system_prompt(self) -> str: ...
    def tools(self) -> list[ToolSpec]: ...

    # ---- legacy contract (unchanged) ----
    async def run(
        self, batch: Any, *, scope: Any = None, ctx: AgentRuntimeContext | None = None
    ) -> list[Signal]: ...
    async def shutdown(self) -> None: ...
