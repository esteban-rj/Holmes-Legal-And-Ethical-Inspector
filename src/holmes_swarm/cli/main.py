"""Typer CLI."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Optional

import httpx
import typer
import yaml

app = typer.Typer(help="Holmes Swarm CLI")


def _load_config(path: str):
    from holmes_swarm.config import Settings
    return Settings.load(path)


@app.command()
def run(config: str = typer.Option("config/example.yml", "--config", "-c")):
    """Run the swarm (in-process: spin up agents and consensus loop)."""
    import asyncio
    from holmes_swarm.api.app import build_app
    application = build_app(config_path=config)
    consensus = application.state.consensus

    async def _main():
        consensus.start()
        try:
            typer.echo(f"swarm running (config={config}); press Ctrl-C to stop")
            while True:
                await asyncio.sleep(1)
        except KeyboardInterrupt:
            pass
        finally:
            await consensus.stop()

    asyncio.run(_main())


@app.command()
def investigate(
    entity: str = typer.Option(..., "--entity"),
    agents: Optional[str] = typer.Option(None, "--agents"),
    config: str = typer.Option("config/example.yml", "--config", "-c"),
    token: str = typer.Option(..., envvar="HOLMES_TOKEN"),
    base_url: str = typer.Option("http://127.0.0.1:8000"),
):
    """Submit an investigation via the API."""
    body = {"target_entity_id": entity, "scope": {}}
    if agents:
        body["agents"] = [a.strip() for a in agents.split(",") if a.strip()]
    r = httpx.post(
        f"{base_url}/investigations",
        json=body,
        headers={"Authorization": f"Bearer {token}"},
        timeout=60,
    )
    typer.echo(r.status_code)
    typer.echo(r.text)


@app.command()
def alerts(
    entity: Optional[str] = typer.Option(None, "--entity"),
    config: str = typer.Option("config/example.yml", "--config", "-c"),
    token: str = typer.Option(..., envvar="HOLMES_TOKEN"),
    base_url: str = typer.Option("http://127.0.0.1:8000"),
):
    """List Critical Fraud Alerts."""
    params = {"entity_id": entity} if entity else {}
    r = httpx.get(
        f"{base_url}/alerts",
        params=params,
        headers={"Authorization": f"Bearer {token}"},
        timeout=30,
    )
    typer.echo(r.text)


@app.command()
def audit_log(
    since: Optional[str] = typer.Option(None, "--since"),
    config: str = typer.Option("config/example.yml", "--config", "-c"),
):
    """Print audit log from in-process build (no API)."""
    from holmes_swarm.api.app import build_app
    application = build_app(config_path=config)
    entries = application.state.audit.all()
    if since:
        from datetime import datetime
        s = datetime.fromisoformat(since.replace("Z", "+00:00"))
        entries = [e for e in entries if e.at >= s]
    typer.echo(json.dumps([e.model_dump(mode="json") for e in entries], indent=2, default=str))


@app.command()
def seed(
    fixtures: str = typer.Option("tests/fixtures/", "--fixtures"),
    autonomous_flood: int = typer.Option(0, "--autonomous-flood"),
    entity: Optional[str] = typer.Option(None, "--entity"),
    config: str = typer.Option("config/example.yml", "--config", "-c"),
):
    """Seed fixtures into the Blackboard (in-process)."""
    import asyncio
    from holmes_swarm.api.app import build_app
    from holmes_swarm.blackboard.schema import Signal
    from datetime import datetime, timezone
    application = build_app(config_path=config)
    bus = application.state.bus
    svc = application.state.investigation_service

    async def _main():
        path = Path(fixtures)
        if path.exists():
            for f in sorted(path.glob("*.json")):
                typer.echo(f"loading {f.name}")
                data = json.loads(f.read_text())
                entity_id = data.get("entity_id") or entity or "900123456-7"
                # submit as a no-agent investigation to push signals with origin
                # autonomous-monitoring by feeding the data through each agent
                # in scope-less mode. This produces per-agent signals.
                for agent_id in ["contracting", "logistics", "medical", "whistleblower"]:
                    agent = application.state.registry.get(agent_id)
                    if agent is None:
                        continue
                    batch = data if isinstance(data, dict) else {"entity_id": entity_id}
                    batch = {**batch, "entity_id": entity_id}
                    try:
                        signals = await agent.run(batch, scope=None)
                    except Exception as exc:
                        typer.echo(f"  {agent_id}: error {exc}")
                        continue
                    for s in signals:
                        s.entity_id = s.entity_id or entity_id
                        s.origin = {"kind": "autonomous-monitoring"}
                        try:
                            await bus.publish(s)
                        except Exception:
                            pass
        if autonomous_flood and entity:
            for i in range(autonomous_flood):
                s = Signal(
                    entity_id=entity,
                    signal_type="financial",
                    source_agent="contracting",
                    confidence=0.95,
                    evidence={"summary": "flood", "i": i},
                    origin={"kind": "autonomous-monitoring"},
                    emitted_at=datetime.now(timezone.utc),
                )
                try:
                    await bus.publish(s)
                except Exception:
                    pass
        typer.echo(f"signals on bus: {len(bus.all_signals())}")
        typer.echo(f"alerts on bus:  {len(bus.list_alerts())}")

    asyncio.run(_main())


@app.command()
def simulate_outage(agent: str = typer.Option(..., "--agent"), seconds: int = typer.Option(60)):
    """Simulate an agent outage (no-op for v1 single-process)."""
    typer.echo(f"simulated outage for agent={agent} for {seconds}s (no-op in v1)")


@app.command()
def simulate_call(agent: str = typer.Option(..., "--agent"), url: str = typer.Option(..., "--url")):
    """Trigger an outbound call from an agent (use to verify allow-list)."""
    import asyncio
    from holmes_swarm.api.app import build_app
    application = build_app(config_path="config/example.yml")
    a = application.state.registry.get(agent)
    if a is None or not getattr(a, "http", None):
        typer.echo(f"agent {agent} has no http client (deny by default; FR-019)")
        raise typer.Exit(code=2)

    async def _go():
        try:
            r = await a.http.get(url)
            typer.echo(f"unexpected success: {r.status_code}")
        except Exception as exc:
            typer.echo(f"blocked/error as expected: {exc}")

    asyncio.run(_go())


@app.command()
def register_agent(path: str = typer.Option(..., help="module:ClassName")):
    """Dynamically register an agent class."""
    import importlib
    mod_name, _, cls_name = path.partition(":")
    mod = importlib.import_module(mod_name)
    cls = getattr(mod, cls_name)
    from holmes_swarm.api.app import build_app
    application = build_app(config_path="config/example.yml")
    inst = cls() if callable(cls) else cls
    application.state.registry.register(inst)
    typer.echo(f"registered {cls_name}")


@app.command()
def wait(seconds: int = typer.Option(60)):
    """Sleep N seconds (used in scripted demos)."""
    import time
    time.sleep(seconds)


@app.command()
def chat(
    agent_id: str = typer.Option(..., "--agent", help="Agent id (e.g. contracting, logistics, medical, whistleblower)"),
    message: Optional[str] = typer.Option(None, "--message", "-m", help="Free-text batch payload as a single JSON object"),
    batch_file: Optional[str] = typer.Option(None, "--batch-file", "-f", help="Path to a JSON file with the batch payload"),
    investigation_id: Optional[str] = typer.Option(None, "--investigation-id", help="If set, signals carry origin=investigation"),
    entity: Optional[str] = typer.Option(None, "--entity", help="Override entity_id in the batch"),
    config: str = typer.Option("config/example.yml", "--config", "-c"),
):
    """Run a single agent on an ad-hoc batch and print the returned signals.

    Bypasses the Consensus Agent and the Blackboard so you can validate that
    an agent is wired up and returning the expected signals. Pass the batch
    inline via --message ('{"contracts":[...]}') or from a file via --batch-file.
    """
    import asyncio
    from holmes_swarm.api.app import build_app

    if message is None and batch_file is None:
        typer.echo("provide --message 'JSON' or --batch-file path.json")
        raise typer.Exit(code=2)
    if message is not None and batch_file is not None:
        typer.echo("use either --message or --batch-file, not both")
        raise typer.Exit(code=2)

    if batch_file:
        raw = Path(batch_file).read_text()
    else:
        raw = message  # type: ignore[assignment]
    try:
        batch = json.loads(raw)  # type: ignore[arg-type]
    except json.JSONDecodeError as exc:
        typer.echo(f"invalid JSON: {exc}")
        raise typer.Exit(code=2)

    if not isinstance(batch, dict):
        typer.echo("batch payload must be a JSON object")
        raise typer.Exit(code=2)
    if entity:
        batch["entity_id"] = entity

    scope = None
    if investigation_id:
        from holmes_swarm.investigations.models import InvestigationScope
        scope = InvestigationScope(investigation_request_id=investigation_id, target_entity_id=batch.get("entity_id") or "")

    application = build_app(config_path=config)
    agent = application.state.registry.get(agent_id)
    if agent is None:
        typer.echo(f"unknown agent: {agent_id}; available: {sorted(application.state.registry.ids())}")
        raise typer.Exit(code=2)

    async def _go():
        try:
            signals = await agent.run(batch, scope=scope)
        except Exception as exc:
            typer.echo(f"agent.run() raised: {type(exc).__name__}: {exc}")
            raise typer.Exit(code=1)
        typer.echo(json.dumps(
            [s.model_dump(mode="json") for s in signals],
            indent=2,
            default=str,
        ))
        typer.echo(f"# {len(signals)} signal(s) from agent '{agent_id}'")

    asyncio.run(_go())


@app.command()
def alert(alert_id: str = typer.Option(..., "--alert-id")):
    """Print one alert in full (FR-016 / SC-004)."""
    import asyncio
    from holmes_swarm.api.app import build_app
    application = build_app(config_path="config/example.yml")
    a = application.state.bus.get_alert(alert_id)
    typer.echo(json.dumps(a.model_dump(mode="json") if a else {"error": "not found"}, indent=2, default=str))
