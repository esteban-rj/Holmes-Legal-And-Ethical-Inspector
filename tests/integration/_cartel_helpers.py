"""Shared helpers for the Cartel de la Cardiología test case.

Loads `tests/fixtures/cartel_cardiologia.json` and
`tests/fixtures/secop_snapshot_cartel.json`, builds a SECOP offline-cache
source, and returns a fully wired app + a data_loader so the
InvestigationService has the case data injected at the agent boundary.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from holmes_swarm.agents.contracting import ContractingAgent
from holmes_swarm.api.app import build_app
from holmes_swarm.data_sources.secop import SecopOfflineCache

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"


def load_case() -> dict[str, Any]:
    return json.loads((FIXTURES / "cartel_cardiologia.json").read_text())


def load_secop_snapshot() -> list:
    return json.loads((FIXTURES / "secop_snapshot_cartel.json").read_text())


def make_secop_source() -> SecopOfflineCache:
    return SecopOfflineCache(snapshot_path=FIXTURES / "secop_snapshot_cartel.json")


def make_data_loader(case: dict[str, Any]):
    """Return a callable (entity_id) -> dict for InvestigationService.data_loader.

    Builds a single big batch containing the contracts, events, clinical
    record, and PQRs for the entity under investigation.

    The cartel scenario implies *implausible monthly volume* (the councillor
    mentioned roughly one cateterismo per day). We pad `procedures` to ~130
    records to push the Medical Agent over the 120/month cap.
    """
    entity = case["entity"]
    pqrs = case.get("pqrs", [])
    base_procs = (case.get("clinical") or {}).get("procedures", []) or []
    target_volume = 130
    padded_procs = (base_procs * ((target_volume // max(1, len(base_procs))) + 1))[
        :target_volume
    ]

    def loader(_entity_id: str) -> dict[str, Any]:
        clinical = case.get("clinical") or {}
        return {
            "entity_id": entity["id"],
            "contracts": case.get("contracts", []),
            "specialty_hint": entity.get("specialty"),
            "events": case.get("events", []),
            "specialty": clinical.get("specialty", entity.get("specialty")),
            "services": clinical.get("services", []),
            "procedures": padded_procs,
            "pqrs": pqrs,
        }

    return loader


@dataclass
class CaseApp:
    """Wraps the FastAPI app with case-specific overrides."""

    app: Any
    secop_source: SecopOfflineCache


def build_case_app(case: dict[str, Any] | None = None) -> CaseApp:
    case = case or load_case()
    application = build_app(config_path="config/example.yml")
    secop_source = make_secop_source()

    # Replace the contracting agent so it uses our SECOP offline cache.
    registry = application.state.registry
    current = registry.get("contracting")
    if current is not None:
        threshold = current.confidence_threshold
        registry.unregister("contracting")
        # 75th percentile reference keeps the 50%-of-ref cut-off sensitive
        # to cartel pricing even when the SECOP snapshot is small.
        new_contracting = ContractingAgent(
            secop_source=secop_source, secop_percentile=0.75
        )
        new_contracting.confidence_threshold = threshold
        registry.register(new_contracting)

    svc = application.state.investigation_service
    svc.data_loader = make_data_loader(case)
    return CaseApp(app=application, secop_source=secop_source)
