"""LLM adapter tests."""

from __future__ import annotations

import asyncio

from holmes_swarm.llm.base import Message
from holmes_swarm.llm.mock_adapter import MockLLMClient


def test_mock_extracts_pqr_modus():
    client = MockLLMClient()
    resp = asyncio.run(client.chat([Message(role="user", content="PQR: uso de WhatsApp y auxiliares para captar pacientes, posible fraude")]))
    assert "whatsapp" in resp.text or "modus_operandi" in resp.text


def test_mock_offline_default():
    client = MockLLMClient()
    resp = asyncio.run(client.chat([Message(role="user", content="hello")]))
    assert resp.text == "[mock] no-op"
