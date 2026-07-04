"""LLM client interface + DTOs + tool abstraction.

The LLM is the *reasoning* layer of every agent. Agents declare a set of tools
(plain async callables) and the LLM decides when to invoke them. Tool execution
is brokered by `execute_tool_calls`, which enforces:

- per-agent allow-list on any outbound HTTP (FR-017..FR-020),
- structured logging with redaction of sensitive payloads (FR-021),
- exponential backoff + outage tolerance (FR-014 / FR-020).

The concrete transport is the adapter's responsibility (MockLLMClient or
MinimaxLLMClient); this module is transport-agnostic.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from collections.abc import Awaitable, Callable, Mapping, Sequence
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any, Protocol

_log = logging.getLogger(__name__)


# ---------- core DTOs ----------


@dataclass
class Message:
    role: str  # "system" | "user" | "assistant" | "tool"
    content: str
    name: str | None = None
    tool_call_id: str | None = None
    tool_calls: list[ToolCall] | None = None


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: Mapping[str, Any]


@dataclass
class ToolResult:
    tool_call_id: str
    name: str
    output: Any
    is_error: bool = False


@dataclass
class ChatResponse:
    text: str
    raw: dict[str, Any] = field(default_factory=dict)
    tool_calls: list[ToolCall] = field(default_factory=list)
    # Chain-of-thought / reasoning tokens from the thinking variant of the
    # model (e.g. MiniMax-M3-thinking). Empty for non-thinking models. The
    # agent loop surfaces this verbatim as `kind=thinking` thoughts so the
    # UI can render the reasoning trace per agent.
    reasoning: str = ""


class LLMClient(Protocol):
    """Transport interface. Adapters must implement either `chat` or `chat_with_tools`."""

    async def chat(
        self, messages: list[Message], **kwargs: Any
    ) -> ChatResponse: ...


# ---------- per-step reasoning sink ----------
#
# `run_with_tool_loop` and `execute_tool_calls` publish per-step "thoughts"
# through a `ThoughtSink`. A sink is a small awaitable callable that receives a
# structured event describing what the agent is currently doing. Adapters and
# runtimes can push these to the UI (investigation service, SSE, etc.) without
# coupling the transport layer to the API surface.


class ThoughtSink:
    """Receives per-step reasoning events from the agent loop.

    Kinds emitted:
    - ``llm_step``: an LLM turn completed. payload = ``{step, text, tool_calls}``
    - ``tool_invoked``: a tool call started. payload = ``{tool, args_redacted}``
    - ``tool_succeeded`` / ``tool_failed``: tool call finished.
    - ``note``: arbitrary status line. payload = ``{message}``
    """

    def __init__(
        self,
        sink: Callable[[str, Mapping[str, Any]], Awaitable[None] | None] | None = None,
        *,
        agent_id: str | None = None,
        agent_name: str | None = None,
    ) -> None:
        self._sink = sink
        self.agent_id = agent_id
        self.agent_name = agent_name

    async def emit(self, kind: str, payload: Mapping[str, Any] | None = None) -> None:
        if self._sink is None:
            return
        try:
            res = self._sink(kind, dict(payload or {}))
            if hasattr(res, "__await__"):
                await res
        except Exception:  # noqa: BLE001 — sinks must never break the loop
            return


_NOOP_SINK = ThoughtSink(None)
_current_thought_sink: ContextVar[ThoughtSink] = ContextVar(
    "current_thought_sink", default=_NOOP_SINK
)


def get_current_thought_sink() -> ThoughtSink:
    return _current_thought_sink.get()


def set_current_thought_sink(sink: ThoughtSink) -> None:
    _current_thought_sink.set(sink)


# ---------- tool declaration ----------


@dataclass
class ToolSpec:
    """Tool metadata exposed to the LLM (OpenAI-compatible `tools` shape)."""

    name: str
    description: str
    parameters: dict[str, Any]  # JSON schema (object type)
    # The actual implementation: async callable receiving (args: dict) -> Any.
    handler: Callable[[dict[str, Any]], Awaitable[Any]]
    # Optional HTTP-side allow-list for tools that fetch URLs (FR-018 / FR-020).
    # A URL is allowed iff `re.match(pattern, host)` is truthy for any pattern.
    allowed_host_patterns: Sequence[str] = field(default_factory=tuple)
    # PII / PHI redaction policy: keys whose values MUST NOT be logged (FR-021).
    redact_arg_keys: Sequence[str] = field(default_factory=tuple)


# ---------- tool execution ----------


class ToolExecutionError(Exception):
    """Raised when a tool call fails irrecoverably (allow-list, exception, etc.)."""

    def __init__(self, tool_name: str, reason: str) -> None:
        super().__init__(f"{tool_name}: {reason}")
        self.tool_name = tool_name
        self.reason = reason


def _redact(args: Mapping[str, Any], keys: Sequence[str]) -> dict[str, Any]:
    """Return a copy of `args` with values for any `keys` replaced by '[redacted]'."""
    out: dict[str, Any] = {}
    for k, v in args.items():
        if k in keys:
            out[k] = "[redacted]"
        elif isinstance(v, Mapping):
            out[k] = _redact(v, keys)
        else:
            out[k] = v
    return out


async def execute_tool_calls(
    calls: Sequence[ToolCall],
    tools: Mapping[str, ToolSpec],
    *,
    max_retries: int = 2,
    retry_base_seconds: float = 0.5,
) -> list[ToolResult]:
    """Execute tool calls surfaced by the LLM.

    - Unknown tool name -> ToolResult with is_error=True (the swarm keeps going).
    - Validation/allow-list failure -> ToolResult with is_error=True (no retry).
    - Transient failure (network/5xx) -> retry with exponential backoff.
    """
    results: list[ToolResult] = []

    # URL allow-list helper built from each tool's declared patterns.
    host_patterns_by_tool: dict[str, Sequence[str]] = {
        name: tuple(spec.allowed_host_patterns) for name, spec in tools.items()
    }

    for call in calls:
        spec = tools.get(call.name)
        if spec is None:
            _log.warning("tool.unknown", extra={"tool": call.name})
            await get_current_thought_sink().emit(
                "tool_failed", {"tool": call.name, "error": "unknown_tool"}
            )
            results.append(
                ToolResult(call.id, call.name, f"unknown tool: {call.name}", is_error=True)
            )
            continue

        # Announce tool invocation (redacted args) and enforce URL allow-list.
        await get_current_thought_sink().emit(
            "tool_invoked",
            {
                "tool": call.name,
                "args_redacted": _redact(call.arguments, spec.redact_arg_keys),
            },
        )

        # Enforce URL allow-list for tools that take a `url` argument.
        url_arg = call.arguments.get("url") if isinstance(call.arguments, Mapping) else None
        if isinstance(url_arg, str):
            host = url_arg.split("//", 1)[-1].split("/", 1)[0].split(":", 1)[0]
            patterns = host_patterns_by_tool.get(call.name, ())
            ok = False
            for pat in patterns:
                try:
                    if re.match(pat, host):
                        ok = True
                        break
                except re.error:
                    continue
            if not ok:
                _log.warning(
                    "tool.host_blocked",
                    extra={"tool": call.name, "host": host},
                )
                await get_current_thought_sink().emit(
                    "tool_failed",
                    {"tool": call.name, "host": host, "error": "host_not_allowed"},
                )
                results.append(
                    ToolResult(call.id, call.name, f"host not allowed: {host}", is_error=True)
                )
                continue

        attempts = 0
        last_exc: BaseException | None = None
        while attempts <= max_retries:
            try:
                # FR-021: log call with redacted args, NOT raw PHI/PQR text.
                _log.info(
                    "tool.invoke",
                    extra={
                        "tool": call.name,
                        "args_redacted": _redact(call.arguments, spec.redact_arg_keys),
                        "attempt": attempts,
                    },
                )
                output = await spec.handler(dict(call.arguments))
                _log.info(
                    "tool.ok",
                    extra={"tool": call.name, "attempt": attempts},
                )
                await get_current_thought_sink().emit(
                    "tool_succeeded",
                    {"tool": call.name, "attempt": attempts},
                )
                results.append(ToolResult(call.id, call.name, output))
                last_exc = None
                break
            except Exception as exc:  # noqa: BLE001 — outage recovery is intentional
                last_exc = exc
                attempts += 1
                if attempts > max_retries:
                    break
                await asyncio.sleep(retry_base_seconds * (2 ** (attempts - 1)))

        if last_exc is not None:
            _log.warning(
                "tool.failed",
                extra={"tool": call.name, "error_type": type(last_exc).__name__},
            )
            await get_current_thought_sink().emit(
                "tool_failed",
                {"tool": call.name, "error_type": type(last_exc).__name__},
            )
            results.append(
                ToolResult(
                    call.id,
                    call.name,
                    f"error: {type(last_exc).__name__}: {last_exc}",
                    is_error=True,
                )
            )

    return results


# ---------- helpers for adapters / agents ----------


def tool_result_message(result: ToolResult) -> Message:
    """Render a `ToolResult` as the `tool` message the LLM expects next turn."""
    payload = json.dumps(result.output, default=str)[:8000]
    return Message(
        role="tool",
        content=payload,
        name=result.name,
        tool_call_id=result.tool_call_id,
    )


async def run_with_tool_loop(
    *,
    llm: LLMClient,
    messages: list[Message],
    tools: Mapping[str, ToolSpec],
    chat_fn: Callable[..., Awaitable[ChatResponse]],
    max_steps: int = 4,
    extra_chat_kwargs: Mapping[str, Any] | None = None,
) -> ChatResponse:
    """Drive an agentic loop: LLM -> (tool calls?) -> execute -> feed back -> repeat.

    `chat_fn` is the adapter-bound function that takes (messages, tools=..., **kwargs)
    and returns a ChatResponse. Keeping the actual chat implementation in adapters
    means tool-calling semantics are negotiated per-provider (OpenAI tool_calls,
    Anthropic tool_use blocks, etc.).
    """
    kwargs: dict[str, Any] = dict(extra_chat_kwargs or {})
    last_resp: ChatResponse = ChatResponse(text="")
    history: list[Message] = list(messages)
    sink = _current_thought_sink.get()
    for step in range(max_steps):
        last_resp = await chat_fn(history, tools=list(tools.values()), **kwargs)
        history.append(
            Message(
                role="assistant",
                content=last_resp.text,
                tool_calls=last_resp.tool_calls or None,
            )
        )
        # Surface the chain-of-thought separately so the UI can render it
        # as a distinct "thinking" thought. The thinking variant of the
        # model returns the rationale in `ChatResponse.reasoning`.
        if last_resp.reasoning:
            await sink.emit(
                "thinking",
                {
                    "step": step,
                    "text": last_resp.reasoning,
                },
            )
        # Surface this LLM step (text reasoning + tool-calls it wants to make).
        await sink.emit(
            "llm_step",
            {
                "step": step,
                "text": last_resp.text,
                "tool_calls": [
                    {
                        "name": tc.name,
                        "arguments_redacted": _redact(
                            tc.arguments,
                            next(iter(tools.values())).redact_arg_keys
                            if tools
                            else (),
                        ),
                    }
                    for tc in (last_resp.tool_calls or [])
                ] or None,
            },
        )
        if not last_resp.tool_calls:
            return last_resp
        results = await execute_tool_calls(last_resp.tool_calls, tools)
        for r in results:
            history.append(tool_result_message(r))
        # If every tool call errored, stop early to avoid infinite loops.
        if results and all(r.is_error for r in results):
            return last_resp
    return last_resp
