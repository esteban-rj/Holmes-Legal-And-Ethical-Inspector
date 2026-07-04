"""Internal helpers shared by every LLM-driven agent.

* `make_signal` — central Signal factory that always respects FR-031/FR-032
  origin rules. Replaces the bespoke `_make_signal` clones in each agent.
* `user_batch_message` / `system_block` — small builders to keep prompts
  consistent across agents.
* `run_agent_loop` — convenience wrapper around `run_with_tool_loop` that
  injects the agent's system prompt, parses the final JSON verdict, and
  lets the agent fall back to deterministic rules if the LLM `no-op`s.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from ..blackboard.schema import InvestigationScope, Signal
from ..llm.base import (
    LLMClient,
    Message,
    ThoughtSink,
    ToolSpec,
    run_with_tool_loop,
    set_current_thought_sink,
)

# ---------- signal factory ----------


def make_signal(
    *,
    agent_id: str,
    signal_type: str,
    entity_id: str,
    confidence: float,
    evidence: Mapping[str, Any],
    scope: Any,
    confidence_threshold: float,
) -> Signal:
    """Build a Signal honouring FR-031 origin and FR-011 below_threshold."""
    if isinstance(scope, InvestigationScope):
        origin = {
            "kind": "investigation",
            "investigation_request_id": str(scope.investigation_request_id),
        }
    else:
        origin = {"kind": "autonomous-monitoring"}
    return Signal(
        entity_id=entity_id,
        signal_type=signal_type,  # type: ignore[arg-type]
        source_agent=agent_id,
        confidence=confidence,
        evidence=dict(evidence),
        below_threshold=confidence < confidence_threshold,
        origin=origin,
    )


# ---------- prompt builders ----------


def system_block(system_prompt: str) -> Message:
    return Message(role="system", content=system_prompt.strip())


def user_batch_message(*, system_prompt: str, batch: Any, scope: Any) -> list[Message]:
    """Initial message list: system + task description containing the batch.

    The batch is JSON-serialised best-effort; pydantic models and dataclasses
    are stringified.
    """
    import json
    from datetime import date, datetime

    def default(o: Any) -> Any:
        if isinstance(o, (datetime, date)):
            return o.isoformat()
        if hasattr(o, "model_dump"):
            return o.model_dump()
        return repr(o)

    payload = {
        "batch": batch,
        "scope": {
            "kind": "investigation" if isinstance(scope, InvestigationScope) else "autonomous",
            "investigation_request_id": (
                str(scope.investigation_request_id) if isinstance(scope, InvestigationScope) else None
            ),
            "target_entity_id": getattr(scope, "target_entity_id", None),
        },
        "instructions": (
            "Analyse the data and emit zero or more Signals. Respond with JSON "
            "matching this schema: {\"signals\": [ {"
            "\"signal_type\": <one of the allowed types>, "
            "\"confidence\": float in [0,1], "
            "\"evidence\": object }, ... ]}. "
            "If you find no patterns, return {\"signals\": []}. "
            "Use the declared tools when you need external or local reference data."
        ),
    }
    user = Message(
        role="user",
        content=(
            f"{system_prompt.strip()}\n\nINPUT:\n"
            + json.dumps(payload, default=default, ensure_ascii=False)
        ),
    )
    return [system_block(system_prompt), user]


# ---------- verdict parsing ----------


def parse_verdict(text: str) -> list[dict[str, Any]]:
    """Extract a JSON `{"signals":[...]}` verdict from an LLM reply.

    Tolerant: handles ```json fenced blocks and embedded JSON.
    """
    import json
    import re

    if not text:
        return []
    # Try fenced block first.
    fence = re.search(r"```(?:json)?\s*([\s\S]+?)```", text, re.IGNORECASE)
    candidate = fence.group(1) if fence else text
    # Try the whole string, then progressively wider sub-strings.
    candidates = [candidate.strip()]
    for i, line in enumerate(candidate.splitlines()):
        stripped = line.strip()
        if stripped.startswith("{") and stripped.endswith("}"):
            candidates.append(stripped)
    body = "".join(candidates[0].splitlines())
    try:
        obj = json.loads(body)
    except json.JSONDecodeError:
        # Last-ditch: regex for the first {...} blob.
        m = re.search(r"\{[\s\S]*\}", text)
        if not m:
            return []
        try:
            obj = json.loads(m.group(0))
        except json.JSONDecodeError:
            return []
    if not isinstance(obj, dict):
        return []
    sigs = obj.get("signals")
    return [s for s in (sigs or []) if isinstance(s, dict)]


# ---------- agentic loop wrapper ----------


async def run_agent_loop(
    *,
    llm: LLMClient,
    system_prompt: str,
    tools: Sequence[ToolSpec],
    batch: Any,
    scope: Any,
    chat_fn,
    max_steps: int = 3,
    thought_sink: ThoughtSink | None = None,
    ctx: Any | None = None,  # AgentRuntimeContext — used as a fallback source of the sink
) -> list[dict[str, Any]]:
    """Convenience: build history, drive the tool loop, return the raw verdict signals (dicts).

    The thought sink resolution order:
    1. `thought_sink` arg (explicit override).
    2. `ctx.thought_sink` (per-run context supplied by the investigation service).
    3. No sink (the loop still runs, but no reasoning events are emitted).

    While the loop is active the sink is installed as the *active* sink via
    `set_current_thought_sink` so `run_with_tool_loop` / `execute_tool_calls`
    in `llm.base` can forward per-step reasoning without any extra wiring.
    """
    # Resolve sink
    sink: ThoughtSink | None = thought_sink
    if sink is None and ctx is not None:
        sink = getattr(ctx, "thought_sink", None)

    history = user_batch_message(system_prompt=system_prompt, batch=batch, scope=scope)

    if sink is not None:
        set_current_thought_sink(sink)
        try:
            await sink.emit(
                "note",
                {"message": f"Sistema: {system_prompt.strip()[:400]}"},
            )
            final = await run_with_tool_loop(
                llm=llm,
                messages=history,
                tools={t.name: t for t in tools},
                chat_fn=chat_fn,
                max_steps=max_steps,
            )
            await sink.emit(
                "note",
                {"message": f"Verdict: {final.text[:600]}"},
            )
        finally:
            # Restore a no-op sink
            set_current_thought_sink(ThoughtSink(None))
    else:
        final = await run_with_tool_loop(
            llm=llm,
            messages=history,
            tools={t.name: t for t in tools},
            chat_fn=chat_fn,
            max_steps=max_steps,
        )
    return parse_verdict(final.text)
