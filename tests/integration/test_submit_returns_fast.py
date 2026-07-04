"""Regression tests for the UI-not-seeing-agents bug.

Original symptom: running an investigation through /chat showed no live
agent progress because `InvestigationService.submit()` was awaiting the
whole investigation before returning the request_id, so the SSE stream
attached by the UI only ever saw a replay of already-finished signals.

These tests verify:
- `submit()` returns promptly (before any agent finishes).
- An SSE subscriber attached right after submit() captures at least one
  `agent_started`/`signal`/`agent_completed` event.
- The chat parser still selects the right agent subset for the canonical
  Ciro Alfonso / SUBRED phrase.
"""

from __future__ import annotations

import asyncio
import time

import httpx
import pytest

from holmes_swarm.api.app import build_app


@pytest.fixture
def app():
    return build_app(config_path="config/example.yml")


def test_submit_returns_before_run_completes(app):
    """submit() must NOT block on the agent run — it returns a request_id
    that the caller can poll or stream."""
    transport = httpx.ASGITransport(app=app)

    async def _go():
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
            t0 = time.monotonic()
            r = await c.post(
                "/chat",
                json={
                    "message": (
                        "Encuentra movimientos alarmantes del Dr. Ciro Alfonso "
                        "Gómez Meisel de la Clínica Meisel SAS en la SUBRED "
                        "INTEGRADA DE SERVICIOS DE SALUD Norte y Sur"
                    ),
                    "auto_submit": True,
                },
                headers={"Authorization": "Bearer demo-token-esteban"},
            )
            elapsed = time.monotonic() - t0
            assert r.status_code == 200, r.text
            data = r.json()
            assert data["request_id"]
            # Allow generous slack: 5s is enough even for a slow LLM mock.
            assert elapsed < 5.0, f"submit() took {elapsed:.2f}s — looks blocking"
            return data["request_id"]

    rid = asyncio.run(_go())
    assert rid


def test_chat_parser_picks_at_least_three_agents_for_canonical_phrase():
    from holmes_swarm.api.chat import _guess_agents

    text = (
        "Encuentra movimientos alarmantes del Dr. Ciro Alfonso Gómez Meisel "
        "de la Clínica Meisel SAS en la SUBRED INTEGRADA DE SERVICIOS DE "
        "SALUD Norte y Sur"
    )
    agents = _guess_agents(text)
    # The classic Cartel phrase triggers contracting (subred), logistics (movimient),
    # and medical (clínica). Whistleblower is optional but harmless.
    assert set(agents) >= {"contracting", "logistics", "medical"}


def test_sse_stream_emits_agent_events(app):
    """Attach a subscriber right after submit() and confirm we see at least
    one agent_started signal before the stream completes."""
    transport = httpx.ASGITransport(app=app)

    async def _go():
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
            # Submit
            r = await c.post(
                "/chat",
                json={"message": "Investiga fraude con whatsapp de la clínica x", "auto_submit": True},
                headers={"Authorization": "Bearer demo-token-esteban"},
            )
            data = r.json()
            rid = data["request_id"]
            stream_url = f"/investigations/{rid}/stream?token=demo-token-esteban"

            # Subscribe
            seen_kinds = set()
            async with c.stream("GET", stream_url) as resp:
                async for line in resp.aiter_lines():
                    if line.startswith("data:"):
                        line = line[len("data:"):].strip()
                        if not line or line == "{}":
                            continue
                        import json
                        try:
                            evt = json.loads(line)
                        except Exception:
                            continue
                        seen_kinds.add(evt.get("kind"))
                        if evt.get("kind") == "completed":
                            break
                        if len(seen_kinds) >= 6 and "agent_completed" in seen_kinds:
                            break
            return seen_kinds

    kinds = asyncio.run(_go())
    # We expect at least an agent_started + agent_completed pair
    assert "agent_started" in kinds, f"no agent_started seen; got {kinds}"
    assert "agent_completed" in kinds, f"no agent_completed seen; got {kinds}"
    assert "completed" in kinds, f"no completed event; got {kinds}"


def test_sse_stream_emits_agent_thoughts(app):
    """For each running agent, the stream must emit at least one
    `agent_thought` event so the UI can render per-agent reasoning in
    real time. Without this, the user only sees the high-level state and
    never the per-agent reasoning trace."""
    transport = httpx.ASGITransport(app=app)

    async def _go():
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
            r = await c.post(
                "/chat",
                json={
                    "message": (
                        "Encuentra movimientos alarmantes del Dr. Ciro Alfonso "
                        "Gómez Meisel de la Clínica Meisel SAS en la SUBRED "
                        "INTEGRADA DE SERVICIOS DE SALUD Norte y Sur"
                    ),
                    "auto_submit": True,
                },
                headers={"Authorization": "Bearer demo-token-esteban"},
            )
            data = r.json()
            rid = data["request_id"]
            stream_url = f"/investigations/{rid}/stream?token=demo-token-esteban"

            thought_messages: list[str] = []
            agents_with_thought: set[str] = set()
            async with c.stream("GET", stream_url) as resp:
                async for line in resp.aiter_lines():
                    if line.startswith("data:"):
                        line = line[len("data:"):].strip()
                        if not line or line == "{}":
                            continue
                        import json
                        try:
                            evt = json.loads(line)
                        except Exception:
                            continue
                        if evt.get("kind") == "agent_thought":
                            payload = evt.get("payload") or {}
                            msg = payload.get("message")
                            if msg:
                                thought_messages.append(msg)
                                agents_with_thought.add(evt.get("agent_id") or "")
                        if evt.get("kind") == "completed":
                            break
            return thought_messages, agents_with_thought

    messages, agents = asyncio.run(_go())
    assert messages, "no agent_thought events were emitted"
    assert len(agents) >= 2, f"expected thoughts for >=2 agents, got {agents}"


def test_llm_failure_surfaces_as_thought_and_does_not_crash_run(app, monkeypatch):
    """When the LLM client raises (e.g. DNS failure, network outage), each
    agent must surface the error as a `kind=note` thought explaining the
    fallback, and the deterministic rules must still produce a result.
    The investigation should end in `completed`, not `failed`."""
    from holmes_swarm.investigations import service as svc_mod

    transport = httpx.ASGITransport(app=app)

    async def _chat_failure(messages, tools=None, **kwargs):
        raise OSError(8, "nodename nor servname provided, or not known")

    class _BoomLLM:
        async def chat(self, messages, **kwargs):
            raise OSError(8, "nodename nor servname provided, or not known")

    # Monkeypatch the LLM on every agent + the service so the chat endpoint
    # also uses the broken client (chat.py uses llm.chat directly).
    app.state.llm.chat = _BoomLLM().chat  # type: ignore[attr-defined]
    for a in app.state.registry.all():
        if hasattr(a, "llm") and a.llm is not None:
            a.llm = app.state.llm

    async def _go():
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
            r = await c.post(
                "/chat",
                json={"message": "Investiga la clínica x", "auto_submit": True},
                headers={"Authorization": "Bearer demo-token-esteban"},
            )
            data = r.json()
            rid = data["request_id"]
            stream_url = f"/investigations/{rid}/stream?token=demo-token-esteban"

            error_notes: list[tuple[str, str]] = []  # (agent_id, message)
            async with c.stream("GET", stream_url) as resp:
                async for line in resp.aiter_lines():
                    if line.startswith("data:"):
                        line = line[len("data:"):].strip()
                        if not line or line == "{}":
                            continue
                        import json
                        try:
                            evt = json.loads(line)
                        except Exception:
                            continue
                        if evt.get("kind") == "agent_thought":
                            payload = evt.get("payload") or {}
                            msg = payload.get("message") or ""
                            if "no disponible" in msg or "Error durante" in msg:
                                error_notes.append(
                                    (evt.get("agent_id") or "?", msg)
                                )
                        if evt.get("kind") == "completed":
                            break
            return error_notes

    notes = asyncio.run(_go())
    assert notes, f"expected at least one error/fallback note; got {notes}"


def test_sse_stream_emits_thinking_events(app):
    """When the LLM returns a `reasoning` payload (M3-thinking variant),
    the agent loop must surface it as `kind=thinking` agent_thought
    events so the UI can render the chain-of-thought trace."""
    from holmes_swarm.llm.base import ChatResponse

    transport = httpx.ASGITransport(app=app)

    class _ThinkingLLM:
        async def chat(self, messages, **kwargs):
            # First call returns chain-of-thought + empty content.
            return ChatResponse(
                text="",
                reasoning=(
                    "Verifying the procedure volume against the SOAT cap.\n"
                    "Querying the local RAG for the reference tariff.\n"
                    "Conclusion: monthly volume exceeds the threshold."
                ),
            )

    app.state.llm.chat = _ThinkingLLM().chat  # type: ignore[attr-defined]
    for a in app.state.registry.all():
        if hasattr(a, "llm") and a.llm is not None:
            a.llm = app.state.llm

    async def _go():
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
            r = await c.post(
                "/chat",
                json={"message": "Investiga fraude con whatsapp de la clínica x", "auto_submit": True},
                headers={"Authorization": "Bearer demo-token-esteban"},
            )
            data = r.json()
            rid = data["request_id"]
            stream_url = f"/investigations/{rid}/stream?token=demo-token-esteban"
            thinking_lines: list[str] = []
            async with c.stream("GET", stream_url) as resp:
                async for line in resp.aiter_lines():
                    if line.startswith("data:"):
                        line = line[len("data:"):].strip()
                        if not line or line == "{}":
                            continue
                        import json
                        try:
                            evt = json.loads(line)
                        except Exception:
                            continue
                        if evt.get("kind") == "agent_thought":
                            payload = evt.get("payload") or {}
                            if payload.get("kind") == "thinking":
                                thinking_lines.append(payload.get("message") or "")
                        if evt.get("kind") == "completed":
                            break
            return thinking_lines

    lines = asyncio.run(_go())
    assert lines, "no kind=thinking events were emitted"
    assert any("SOAT" in l or "Verifying" in l for l in lines), f"unexpected thinking lines: {lines}"


def test_ui_assets_served_with_no_cache_headers_and_cache_buster(app):
    """The HTML index must reference JS/CSS with a per-file `?v=<mtime>`
    cache-buster AND the response headers must disable browser caching.
    Without this, every UI fix is invisible until the user does a hard
    reload (Cmd+Shift+R)."""
    transport = httpx.ASGITransport(app=app)

    async def _go():
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
            # 1. HTML index has cache-buster.
            r = await c.get("/")
            assert r.status_code == 200
            assert "no-store" in r.headers.get("cache-control", "").lower()
            body = r.text
            assert "/ui/styles.css?v=" in body, f"no cache-buster on styles.css; body={body[:600]}"
            assert "/ui/app.js?v=" in body, f"no cache-buster on app.js; body={body[:600]}"

            # 2. JS/CSS assets themselves carry no-cache headers.
            for path in ("/ui/styles.css", "/ui/app.js"):
                r2 = await c.get(path)
                assert r2.status_code == 200, f"{path} returned {r2.status_code}"
                assert "no-store" in r2.headers.get("cache-control", "").lower(), (
                    f"{path} missing no-store: {dict(r2.headers)}"
                )

            # 3. The cache-buster URL still resolves (no path mismatch).
            r3 = await c.get("/ui/styles.css?v=12345")
            assert r3.status_code == 200

    asyncio.run(_go())
