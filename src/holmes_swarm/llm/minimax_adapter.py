"""Minimax (minimax/MiniMax-M3) LLM adapter.

OpenAI-compatible chat-completions endpoint. The provider id is `minimax`; the
model id is `MiniMax-M3`. Reads `api_base` / `api_key_env` from `Settings.llm`.

Supports the OpenAI `tools` array (function-calling). Outbound HTTP goes
through the configured allow-listed httpx client when one is provided,
preserving FR-018 / FR-021 enforcement.

Thinking variant
----------------

When `thinking=True` (default), the adapter switches to the
`MiniMax-M3-thinking` model variant — the same model exposed by the
provider with explicit chain-of-thought reasoning enabled. The model's
reasoning arrives as a separate `reasoning_content` field in the
chat-completions response; we surface it as `ChatResponse.reasoning` so
the agent loop can publish each chunk as a `kind=thinking` thought to
the UI.

The reasoning field is sent verbatim as `thinking: {type: "enabled"}`
plus a `reasoning_effort` knob (low|medium|high) so providers that
honor those flags can throttle the chain-of-thought budget.
"""

from __future__ import annotations

import json
import os
import uuid
from typing import Any

import httpx

from .base import ChatResponse, Message, ToolCall, ToolSpec


# ---------- message serialiser ----------


def _serialize_message(m: Message) -> dict[str, Any]:
    """Render a `Message` in the OpenAI chat-completions wire format.

    The previous implementation only forwarded `role` + `content`, which
    silently dropped `tool_calls` on assistant messages and `tool_call_id`
    on tool messages. As soon as the agent loop entered its second turn the
    provider saw an orphaned `role="tool"` message and rejected the request
    with HTTP 400. We now emit the full envelope that tool-calling
    conversations require.
    """
    payload: dict[str, Any] = {"role": m.role}
    if m.content is not None:
        payload["content"] = m.content
    if m.name:
        payload["name"] = m.name
    if m.tool_call_id:
        payload["tool_call_id"] = m.tool_call_id
    if m.tool_calls:
        payload["tool_calls"] = [
            {
                "id": tc.id,
                "type": "function",
                "function": {
                    "name": tc.name,
                    "arguments": json.dumps(tc.arguments, ensure_ascii=False),
                },
            }
            for tc in m.tool_calls
        ]
    return payload


# Model id resolver ----------------------------------------------------------


def resolve_thinking_model(model: str, override: str | None = None) -> str:
    """Return the thinking-variant model id.

    Default rule: append `-thinking` to the base model id. Examples:
        MiniMax-M3          -> MiniMax-M3-thinking
        MiniMax-M3-preview  -> MiniMax-M3-preview-thinking

    A custom `override` wins when supplied (config: `llm.thinking_model`).
    """
    if override:
        return override
    return f"{model}-thinking"


class MinimaxLLMClient:
    def __init__(
        self,
        api_base: str,
        model: str,
        api_key_env: str = "MINIMAX_API_KEY",
        *,
        http_client: httpx.AsyncClient | None = None,
        thinking: bool = True,
        thinking_model: str | None = None,
        reasoning_effort: str | None = "medium",
        send_thinking_flags: bool = False,
    ) -> None:
        self.api_base = api_base.rstrip("/")
        self.base_model = model
        self.thinking = thinking
        self.thinking_model = resolve_thinking_model(model, thinking_model)
        self.reasoning_effort = reasoning_effort
        self.api_key = os.environ.get(api_key_env, "")
        self._http = http_client
        self.send_thinking_flags = send_thinking_flags

    @property
    def active_model(self) -> str:
        """The model id this client will actually invoke.

        The `minimax` provider exposes a single chat-completions model id
        (`MiniMax-M3`) and rejects 400 on any `-thinking` variant id, so
        we always return the base model id. `resolve_thinking_model` and
        `thinking_model` are kept around for forward-compatibility with
        providers that do expose a separate thinking variant.
        """
        return self.base_model

    async def chat(
        self,
        messages: list[Message],
        tools: list[ToolSpec] | None = None,
        **kwargs: Any,
    ) -> ChatResponse:
        url = f"{self.api_base}/chat/completions"
        body: dict[str, Any] = {
            "model": self.active_model,
            "messages": [_serialize_message(m) for m in messages],
            **kwargs,
        }
        if tools:
            body["tools"] = [
                {
                    "type": "function",
                    "function": {
                        "name": t.name,
                        "description": t.description,
                        "parameters": t.parameters,
                    },
                }
                for t in tools
            ]
        # When tools are declared, force the provider to use them rather
        # than guess a tool_choice default; some providers reject with 400
        # if the conversation includes role="tool" messages without an
        # explicit tool_choice set.
        if tools and "tool_choice" not in body:
            body["tool_choice"] = "auto"
        # Thinking-mode flags. Providers (OpenRouter, DeepSeek, MiniMax M3
        # thinking variant, etc.) honour either `reasoning_effort` or
        # `thinking: {type: "enabled"}`; we send both for portability.
        # Thinking-mode flags. The provider exposes only a single chat-
        # completions model id and does NOT advertise a separate
        # `-thinking` variant — passing `reasoning_effort` / `thinking` in
        # the body returns HTTP 400. We therefore keep the base model id
        # and forward thinking toggles only when the provider is known to
        # accept them (controlled via the `send_thinking_flags` flag,
        # default off because the only currently supported provider,
        # `minimax`, rejects unknown body fields).
        if self.thinking and self.send_thinking_flags:
            body.setdefault("reasoning_effort", self.reasoning_effort)
            body.setdefault("thinking", {"type": "enabled"})

        headers = {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}
        client = self._http or httpx.AsyncClient(timeout=30.0)
        owns_client = self._http is None
        try:
            resp = await client.post(url, json=body, headers=headers)
            resp.raise_for_status()
            data = resp.json()
        finally:
            if owns_client:
                await client.aclose()

        msg = data.get("choices", [{}])[0].get("message", {}) or {}
        text = msg.get("content", "") or ""
        # The thinking variant puts chain-of-thought in a sibling field.
        # Providers use slightly different keys; we try them in order so
        # the agent loop can render the reasoning trace regardless of
        # which one the model speaks.
        reasoning = (
            msg.get("reasoning_content")
            or msg.get("reasoning")
            or msg.get("thinking")
            or ""
        )
        # When the thinking variant returns an empty `content` but the JSON
        # verdict the agent asked for is embedded inside the chain-of-thought
        # blob (some MiniMax-M3 responses do exactly this), the verdict would
        # otherwise be invisible to downstream parsers that only read
        # `ChatResponse.text`. Surface it as `text` too so `parse_verdict`
        # can find it. Reasoning is kept verbatim for the UI.
        if not text.strip() and reasoning:
            stripped = reasoning.strip()
            looks_like_json = stripped.startswith(("{", "```")) or "{" in stripped[:64]
            if looks_like_json:
                text = stripped
        tool_calls: list[ToolCall] = []
        for tc in msg.get("tool_calls", []) or []:
            fn = tc.get("function", {}) or {}
            try:
                args = json.loads(fn.get("arguments", "{}") or "{}")
            except json.JSONDecodeError:
                args = {}
            # Providers sometimes return tool calls without an `id` (or with
            # one that's not a non-empty string). The OpenAI-compatible spec
            # requires a non-empty id because the next turn's `role="tool"`
            # message is keyed by `tool_call_id`; if we forward an empty id
            # the provider rejects the follow-up turn with HTTP 400.
            tc_id = tc.get("id")
            if not isinstance(tc_id, str) or not tc_id:
                tc_id = f"call_{uuid.uuid4().hex[:24]}"
            tool_calls.append(
                ToolCall(id=tc_id, name=fn.get("name", ""), arguments=args)
            )
        return ChatResponse(
            text=text,
            raw=data,
            tool_calls=tool_calls,
            reasoning=str(reasoning) if reasoning else "",
        )
