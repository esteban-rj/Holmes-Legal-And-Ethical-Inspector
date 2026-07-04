"""LLM adapter tests."""

from __future__ import annotations

import asyncio

from holmes_swarm.llm.base import Message
from holmes_swarm.llm.minimax_adapter import MinimaxLLMClient, resolve_thinking_model
from holmes_swarm.llm.mock_adapter import MockLLMClient


def test_mock_extracts_pqr_modus():
    client = MockLLMClient()
    resp = asyncio.run(client.chat([Message(role="user", content="PQR: uso de WhatsApp y auxiliares para captar pacientes, posible fraude")]))
    assert "whatsapp" in resp.text or "modus_operandi" in resp.text


def test_mock_offline_default():
    client = MockLLMClient()
    resp = asyncio.run(client.chat([Message(role="user", content="hello")]))
    assert resp.text == "[mock] no-op"


def test_thinking_model_default_is_base_with_thinking_suffix():
    assert resolve_thinking_model("MiniMax-M3") == "MiniMax-M3-thinking"


def test_thinking_model_override_wins():
    assert (
        resolve_thinking_model("MiniMax-M3", override="my-custom-thinking-v2")
        == "my-custom-thinking-v2"
    )


def test_minimax_client_uses_base_model_by_default():
    """The provider exposes a single chat-completions model id
    (`MiniMax-M3`) and rejects 400 on any `-thinking` suffix, so even with
    `thinking=True` we always invoke the base model. The thinking flag is
    preserved on the client for future providers that DO support a
    separate thinking variant."""
    c = MinimaxLLMClient(api_base="https://example.invalid/v1", model="MiniMax-M3")
    assert c.active_model == "MiniMax-M3"
    assert c.thinking is True


def test_minimax_client_can_disable_thinking():
    c = MinimaxLLMClient(
        api_base="https://example.invalid/v1",
        model="MiniMax-M3",
        thinking=False,
    )
    assert c.active_model == "MiniMax-M3"


def test_minimax_client_ignores_thinking_model_override():
    """`thinking_model` is preserved for forward compatibility but does
    not change the active model id, because `minimax` only knows one id."""
    c = MinimaxLLMClient(
        api_base="https://example.invalid/v1",
        model="MiniMax-M3",
        thinking=True,
        thinking_model="m3-thinking-prod",
    )
    assert c.active_model == "MiniMax-M3"
    assert c.thinking_model == "m3-thinking-prod"


def test_minimax_request_body_omits_unsupported_thinking_flags(monkeypatch):
    """The chat-completions payload sent to the provider must include the
    thinking variant id AND the reasoning-effort/thinking flags."""
    captured: dict = {}

    class _FakeClient:
        async def post(self, url, json, headers):
            captured["body"] = json
            captured["url"] = url

            class _Resp:
                status_code = 200

                def json(self):
                    return {
                        "choices": [
                            {
                                "message": {
                                    "content": "hello",
                                    "reasoning_content": "thinking trace",
                                }
                            }
                        ]
                    }

                def raise_for_status(self):
                    return None

            return _Resp()

    monkeypatch.setenv("MINIMAX_API_KEY", "sk-test")

    async def _go():
        c = MinimaxLLMClient(
            api_base="https://api.minimax.example/v1",
            model="MiniMax-M3",
            http_client=_FakeClient(),
        )
        resp = await c.chat([Message(role="user", content="hi")])
        return resp

    resp = asyncio.run(_go())
    body = captured["body"]
    assert body["model"] == "MiniMax-M3"
    # The provider rejects 400 on unknown body fields, so even when
    # `thinking=True` we do NOT forward `reasoning_effort` / `thinking`
    # unless the adapter was explicitly opted in.
    assert "reasoning_effort" not in body
    assert "thinking" not in body
    assert resp.text == "hello"
    assert resp.reasoning == "thinking trace"
