"""Internal helpers shared by every LLM-driven agent.

* `make_signal` — central Signal factory that always respects FR-031/FR-032
  origin rules. Replaces the bespoke `_make_signal` clones in each agent.
* `user_batch_message` / `system_block` — small builders to keep prompts
  consistent across agents. The system prompt injected via
  ``user_batch_message`` ends with the conclusion JSON schema (``verdict`` /
  ``confidence`` / ``summary`` ≤100 words) so each agent produces a chat-ready
  conclusion in addition to its structured verdict.
* `emit_conclusion` — helper agents use to forward the chat-shaped conclusion
  to the UI through their thought sink. No-op when no sink is configured.
* `parse_verdict` — legacy extractor for ``{"signals":[...]}`` envelopes.
* `parse_conclusion` — extractor for ``{"verdict","confidence","summary"}``
  conclusion envelopes. Always returns ``inconclusive`` on parse failure and
  enforces the 100-word / 600-char ceiling on ``summary``.
* `run_agent_loop` — convenience wrapper around `run_with_tool_loop` that
  injects the agent's system prompt, parses the final JSON verdict and
  conclusion, and lets the agent fall back to deterministic rules if the LLM
  ``no-op``s.
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


async def emit_conclusion(sink: ThoughtSink | None, conclusion: Mapping[str, Any]) -> None:
    """Forward a parsed chat-conclusion to the UI via the agent's thought sink.

    The InvestigationService watches the sink for ``kind='conclusion'`` events
    and translates them into ``agent_conclusion`` SSE events so the chat pane
    can render one bubble per agent. Silently no-ops when no sink is wired.
    """
    if sink is None:
        return
    payload = {
        "kind": "conclusion",
        "verdict": conclusion.get("verdict", "inconclusive"),
        "confidence": conclusion.get("confidence", 0.0),
        "summary": conclusion.get("summary", ""),
    }
    try:
        await sink.emit("conclusion", payload)
    except Exception:
        # Never let a sink failure mask the agent's actual result.
        pass


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
            "Analyse the data and conclude with a verdict for the chat. "
            "Respond with STRICT JSON matching this schema: "
            "{\"verdict\": <one of 'suspicious'|'inconclusive'|'no_findings'>, "
            "\"confidence\": float in [0,1], "
            "\"summary\": <plain text, MUST be written in Spanish, stating your final "
            "verdict and the key evidence behind it>}. "
            "The summary is what the human will read in the chat; be specific "
            "and evidence-grounded. Aim for around 100 words, but you may use "
            "more if the evidence requires it. "
            "Return ONLY the JSON object, no prose."
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


_ALLOWED_VERDICTS = {"suspicious", "inconclusive", "no_findings"}


def _extract_json_object(text: str) -> Any:
    """Best-effort JSON object extraction from an LLM reply.

    Handles ```json fenced blocks and extra prose. Returns the parsed JSON
    object/dict, or None if nothing usable was found.
    """
    import json
    import re

    if not text:
        return None
    fence = re.search(r"```(?:json)?\s*([\s\S]+?)```", text, re.IGNORECASE)
    candidate = fence.group(1) if fence else text
    body = "".join(candidate.splitlines())
    for c in [body, text]:
        try:
            return json.loads(c)
        except json.JSONDecodeError:
            continue
    m = re.search(r"\{[\s\S]*\}", text)
    if m:
        try:
            return json.loads(m.group(0))
        except json.JSONDecodeError:
            return None
    return None


def parse_verdict(text: str) -> list[dict[str, Any]]:
    """Extract a JSON `{"signals":[...]}` verdict from an LLM reply.

    Tolerant: handles ```json fenced blocks and embedded JSON. Also tolerant
    to the verdict being a *list* of signals at the top level (some models
    forget the wrapping `{"signals": [...]}` envelope) and to extra prose
    around the JSON object.
    """
    obj = _extract_json_object(text)
    if obj is None:
        return []
    if isinstance(obj, list):
        return [s for s in obj if isinstance(s, dict)]
    if not isinstance(obj, dict):
        return []
    sigs = obj.get("signals")
    return [s for s in (sigs or []) if isinstance(s, dict)]


def parse_conclusion(text: str) -> dict[str, Any]:
    """Extract a `{"verdict","confidence","summary"}` conclusion from an LLM reply.

    Falls back to `inconclusive` if the JSON cannot be parsed. The summary is
    truncated to ~100 words (600 chars) to honour the contract the chat UI
    relies on.
    """
    obj = _extract_json_object(text)
    if not isinstance(obj, dict):
        return {"verdict": "inconclusive", "confidence": 0.0, "summary": ""}
    verdict = str(obj.get("verdict", "inconclusive")).strip().lower()
    if verdict not in _ALLOWED_VERDICTS:
        verdict = "inconclusive"
    try:
        confidence = float(obj.get("confidence", 0.0))
    except (TypeError, ValueError):
        confidence = 0.0
    confidence = max(0.0, min(1.0, confidence))
    summary = str(obj.get("summary", "") or "").strip()
    return {"verdict": verdict, "confidence": confidence, "summary": summary}


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
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Convenience: build history, drive the tool loop.

    Returns ``(signals_from_verdict, conclusion)``. ``signals_from_verdict``
    is the legacy ``[{"signal_type":..., "confidence":..., "evidence":...}, ...]``
    shape (kept for backwards compatibility with callers that still emit
    signals onto the blackboard). ``conclusion`` is the new chat-shaped
    dict ``{"verdict","confidence","summary"}`` used to render the per-agent
    conclusion bubble in the chat pane.

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
    signals = parse_verdict(final.text)
    conclusion = parse_conclusion(final.text)
    return signals, conclusion
