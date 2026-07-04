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
            thinking=settings.llm.thinking,
            thinking_model=settings.llm.thinking_model,
            reasoning_effort=settings.llm.reasoning_effort,
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
    if "contracting" in settings.agents:
        contracting = ContractingAgent(
            llm=llm,
            http_client=contracting_client,
            retriever=retriever,
            explore_allowed_hosts=_explore_hosts("contracting"),
        )
    else:
        contracting = ContractingAgent(llm=llm)

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

    # Static UI (chat + live agent progress).
    #
    # The browser loves to cache `app.js` and `styles.css` aggressively, and
    # every fix in those files is invisible until the user does a hard reload
    # (Cmd+Shift+R). To make life easier we wrap the assets in a tiny router
    # that emits `Cache-Control: no-store` AND lets the HTML reference each
    # file with a `?v=<mtime>` cache-buster so a normal reload always picks
    # up the latest bytes from disk.
    from fastapi.responses import FileResponse, HTMLResponse, PlainTextResponse
    from starlette.staticfiles import StaticFiles

    _ui_dir = Path(__file__).resolve().parent.parent / "ui"
    _ui_dir.mkdir(parents=True, exist_ok=True)

    _NO_CACHE = {
        "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
        "Pragma": "no-cache",
        "Expires": "0",
    }

    @app.get("/ui/{asset:path}", include_in_schema=False)
    def _ui_asset(asset: str):
        # Resolve with a guard against path traversal (asset is provided by
        # the URL, not the filesystem).
        target = (_ui_dir / asset).resolve()
        if _ui_dir.resolve() not in target.parents and target != _ui_dir.resolve():
            from fastapi import HTTPException

            raise HTTPException(404, "unknown asset")
        if not target.exists() or target.is_dir():
            from fastapi import HTTPException

            raise HTTPException(404, "unknown asset")
        if target.suffix == ".html":
            body = target.read_text(encoding="utf-8")
            # Inject a per-file mtime cache-buster into <link>/<script>
            # references so the browser always re-fetches the new bytes.
            import re

            def _bust(match: re.Match[str]) -> str:
                tag = match.group(1)
                attr = match.group(2)
                url = match.group(4)  # the /ui/... URL is group 4 (group 3 is the quote)
                rel = url[len("/ui/"):] if url.startswith("/ui/") else url.lstrip("/")
                file_path = (_ui_dir / rel).resolve()
                if not file_path.exists() or _ui_dir not in file_path.parents:
                    return match.group(0)
                stamp = int(file_path.stat().st_mtime)
                return f"{tag} {attr}={url}?v={stamp}"

            body = re.sub(
                r'(<link|<script)\s+(href|src)=(["\'])(/ui/[^"\']+)\3',
                _bust,
                body,
            )
            return HTMLResponse(body, headers=_NO_CACHE)
        if target.suffix == ".js":
            return PlainTextResponse(
                target.read_text(encoding="utf-8"),
                media_type="application/javascript",
                headers=_NO_CACHE,
            )
        if target.suffix == ".css":
            return PlainTextResponse(
                target.read_text(encoding="utf-8"),
                media_type="text/css",
                headers=_NO_CACHE,
            )
        return FileResponse(str(target), headers=_NO_CACHE)

    def _serve_index() -> HTMLResponse:
        import re

        index_path = _ui_dir / "index.html"
        if not index_path.exists():
            return HTMLResponse(
                "<h1>UI not installed</h1>", status_code=500, headers=_NO_CACHE
            )
        body = index_path.read_text(encoding="utf-8")

        def _bust(match: re.Match[str]) -> str:
            prefix = match.group(1)
            quote = match.group(2)
            url = match.group(3)
            # url is something like "/ui/styles.css"; resolve by dropping the
            # leading "/ui/" prefix so it joins cleanly under _ui_dir.
            rel = url[len("/ui/"):] if url.startswith("/ui/") else url.lstrip("/")
            file_path = (_ui_dir / rel).resolve()
            if not file_path.exists() or _ui_dir not in file_path.parents:
                return match.group(0)
            stamp = int(file_path.stat().st_mtime)
            return f"{prefix}{quote}{url}?v={stamp}{quote}"

        body = re.sub(
            r'(<(?:link|script)[^>]*?(?:href|src)=)(["\'])(/ui/[^"\']+)\2',
            _bust,
            body,
        )
        return HTMLResponse(body, headers=_NO_CACHE)

    @app.get("/ui", include_in_schema=False)
    @app.get("/ui/", include_in_schema=False)
    def _ui_root() -> HTMLResponse:
        return _serve_index()

    @app.get("/", include_in_schema=False)
    def index() -> HTMLResponse:
        return _serve_index()

    return app


# Default app for `uvicorn holmes_swarm.api.app:app`
_DEFAULT_CONFIG = os.environ.get("HOLMES_CONFIG", "config/example.yml")
app = build_app(config_path=_DEFAULT_CONFIG)
