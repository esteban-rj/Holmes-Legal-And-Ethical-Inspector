"""FastAPI app wiring."""

from __future__ import annotations

import logging
import os
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse

from ..agents.consensus import ConsensusAgent
from ..agents.contracting import ContractingAgent
from ..agents.logistics import LogisticsAgent
from ..agents.medical import MedicalAgent
from ..agents.registry import AgentRegistry
from ..agents.whistleblower import WhistleblowerAgent
from ..blackboard.queue_bus import QueueBus
from ..config import Settings
from ..data_sources.default_loader import make_default_data_loader
from ..data_sources.secop import make_default_secop_source
from ..investigations.audit import AuditLog
from ..investigations.service import InvestigationService
from ..llm.base import LLMClient
from ..llm.minimax_adapter import MinimaxLLMClient
from ..llm.mock_adapter import MockLLMClient
from ..net.allowlist_client import make_allowlisted_client
from ..rag.langchain_retriever import load_default_retriever
from .auth import Auth
from .routes import agents as agents_routes
from .routes import alerts as alerts_routes
from .routes import chat as chat_routes
from .routes import investigations as investigations_routes
from .routes import signals as signals_routes

_log = logging.getLogger(__name__)


def build_app(*, config_path: str) -> FastAPI:
    settings = Settings.load(config_path)

    bus = QueueBus(dedup_window_seconds=settings.blackboard.dedup_window_seconds)
    registry = AgentRegistry()
    audit = AuditLog()

    # ---- LLM (shared across LLM-driven agents) ----
    if settings.llm.provider == "minimax":
        llm: LLMClient = MinimaxLLMClient(
            api_base=settings.llm.api_base,
            model=settings.llm.model,
            api_key_env=settings.llm.api_key_env,
        )
    else:
        llm = MockLLMClient()

    # ---- RAG (Medical Agent) ----
    corpora_dir = Path(__file__).resolve().parent.parent / "rag" / "corpora"
    retriever = load_default_retriever(corpora_dir)

    def _client_for(agent_cfg):
        return make_allowlisted_client(agent_cfg.internet_profile)

    def _explore_hosts(agent_id: str) -> tuple[str, ...]:
        cfg = settings.agents.get(agent_id)
        if cfg is None or cfg.internet_profile is None:
            return ()
        return tuple(cfg.internet_profile.explore_allowed_hosts or ())

    # ---- Contracting Agent ----
    _contracting_cfg = settings.agents.get("contracting")
    contracting_client = (
        _client_for(_contracting_cfg) if _contracting_cfg is not None else None
    )
    secop_source = make_default_secop_source(http_client=contracting_client)
    if "contracting" in settings.agents:
        contracting = ContractingAgent(
            llm=llm,
            http_client=contracting_client,
            retriever=retriever,
            secop_source=secop_source,
            explore_allowed_hosts=_explore_hosts("contracting"),
        )
    else:
        contracting = ContractingAgent(llm=llm, secop_source=secop_source)

    # ---- Logistics Agent ----
    logistics = (
        LogisticsAgent(
            llm=llm,
            http_client=_client_for(settings.agents["logistics"]),
            explore_allowed_hosts=_explore_hosts("logistics"),
        )
        if "logistics" in settings.agents
        else LogisticsAgent(llm=llm)
    )

    # ---- Medical Agent ----
    medical = MedicalAgent(llm=llm, retriever=retriever)

    # ---- Whistleblower Agent ----
    whistleblower = WhistleblowerAgent(
        llm=llm,
        http_client=(
            _client_for(settings.agents["whistleblower"])
            if "whistleblower" in settings.agents
            else None
        ),
        explore_allowed_hosts=(
            _explore_hosts("whistleblower")
            if "whistleblower" in settings.agents
            else ()
        ),
    )

    for a in (contracting, logistics, medical, whistleblower):
        if settings.agents.get(a.id, None) and not settings.agents[a.id].enabled:
            continue
        cfg = settings.agents.get(a.id)
        if cfg is not None:
            a.confidence_threshold = cfg.confidence_threshold
        registry.register(a)

    consensus = ConsensusAgent(
        bus=bus,
        llm=llm,
        staleness_window_seconds=settings.blackboard.staleness_window_seconds,
    )

    svc = InvestigationService(
        registry=registry,
        bus=bus,
        audit=audit,
        settings=settings,
        llm=llm,
        data_loader=make_default_data_loader(),
    )

    auth = Auth(tokens=settings.auth.tokens)

    app = FastAPI(title="Holmes Swarm API", version="0.1.0")
    app.state.bus = bus
    app.state.registry = registry
    app.state.audit = audit
    app.state.settings = settings
    app.state.investigation_service = svc
    app.state.consensus = consensus
    app.state.auth = auth
    app.state.llm = llm

    @app.on_event("startup")
    async def _start() -> None:
        consensus.start()

    @app.on_event("shutdown")
    async def _stop() -> None:
        await consensus.stop()
        await registry.shutdown_all()

    @app.get("/health")
    def health() -> dict:
        return {"status": "ok"}

    app.include_router(investigations_routes.router)
    app.include_router(signals_routes.router)
    app.include_router(alerts_routes.router)
    app.include_router(agents_routes.router)
    app.include_router(chat_routes.router)

    # Static UI (chat + live agent progress)
    from fastapi.staticfiles import StaticFiles
    _ui_dir = Path(__file__).resolve().parent.parent / "ui"
    _ui_dir.mkdir(parents=True, exist_ok=True)
    if any(_ui_dir.glob("*")):
        app.mount("/ui", StaticFiles(directory=str(_ui_dir), html=True), name="ui")

    @app.get("/", include_in_schema=False)
    def index() -> FileResponse:
        index_path = _ui_dir / "index.html"
        if index_path.exists():
            return FileResponse(str(index_path))
        return FileResponse(str(_ui_dir))

    return app


# Default app for `uvicorn holmes_swarm.api.app:app`
_DEFAULT_CONFIG = os.environ.get("HOLMES_CONFIG", "config/example.yml")
app = build_app(config_path=_DEFAULT_CONFIG)
