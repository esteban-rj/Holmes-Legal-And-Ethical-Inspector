"""Chat endpoint: natural-language -> investigation request."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request, status

from ..chat import ChatRequest, ChatResponse, parse_chat
from ...llm.base import LLMClient
from ...llm.mock_adapter import MockLLMClient
from ...llm.minimax_adapter import MinimaxLLMClient
from .investigations import _principal


router = APIRouter(prefix="/chat", tags=["chat"])


@router.post("", response_model=ChatResponse)
async def chat(body: ChatRequest, request: Request, requester_id: str = Depends(_principal)) -> ChatResponse:
    svc = request.app.state.investigation_service
    settings = request.app.state.settings
    llm_client: LLMClient
    if settings.llm.provider == "minimax":
        llm_client = MinimaxLLMClient(
            api_base=settings.llm.api_base,
            model=settings.llm.model,
            api_key_env=settings.llm.api_key_env,
        )
    else:
        llm_client = MockLLMClient()

    registry = request.app.state.registry
    parsed = await parse_chat(llm_client, registry, body.message)

    if not body.auto_submit:
        return ChatResponse(parsed=parsed, message="Parsed (not submitted).")

    # Validate agents against the registry
    valid = {a.id for a in registry.all() if a.id != "consensus"}
    agents = [a for a in (parsed.agents or []) if a in valid] or list(valid)

    scope: dict = {}
    if parsed.location:
        scope["location"] = parsed.location
    if parsed.procedure:
        scope["procedure"] = parsed.procedure
    if parsed.date_from:
        scope["date_from"] = parsed.date_from
    if parsed.date_to:
        scope["date_to"] = parsed.date_to
    if parsed.narrative:
        scope["narrative"] = parsed.narrative

    try:
        req = await svc.submit(
            requester_id=requester_id,
            target_entity_id=parsed.target_entity_id,
            agents=agents,
            scope=scope,
        )
    except ValueError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc))

    return ChatResponse(
        parsed=parsed,
        request_id=str(req.id),
        status_url=f"/investigations/{req.id}",
        stream_url=f"/investigations/{req.id}/stream",
        message=f"Investigation submitted for {parsed.target_entity_id}.",
    )