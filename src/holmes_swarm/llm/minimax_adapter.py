"""Minimax (minimax/MiniMax-M3) LLM adapter.

OpenAI-compatible chat-completions endpoint. The provider id is `minimax`; the
model id is `MiniMax-M3`. Reads `api_base` / `api_key_env` from `Settings.llm`.

Supports the OpenAI `tools` array (function-calling). Outbound HTTP goes
through the configured allow-listed httpx client when one is provided,
preserving FR-018 / FR-021 enforcement.
"""

from __future__ import annotations

import json
import os
from typing import Any

import httpx

from .base import ChatResponse, Message, ToolCall, ToolSpec


class MinimaxLLMClient:
    def __init__(
        self,
        api_base: str,
        model: str,
        api_key_env: str = "MINIMAX_API_KEY",
        *,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self.api_base = api_base.rstrip("/")
        self.model = model
        self.api_key = os.environ.get(api_key_env, "")
        self._http = http_client

    async def chat(
        self,
        messages: list[Message],
        tools: list[ToolSpec] | None = None,
        **kwargs: Any,
    ) -> ChatResponse:
        url = f"{self.api_base}/chat/completions"
        body: dict[str, Any] = {
            "model": self.model,
            "messages": [{"role": m.role, "content": m.content} for m in messages],
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

        text = data.get("choices", [{}])[0].get("message", {}).get("content", "") or ""
        tool_calls: list[ToolCall] = []
        for tc in data.get("choices", [{}])[0].get("message", {}).get("tool_calls", []) or []:
            fn = tc.get("function", {}) or {}
            try:
                args = json.loads(fn.get("arguments", "{}") or "{}")
            except json.JSONDecodeError:
                args = {}
            tool_calls.append(
                ToolCall(id=tc.get("id", ""), name=fn.get("name", ""), arguments=args)
            )
        return ChatResponse(text=text, raw=data, tool_calls=tool_calls)
